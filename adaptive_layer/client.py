"""AdaptiveClient: a thin pymongo wrapper (Proxy/Interceptor layer).

write()/read() no longer take settings as call arguments - they look up
the collection's *active settings* internally (set via
set_active_settings(), defaulting to MODERATE_SETTINGS for any collection
that hasn't been told otherwise). This is the seam Phase 6's Feedback Loop
plugs into: it calls set_active_settings() on a timer, and the very next
write()/read() on that collection picks up the new settings automatically.

Every operation is logged via _log_op, which also feeds an optional
AccessPatternMonitor (Phase 3) in addition to printing for visibility.
"""

import time

from pymongo import MongoClient
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

from adaptive_layer.decision import MODERATE_SETTINGS, Settings

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


class AdaptiveClient:
    """Wraps a pymongo MongoClient. Applies each collection's currently
    active settings (see set_active_settings) and logs every operation."""

    def __init__(self, uri, db_name, monitor=None):
        self._client = MongoClient(uri)
        self._db = self._client[db_name]
        self._monitor = monitor
        self._active_settings: dict[str, Settings] = {}

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
        result = coll.insert_one(doc)
        latency_ms = (time.monotonic() - start) * 1000
        self._log_op(collection, "write", time.time(), latency_ms)
        return result

    def read(self, collection, query):
        """Find one document in `collection` matching `query`, using its
        current active read concern and read preference. Returns the
        document or None."""
        settings = self.get_active_settings(collection)
        start = time.monotonic()
        coll = self._db.get_collection(
            collection,
            read_concern=ReadConcern(level=settings.read_concern),
            read_preference=_resolve_read_preference(settings.read_preference),
        )
        doc = coll.find_one(query)
        latency_ms = (time.monotonic() - start) * 1000
        self._log_op(collection, "read", time.time(), latency_ms)
        return doc

    def _log_op(self, collection, op_type, timestamp, latency_ms):
        """Print for visibility, and - if a monitor was supplied - feed it
        so AccessPatternMonitor.get_stats(collection) reflects real traffic."""
        print(
            f"[AdaptiveClient] {op_type:5s} collection={collection!r} "
            f"latency={latency_ms:.2f}ms ts={timestamp:.3f}"
        )
        if self._monitor is not None:
            self._monitor.record(collection, op_type, timestamp)

    def close(self):
        self._client.close()
