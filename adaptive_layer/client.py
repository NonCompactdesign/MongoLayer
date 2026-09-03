"""AdaptiveClient: a thin pymongo wrapper (Proxy/Interceptor layer).

Settings are still passed explicitly by the caller on every read/write
(Phase 5 replaces this with a per-collection "active settings" lookup).
Every operation is logged via _log_op, which now also feeds an optional
AccessPatternMonitor (Phase 3) in addition to printing for visibility.
"""

import time

from pymongo import MongoClient
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

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
    """Wraps a pymongo MongoClient, applying explicit per-call settings
    and logging every operation it performs."""

    def __init__(self, uri, db_name, monitor=None):
        self._client = MongoClient(uri)
        self._db = self._client[db_name]
        self._monitor = monitor

    def write(self, collection, doc, write_concern="majority"):
        """Insert `doc` into `collection` with the given write concern.
        Returns the pymongo InsertOneResult."""
        start = time.monotonic()
        coll = self._db.get_collection(
            collection, write_concern=WriteConcern(w=write_concern)
        )
        result = coll.insert_one(doc)
        latency_ms = (time.monotonic() - start) * 1000
        self._log_op(collection, "write", time.time(), latency_ms)
        return result

    def read(self, collection, query, read_concern="local", read_preference="primary"):
        """Find one document in `collection` matching `query`, with the
        given read concern and read preference. Returns the document or None."""
        start = time.monotonic()
        coll = self._db.get_collection(
            collection,
            read_concern=ReadConcern(level=read_concern),
            read_preference=_resolve_read_preference(read_preference),
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
