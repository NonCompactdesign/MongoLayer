"""Locust-based benchmark workload generator (Phase 8).

Decision (logged in the project vault): use `locust` instead of a
hand-rolled asyncio generator - rate control, ramping, and per-request-type
percentile stats come for free. MongoDB isn't HTTP, so Locust's request
tracking has to be fired manually via events.request.fire(...) instead of
happening automatically.

One User class, one shared AdaptiveClient/Monitor/FeedbackLoop for the
whole test (not one per simulated user - pymongo's MongoClient is meant to
be shared and pooled, and "adaptive" mode specifically needs everyone
observing/reacting to the SAME aggregate traffic pattern, not N independent
copies of it). Shared state lives on `environment` via test_start/test_stop
listeners, which is the idiomatic Locust pattern for this.

Configuration is via environment variables so Phase 9's run_benchmark.py
can drive this file across configurations/presets without editing code:

  BENCHMARK_MODE     static_fast | static_safe | adaptive   (default: adaptive)
  WORKLOAD_PRESET    READ_HEAVY | WRITE_HEAVY | MIXED_BURSTY (default: MIXED_BURSTY)
  BENCHMARK_COLLECTION  Mongo collection name  (default: bench)
  BENCHMARK_KEY_SPACE   number of distinct synthetic keys  (default: 1000)
  BENCHMARK_OPS_PER_SEC per-user target ops/sec via constant_throughput (default: 5)

Standalone sanity run:
  locust -f benchmark/locustfile.py --headless -u 10 -r 5 -t 30s
"""

import os
import random
import time

from locust import User, constant_throughput, events, task

from adaptive_layer.client import AdaptiveClient
from adaptive_layer.decision import FAST_SETTINGS, SAFE_SETTINGS
from adaptive_layer.feedback import FeedbackLoop
from adaptive_layer.monitor import AccessPatternMonitor

URI = "mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0"
DB_NAME = "benchmark"

MODE = os.environ.get("BENCHMARK_MODE", "adaptive")
PRESET = os.environ.get("WORKLOAD_PRESET", "MIXED_BURSTY")
COLLECTION = os.environ.get("BENCHMARK_COLLECTION", "bench")
KEY_SPACE = int(os.environ.get("BENCHMARK_KEY_SPACE", "1000"))
OPS_PER_SEC_PER_USER = float(os.environ.get("BENCHMARK_OPS_PER_SEC", "5"))

# Adaptive-mode Feedback Loop tuning (production-scale defaults, not the
# short test-only values used in Phase 6's unit/integration tests).
WINDOW_SECONDS = 15
INTERVAL_SECONDS = 5
COOLDOWN_SECONDS = 10

_MIXED_BURSTY_PERIOD_SECONDS = 15  # how long each read-heavy/write-heavy sub-phase lasts


def current_write_probability(preset, now=None):
    """Fraction of ops that should be writes, for the given preset.

    MIXED_BURSTY oscillates between read-heavy and write-heavy sub-phases
    purely as a function of wall-clock time (period-based), rather than via
    a Locust LoadTestShape - LoadTestShape controls simulated USER COUNT
    over time, not the read/write MIX each user generates, so it's the
    wrong tool for what this preset actually needs to demonstrate: the
    adaptive layer tracking a pattern that shifts *within* a steady user
    count. A simple time-modulo probability is simpler, easier to reason
    about, and just as effective at producing the oscillation.
    """
    if preset == "READ_HEAVY":
        return 0.05
    if preset == "WRITE_HEAVY":
        return 0.95
    if preset == "MIXED_BURSTY":
        now = now if now is not None else time.time()
        phase = int(now // _MIXED_BURSTY_PERIOD_SECONDS) % 2
        return 0.95 if phase == 0 else 0.05
    raise ValueError(f"Unknown WORKLOAD_PRESET {preset!r}")


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    monitor = AccessPatternMonitor(window_seconds=WINDOW_SECONDS)
    client = AdaptiveClient(URI, DB_NAME, monitor=monitor)

    feedback_loop = None
    if MODE == "static_fast":
        client.set_active_settings(COLLECTION, FAST_SETTINGS)
    elif MODE == "static_safe":
        client.set_active_settings(COLLECTION, SAFE_SETTINGS)
    elif MODE == "adaptive":
        feedback_loop = FeedbackLoop(
            client, monitor, interval_seconds=INTERVAL_SECONDS, cooldown_seconds=COOLDOWN_SECONDS
        )
        feedback_loop.start()
    else:
        raise ValueError(f"Unknown BENCHMARK_MODE {MODE!r}")

    environment.shared_client = client
    environment.shared_monitor = monitor
    environment.shared_feedback_loop = feedback_loop
    print(
        f"[locustfile] test_start: mode={MODE!r} preset={PRESET!r} "
        f"collection={COLLECTION!r} key_space={KEY_SPACE}"
    )


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    if getattr(environment, "shared_feedback_loop", None) is not None:
        environment.shared_feedback_loop.stop()
    client = getattr(environment, "shared_client", None)
    if client is not None:
        client.close()
    print("[locustfile] test_stop: cleaned up shared client/feedback loop")


class AdaptiveMongoUser(User):
    """One simulated concurrent caller. Reuses the shared client/monitor
    set up in on_test_start rather than opening its own connection -
    correct because pymongo's MongoClient is meant to be shared/pooled,
    and required for "adaptive" mode so every user observes and reacts to
    the same aggregate access pattern."""

    wait_time = constant_throughput(OPS_PER_SEC_PER_USER)

    @task
    def read_or_write(self):
        client = self.environment.shared_client
        key = random.randrange(KEY_SPACE)
        op_name = "write" if random.random() < current_write_probability(PRESET) else "read"

        start = time.monotonic()
        exc = None
        try:
            if op_name == "write":
                client.write(COLLECTION, {"key": key, "ts": time.time()})
            else:
                client.read(COLLECTION, {"key": key})
        except Exception as caught:  # noqa: BLE001 - Locust wants failures reported, not raised
            exc = caught

        response_time_ms = (time.monotonic() - start) * 1000
        self.environment.events.request.fire(
            request_type="mongo",
            name=op_name,
            response_time=response_time_ms,
            response_length=0,
            exception=exc,
            context={},
        )
