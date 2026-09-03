"""AccessPatternMonitor: turns a stream of logged operations into
per-collection access-pattern stats.

Deliberately decoupled from MongoDB and from AdaptiveClient - it only ever
sees (collection, op_type, timestamp) tuples, so it can be unit tested with
fake timestamps instead of needing a live database or real sleep() calls.
"""

import collections
import time
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Stats:
    read_count: int
    write_count: int
    read_write_ratio: float  # reads per write; float('inf') if reads>0 and writes==0
    write_freq: float  # writes per second, averaged over the window
    seconds_since_last_write: Optional[float]  # None if no write seen in the window


NEUTRAL_STATS = Stats(
    read_count=0,
    write_count=0,
    read_write_ratio=0.0,
    write_freq=0.0,
    seconds_since_last_write=None,
)

_VALID_OP_TYPES = ("read", "write")


class AccessPatternMonitor:
    """Sliding-window per-collection access pattern tracker.

    record() and get_stats() are the only two entry points a caller needs.
    Everything else (which collections exist, how big each window is) is
    internal bookkeeping.
    """

    def __init__(self, window_seconds=30):
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.window_seconds = window_seconds
        # collection -> deque[(timestamp, op_type)], oldest first
        self._events = collections.defaultdict(collections.deque)

    def record(self, collection, op_type, timestamp):
        if op_type not in _VALID_OP_TYPES:
            raise ValueError(f"op_type must be one of {_VALID_OP_TYPES}, got {op_type!r}")
        self._events[collection].append((timestamp, op_type))
        self._prune(collection, timestamp)

    def get_stats(self, collection, now=None):
        """Return a Stats snapshot for `collection` as of `now` (defaults to
        time.time()). A collection with no recent events returns NEUTRAL_STATS
        rather than raising - callers (the Decision Engine in particular)
        should never have to special-case "never seen this collection"."""
        if now is None:
            now = time.time()

        if collection not in self._events:
            return NEUTRAL_STATS

        self._prune(collection, now)
        events = self._events[collection]
        if not events:
            return NEUTRAL_STATS

        read_count = sum(1 for _, op in events if op == "read")
        write_count = sum(1 for _, op in events if op == "write")

        if write_count > 0:
            read_write_ratio = read_count / write_count
        elif read_count > 0:
            read_write_ratio = float("inf")
        else:
            read_write_ratio = 0.0

        write_freq = write_count / self.window_seconds

        write_timestamps = [ts for ts, op in events if op == "write"]
        seconds_since_last_write = (now - max(write_timestamps)) if write_timestamps else None

        return Stats(
            read_count=read_count,
            write_count=write_count,
            read_write_ratio=read_write_ratio,
            write_freq=write_freq,
            seconds_since_last_write=seconds_since_last_write,
        )

    def _prune(self, collection, now):
        """Drop events older than the sliding window. Called on every
        record() (so memory doesn't grow unbounded even if get_stats() is
        never called) and again at the start of every get_stats() (so a
        stale record() call doesn't leave a too-fresh view)."""
        events = self._events[collection]
        cutoff = now - self.window_seconds
        while events and events[0][0] < cutoff:
            events.popleft()
