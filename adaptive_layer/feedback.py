"""FeedbackLoop: ties Monitor -> Decision Engine -> AdaptiveClient
together automatically, on a timer, with a cooldown to prevent oscillation
("flapping") between settings under bursty load.

This is the actual novel contribution of the project - Phases 2-5 are all
plumbing that makes this phase possible. See the project vault's Design
Rationale note for the full argument.

tick() is deliberately public and separate from run()'s threaded loop: it
lets tests (and manual scripts) drive one evaluation pass directly, with a
fake clock and no real sleeping, instead of needing a background thread.
start()/stop() are what demo_app.py and the benchmark harness actually use.
"""

import threading
import time

from adaptive_layer.decision import decide


class FeedbackLoop:
    def __init__(self, client, monitor, interval_seconds=30, cooldown_seconds=60, clock=time.time):
        self._client = client
        self._monitor = monitor
        self.interval_seconds = interval_seconds
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock

        # collection -> timestamp of the last time we actually changed its
        # settings (not just evaluated it - see tick()'s logic).
        self._last_switch = {}

        self._thread = None
        self._stop_event = threading.Event()

    def tick(self):
        """One evaluation pass over every collection the monitor has seen.
        For each: decide what its settings *should* be right now; if that's
        different from what's currently active AND the cooldown has elapsed
        since the last switch, apply it. A decision that doesn't actually
        change anything never touches the cooldown timer - cooldown only
        gates real switches, not routine "no change needed" evaluations."""
        now = self._clock()
        for collection in self._monitor.known_collections():
            stats = self._monitor.get_stats(collection, now=now)
            decision = decide(stats)
            current = self._client.get_active_settings(collection)

            if decision == current:
                continue

            last_switch = self._last_switch.get(collection)
            if last_switch is not None and (now - last_switch) < self.cooldown_seconds:
                continue  # would switch, but still in cooldown - suppress

            self._client.set_active_settings(collection, decision)
            self._last_switch[collection] = now

    def run(self):
        """Blocking loop: tick(), sleep interval_seconds, repeat, until
        stop() is called. Intended to run in a background thread via
        start() - call run() directly only in tests that want to control
        the loop manually."""
        while not self._stop_event.is_set():
            self.tick()
            self._stop_event.wait(self.interval_seconds)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("FeedbackLoop is already running")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def stop(self, join_timeout=None):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout or (self.interval_seconds + 1))
            self._thread = None
