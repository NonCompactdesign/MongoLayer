"""RecordingMonitor: wraps a real AccessPatternMonitor and additionally
keeps a small ring buffer of recent ops for the dashboard's live feed.

Duck-types the same interface AdaptiveClient expects from a monitor
(record/get_stats/known_collections) and forwards everything to the real
Monitor - adaptive_layer code is completely untouched by the dashboard.
"""

import collections
import threading


class RecordingMonitor:
    def __init__(self, real_monitor, feed_maxlen=30):
        self._real = real_monitor
        self._feed = collections.deque(maxlen=feed_maxlen)
        self._lock = threading.Lock()

    def record(self, collection, op_type, timestamp):
        self._real.record(collection, op_type, timestamp)
        with self._lock:
            self._feed.appendleft({"collection": collection, "op_type": op_type, "timestamp": timestamp})

    def get_stats(self, collection, now=None):
        return self._real.get_stats(collection, now=now)

    def known_collections(self):
        return self._real.known_collections()

    def recent_ops(self):
        with self._lock:
            return list(self._feed)
