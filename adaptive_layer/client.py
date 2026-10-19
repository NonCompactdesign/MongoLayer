"""AdaptiveClient: a thin pymongo wrapper (Proxy/Interceptor layer).

write()/read() no longer take settings as call arguments - they look up
the collection's *active settings* internally (set via
set_active_settings(), defaulting to MODERATE_SETTINGS for any collection
that hasn't been told otherwise). This is the seam Phase 6's Feedback Loop
plugs into: it calls set_active_settings() on a timer, and the very next
write()/read() on that collection picks up the new settings automatically.

Every operation is logged via _log_op, which also feeds an optional
AccessPatternMonitor (Phase 3) in addition to printing for visibility. The
monitor also gets each operation's latency and the name of the settings it ran
under, which the SAFE latency budget uses (adaptive_layer/rules.py). Failed
operations are logged too: the monitor tracks what the application asks for.

Read-your-own-writes per key (rule 6): with read_own_writes_seconds=N, every
written document's key_field value and _id are remembered for N seconds, and a
read whose query asks for one of them by equality goes to the primary, even if
the collection is FAST. Only those reads pay the primary's distance; all other
reads keep using `nearest`. The memory is per AdaptiveClient (per process): it
covers this process's own writes, not other processes'. N has to outlast the
replication delay; on the test bed it is well under a second even with 86 ms
between nodes, and the benchmark uses 5 s.
"""

import collections
import time

from pymongo import MongoClient
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

from adaptive_layer._locks import native_lock
from adaptive_layer.decision import MODERATE_SETTINGS, Settings, settings_name

_READ_PREFERENCES = {
    "primary": ReadPreference.PRIMARY,
    "primaryPreferred": ReadPreference.PRIMARY_PREFERRED,
    "secondary": ReadPreference.SECONDARY,
    "secondaryPreferred": ReadPreference.SECONDARY_PREFERRED,
    "nearest": ReadPreference.NEAREST,
}


def _resolve_read_preference(name):
    try:
        return _READ_PREFERENCES[name]
    except KeyError:
        raise ValueError(
            f"Unknown read preference {name!r}; expected one of {sorted(_READ_PREFERENCES)}"
        )


def _hashable(value):
    try:
        hash(value)
    except TypeError:
        return False
    return True


class AdaptiveClient:
    """Wraps a pymongo MongoClient. Applies each collection's currently
    active settings (see set_active_settings) and logs every operation."""

    def __init__(self, uri, db_name, monitor=None, verbose=True, key_field="_id", read_own_writes_seconds=None):
        self._client = MongoClient(uri)
        self._verbose = verbose
        self._db = self._client[db_name]
        self._monitor = monitor
        self._active_settings: dict[str, Settings] = {}
        self.key_field = key_field
        self.read_own_writes_seconds = read_own_writes_seconds
        # collection -> OrderedDict[(field, value) -> time written], oldest first
        self._recent = collections.defaultdict(collections.OrderedDict)
        self._recent_lock = native_lock()  # see adaptive_layer/_locks.py
        self.redirected_reads = 0  # reads sent to the primary by read-your-own-writes

    @property
    def mongo_client(self):
        """The underlying pymongo MongoClient."""
        return self._client

    def set_active_settings(self, collection, settings: Settings):
        """Set the settings write()/read() will use for `collection` from
        this point on. This is the only mutation point in the class - the
        Feedback Loop (Phase 6) is the intended caller."""
        self._active_settings[collection] = settings

    def get_active_settings(self, collection) -> Settings:
        """The settings currently in effect for `collection`. Collections
        that have never had set_active_settings() called for them default
        to MODERATE_SETTINGS - never an error, never undefined behavior."""
        return self._active_settings.get(collection, MODERATE_SETTINGS)

    def write(self, collection, doc):
        """Insert `doc` into `collection` using its current active write
        concern. Returns the pymongo InsertOneResult."""
        settings = self.get_active_settings(collection)
        start = time.monotonic()
        coll = self._db.get_collection(
            collection, write_concern=WriteConcern(w=settings.write_concern)
        )
        error = None
        try:
            return coll.insert_one(doc)  # pymongo adds _id to doc
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            # Logged even when it failed: a failed write is still a write the application asked for.
            latency_ms = (time.monotonic() - start) * 1000
            if self.read_own_writes_seconds and error is None:
                self._remember(collection, doc)
            self._log_op(collection, "write", time.time(), latency_ms, settings, error)

    def read(self, collection, query):
        """Find one document in `collection` matching `query`, using its
        current active read concern and read preference. Returns the
        document or None."""
        settings = self.get_active_settings(collection)
        read_preference = settings.read_preference
        # primaryPreferred already reads from the primary while there is one: nothing to redirect
        if (read_preference not in ("primary", "primaryPreferred") and self.read_own_writes_seconds
                and self._recently_written(collection, query)):
            read_preference = "primary"
            self.redirected_reads += 1
        start = time.monotonic()
        coll = self._db.get_collection(
            collection,
            read_concern=ReadConcern(level=settings.read_concern),
            read_preference=_resolve_read_preference(read_preference),
        )
        error = None
        try:
            return coll.find_one(query)
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            self._log_op(collection, "read", time.time(), latency_ms, settings, error)

    def _remember(self, collection, doc):
        now = time.monotonic()
        with self._recent_lock:
            recent = self._recent[collection]
            for field in {self.key_field, "_id"}:
                if field in doc and _hashable(doc[field]):
                    entry = (field, doc[field])
                    recent.pop(entry, None)
                    recent[entry] = now
            cutoff = now - self.read_own_writes_seconds
            while recent and next(iter(recent.values())) < cutoff:
                recent.popitem(last=False)

    def _recently_written(self, collection, query):
        """True if the query asks, by equality, for a key or _id written in the last N seconds."""
        if not isinstance(query, dict):
            return False
        cutoff = time.monotonic() - self.read_own_writes_seconds
        with self._recent_lock:
            recent = self._recent.get(collection)
            if not recent:
                return False
            for field in {self.key_field, "_id"}:
                value = query.get(field)
                if value is None or not _hashable(value):
                    continue  # absent, or an operator like {"$in": [...]}
                written = recent.get((field, value))
                if written is not None and written >= cutoff:
                    return True
        return False

    def _log_op(self, collection, op_type, timestamp, latency_ms, settings=None, error=None):
        """Print for visibility, and - if a monitor was supplied - feed it
        so AccessPatternMonitor.get_stats(collection) reflects real traffic."""
        if self._verbose:
            print(
                f"[AdaptiveClient] {op_type:5s} collection={collection!r} "
                f"latency={latency_ms:.2f}ms ts={timestamp:.3f}" + (f" error={error}" if error else "")
            )
        if self._monitor is not None:
            tag = settings_name(settings) if settings is not None else None
            self._monitor.record(collection, op_type, timestamp, latency_ms=latency_ms, tag=tag)

    def close(self):
        self._client.close()
