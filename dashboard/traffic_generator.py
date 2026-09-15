"""TrafficGenerator: a controllable, single-threaded read/write traffic
source for the live dashboard. Deliberately simpler than the Locust-based
benchmark harness (Phase 8/9) - this isn't trying to simulate concurrent
users or measure percentiles, it's a live knob a person turns during a demo
to change read:write ratio and rate on the fly and watch the system react.
"""

import random
import threading
import time


class TrafficGenerator:
    def __init__(self, client, collection):
        self._client = client
        self._collection = collection
        self._lock = threading.Lock()
        self._write_ratio = 0.5  # fraction of ops that are writes, 0.0-1.0
        self._ops_per_sec = 5.0
        self._running = threading.Event()
        self._thread = None
        self._op_count = 0

    def configure(self, write_ratio=None, ops_per_sec=None):
        with self._lock:
            if write_ratio is not None:
                self._write_ratio = max(0.0, min(1.0, write_ratio))
            if ops_per_sec is not None:
                self._ops_per_sec = max(0.1, ops_per_sec)

    def snapshot(self):
        with self._lock:
            return {
                "running": self._running.is_set(),
                "write_ratio": self._write_ratio,
                "ops_per_sec": self._ops_per_sec,
                "op_count": self._op_count,
            }

    def reset_count(self):
        """Zero the displayed op counter. Purely cosmetic - does not affect
        the traffic itself or any real system state (Monitor/settings)."""
        with self._lock:
            self._op_count = 0

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running.clear()

    def _run(self):
        while self._running.is_set():
            with self._lock:
                write_ratio = self._write_ratio
                delay = 1.0 / self._ops_per_sec

            if random.random() < write_ratio:
                self._client.write(self._collection, {"i": self._op_count, "ts": time.time()})
            else:
                self._client.read(self._collection, {})

            with self._lock:
                self._op_count += 1

            time.sleep(delay)
