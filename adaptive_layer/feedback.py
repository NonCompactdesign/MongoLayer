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

With no extra arguments the loop runs the traffic rule alone, exactly as
before. signals=SignalConfig() turns on the SAFE latency budget
(adaptive_layer/rules.py); policies={collection: CollectionPolicy(...)} works
with or without it; thresholds=SIGNALS_THRESHOLDS adds hysteresis. Every
switch records the reason the rules gave.
"""

import threading
import time
from dataclasses import dataclass

from adaptive_layer.decision import DEFAULT_THRESHOLDS, Settings, Thresholds
from adaptive_layer.monitor import Stats
from adaptive_layer.rules import NO_POLICY, CollectionPolicy, Decision, SignalConfig, apply_rules


@dataclass(frozen=True)
class SwitchEvent:
    """One real settings change: when, for which collection, from what to
    what, and the Stats that triggered it."""

    timestamp: float
    collection: str
    old: Settings
    new: Settings
    stats: Stats
    reason: str = ""  # why, e.g. "write share 0.52 > 0.40" (see rules.Decision)


class FeedbackLoop:
    def __init__(
        self, client, monitor, interval_seconds=30, cooldown_seconds=60, clock=time.time,
        thresholds: Thresholds = DEFAULT_THRESHOLDS,
        signals: SignalConfig | None = None,
        policies: dict[str, CollectionPolicy] | None = None,
    ):
        self._client = client
        self._monitor = monitor
        self.interval_seconds = interval_seconds
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._thresholds = thresholds
        self._signals = signals
        self._policies = dict(policies or {})

        # collection -> (time of the last budget breach, the p95 that breached it)
        self._budget_breach = {}
        # collection -> the latest Decision, switched to or not (for displays / demos)
        self.last_decisions: dict[str, Decision] = {}

        # collection -> timestamp of the last time we actually changed its
        # settings (not just evaluated it - see tick()'s logic).
        self._last_switch = {}

        # Every real switch, oldest first. Appended from the loop thread;
        # read it with list(loop.switch_log) from another thread.
        self.switch_log: list[SwitchEvent] = []

        self._thread = None
        self._stop_event = threading.Event()

        # A collection with a floor above MODERATE (or a ceiling below it) must not
        # run its first operations on the client's MODERATE default.
        for collection, policy in self._policies.items():
            start, _ = policy.clamp(client.get_active_settings(collection))
            if start != client.get_active_settings(collection):
                client.set_active_settings(collection, start)

    def _safe_write_p95(self, collection, stats, now):
        """The SAFE write p95 the budget rule should judge. A breach is remembered
        for budget_backoff_seconds: once the loop has left SAFE there are no new
        SAFE samples, and without the memory it would go straight back."""
        cfg = self._signals
        if cfg is None or cfg.write_p95_budget_ms is None:
            return None
        measured = stats.write_p95("SAFE")
        if measured is not None and measured[1] >= cfg.budget_min_samples:
            p95 = measured[0]
            if p95 > cfg.write_p95_budget_ms:
                self._budget_breach[collection] = (now, p95)
                return p95
        breach = self._budget_breach.get(collection)
        if breach is not None and now - breach[0] < cfg.budget_backoff_seconds:
            return breach[1]
        return measured[0] if measured is not None and measured[1] >= cfg.budget_min_samples else None

    def decide(self, collection, now=None):
        """The Decision for one collection right now, without applying it."""
        now = self._clock() if now is None else now
        stats = self._monitor.get_stats(collection, now=now)
        return stats, apply_rules(
            stats,
            current=self._client.get_active_settings(collection),
            config=self._signals,
            policy=self._policies.get(collection, NO_POLICY),
            thresholds=self._thresholds,
            safe_write_p95_ms=self._safe_write_p95(collection, stats, now),
        )

    def tick(self):
        """One evaluation pass over every collection the monitor has seen.
        For each: decide what its settings *should* be right now; if that's
        different from what's currently active AND the cooldown has elapsed
        since the last switch, apply it. A decision that doesn't actually
        change anything never touches the cooldown timer - cooldown only
        gates real switches, not routine "no change needed" evaluations."""
        now = self._clock()
        for collection in self._monitor.known_collections():
            stats, decision = self.decide(collection, now)
            self.last_decisions[collection] = decision
            current = self._client.get_active_settings(collection)

            if decision.settings == current:
                continue

            last_switch = self._last_switch.get(collection)
            if last_switch is not None and (now - last_switch) < self.cooldown_seconds:
                continue  # would switch, but still in cooldown - suppress

            self._client.set_active_settings(collection, decision.settings)
            self._last_switch[collection] = now
            self.switch_log.append(SwitchEvent(now, collection, current, decision.settings, stats, decision.reason))

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
