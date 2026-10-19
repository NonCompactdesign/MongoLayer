"""AccessPatternMonitor: turns a stream of logged operations into
per-collection access-pattern stats.

Deliberately decoupled from MongoDB and from AdaptiveClient - it only ever
sees (collection, op_type, timestamp) tuples, so it can be unit tested with
fake timestamps instead of needing a live database or real sleep() calls.

One optional extra per operation feeds the latency budget (adaptive_layer/rules.py):
  latency_ms  how long the operation took, with `tag` = the name of the settings it ran
              under. Stats.write_p95(tag) gives the 95th-percentile write latency per
              settings name in the window.
Callers that pass neither get exactly the original stats.
"""

import collections
import math
import time
from dataclasses import dataclass
from typing import Optional

from adaptive_layer._locks import native_lock


@dataclass(frozen=True)
class Stats:
    read_count: int
    write_count: int
    read_write_ratio: float  # reads per write; float('inf') if reads>0 and writes==0
    write_freq: float  # writes per second, averaged over the window
    seconds_since_last_write: Optional[float]  # None if no write seen in the window
    # ((settings name, p95 write latency in ms, number of writes), ...) for the window
    write_latency: tuple = ()

    @property
    def write_share(self) -> float:
        """Writes as a fraction of all operations in the window; 0.0 if there were none."""
        total = self.read_count + self.write_count
        return self.write_count / total if total else 0.0

    def write_p95(self, tag):
        """(p95 ms, sample count) of writes made under settings `tag` in the window, or None."""
        for name, p95, n in self.write_latency:
            if name == tag:
                return p95, n
        return None


NEUTRAL_STATS = Stats(
    read_count=0,
    write_count=0,
    read_write_ratio=0.0,
    write_freq=0.0,
    seconds_since_last_write=None,
)

_VALID_OP_TYPES = ("read", "write")


def _p95(values):
    """Nearest-rank 95th percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


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
        # collection -> deque[(timestamp, op_type, latency_ms, tag)], oldest first
        self._events = collections.defaultdict(collections.deque)
        # record() is called from many request threads while the feedback loop reads
        self._lock = native_lock()  # see adaptive_layer/_locks.py

    def known_collections(self):
        """Every collection name record() has ever been called for. Used by
        the Feedback Loop (Phase 6) to know which collections to evaluate on
        each tick, without needing the caller to track that list separately.
        Note a collection stays "known" even after all its events have aged
        out of the window - get_stats() for it then correctly returns
        NEUTRAL_STATS, which is exactly what we want the Feedback Loop to see."""
        with self._lock:
            return list(self._events.keys())

    def record(self, collection, op_type, timestamp, latency_ms=None, tag=None):
        if op_type not in _VALID_OP_TYPES:
            raise ValueError(f"op_type must be one of {_VALID_OP_TYPES}, got {op_type!r}")
        with self._lock:
            self._events[collection].append((timestamp, op_type, latency_ms, tag))
            self._prune(collection, timestamp)

    def get_stats(self, collection, now=None):
        """Return a Stats snapshot for `collection` as of `now` (defaults to
        time.time()). A collection with no recent events returns NEUTRAL_STATS
        rather than raising - callers (the Decision Engine in particular)
        should never have to special-case "never seen this collection"."""
        if now is None:
            now = time.time()

        with self._lock:
            if collection not in self._events:
                return NEUTRAL_STATS
            self._prune(collection, now)
            events = list(self._events[collection])
        if not events:
            return NEUTRAL_STATS

        read_count = sum(1 for e in events if e[1] == "read")
        write_count = len(events) - read_count

        if write_count > 0:
            read_write_ratio = read_count / write_count
        elif read_count > 0:
            read_write_ratio = float("inf")
        else:
            read_write_ratio = 0.0

        write_freq = write_count / self.window_seconds

        write_timestamps = [e[0] for e in events if e[1] == "write"]
        seconds_since_last_write = (now - max(write_timestamps)) if write_timestamps else None

        by_tag = collections.defaultdict(list)
        for e in events:
            if e[1] == "write" and e[2] is not None:
                by_tag[e[3]].append(e[2])
        write_latency = tuple((tag, _p95(v), len(v)) for tag, v in sorted(by_tag.items(), key=lambda kv: str(kv[0])))

        return Stats(
            read_count=read_count,
            write_count=write_count,
            read_write_ratio=read_write_ratio,
            write_freq=write_freq,
            seconds_since_last_write=seconds_since_last_write,
            write_latency=write_latency,
        )

    def _prune(self, collection, now):
        """Drop events older than the sliding window. Called on every
        record() (so memory doesn't grow unbounded even if get_stats() is
        never called) and again at the start of every get_stats() (so a
        stale record() call doesn't leave a too-fresh view). Callers hold the lock."""
        events = self._events[collection]
        cutoff = now - self.window_seconds
        while events and events[0][0] < cutoff:
            events.popleft()
