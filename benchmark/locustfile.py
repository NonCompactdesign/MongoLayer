"""Locust-based benchmark workload generator (Phase 8).

Decision (logged in the project vault): use `locust` instead of a
hand-rolled asyncio generator - rate control, ramping, and per-request-type
percentile stats come for free. MongoDB isn't HTTP, so Locust's request
tracking has to be fired manually via events.request.fire(...) instead of
happening automatically.

One User class, one shared AdaptiveClient/Monitor/FeedbackLoop for the
whole test (not one per simulated user - pymongo's MongoClient is meant to
be shared and pooled, and the adaptive mode specifically needs everyone
observing/reacting to the SAME aggregate traffic pattern, not N independent
copies of it). Shared state lives on `environment` via test_start/test_stop
listeners, which is the idiomatic Locust pattern for this.

Configuration is via environment variables so Phase 9's run_benchmark.py
can drive this file across configurations/presets without editing code:

  BENCHMARK_MODE     static_fast | static_moderate | static_safe | adaptive_signals
                     (default: adaptive_signals = the adaptive layer: traffic rule + hysteresis +
                     the SAFE latency budget (adaptive_layer/rules.py) + per-key
                     read-your-own-writes (see BENCHMARK_READ_OWN_WRITES)).
  WORKLOAD_PRESET    READ_HEAVY | WRITE_HEAVY | MIXED_BURSTY | BALANCED | HOTSPOT |
                     RAMP | SPIKE | HIGH_CONCURRENCY | MODEST_WRITES | READ_HEAVY_HOTSPOT
                     (default: MIXED_BURSTY)
  BENCHMARK_COLLECTION  Mongo collection name  (default: bench)
  BENCHMARK_KEY_SPACE   number of distinct synthetic keys  (default: 1000)
  BENCHMARK_OPS_PER_SEC per-user target ops/sec via constant_throughput (default: 5)
  BENCHMARK_SWITCH_LOG  path; if set, an adaptive run writes what settings it chose and when
  BENCHMARK_PROBE_LOG   path; if set, the staleness probes' tally is written there
  BENCHMARK_PROBE_FRACTION share of iterations that are staleness probes (default 0.02; 0 disables)
  BENCHMARK_PAYLOAD_BYTES size of the payload field on every written doc, so
                        writes/reads/replication carry real data (default: 2048)
  BENCHMARK_READ_OWN_WRITES adaptive_signals only: seconds the client remembers its own
                        writes per key and reads them from the primary (default 5; 0 disables)
  BENCHMARK_CLIENT_LOG  path; if set, adaptive_signals writes the client's counters (JSON)

Standalone sanity run:
  locust -f benchmark/locustfile.py --headless -u 10 -r 5 -t 30s
"""

import json
import os
import random
import time

from locust import User, constant_throughput, events, task

from adaptive_layer.client import AdaptiveClient
from adaptive_layer.decision import FAST_SETTINGS, MODERATE_SETTINGS, SAFE_SETTINGS, settings_name
from adaptive_layer.feedback import FeedbackLoop
from adaptive_layer.monitor import AccessPatternMonitor
from benchmark.probe_log import ProbeTally
from benchmark.switch_log import write_switch_log

URI = "mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0"
DB_NAME = "benchmark"

MODE = os.environ.get("BENCHMARK_MODE", "adaptive_signals")
PRESET = os.environ.get("WORKLOAD_PRESET", "MIXED_BURSTY")
COLLECTION = os.environ.get("BENCHMARK_COLLECTION", "bench")
KEY_SPACE = int(os.environ.get("BENCHMARK_KEY_SPACE", "1000"))
OPS_PER_SEC_PER_USER = float(os.environ.get("BENCHMARK_OPS_PER_SEC", "5"))
SWITCH_LOG = os.environ.get("BENCHMARK_SWITCH_LOG")
PROBE_LOG = os.environ.get("BENCHMARK_PROBE_LOG")
PROBE_FRACTION = float(os.environ.get("BENCHMARK_PROBE_FRACTION", "0.02"))
PAYLOAD_BYTES = int(os.environ.get("BENCHMARK_PAYLOAD_BYTES", "2048"))
READ_OWN_WRITES_SECONDS = float(os.environ.get("BENCHMARK_READ_OWN_WRITES", "5"))
CLIENT_LOG = os.environ.get("BENCHMARK_CLIENT_LOG")
# Random (incompressible) content, built once: every doc carries the same
# bytes, but the size is what drives serialization/oplog/replication cost.
PAYLOAD = os.urandom(PAYLOAD_BYTES // 2).hex()

# Adaptive-mode Feedback Loop tuning (production-scale defaults, not the
# short test-only values used in Phase 6's unit/integration tests).
WINDOW_SECONDS = 15
INTERVAL_SECONDS = 5
COOLDOWN_SECONDS = 10

_MIXED_BURSTY_PERIOD_SECONDS = 15  # how long each read-heavy/write-heavy sub-phase lasts

_SPIKE_PERIOD_SECONDS = 20   # how often a write spike starts
_SPIKE_ON_SECONDS = 3        # how long each write spike lasts

_HOTSPOT_FRACTION = 0.05    # fraction of KEY_SPACE treated as "hot"
_HOTSPOT_HIT_RATE = 0.80    # fraction of ops that land on the hot subset


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

    BALANCED/HOTSPOT/RAMP/HIGH_CONCURRENCY all use a flat "normal" ratio -
    each of those presets' actual signal comes from something other than
    the write mix (key distribution, load-shape params in run_benchmark.py),
    so they share the same probability here rather than inventing new ones.
    SPIKE is the one new preset whose signal lives in this function: a
    short, sharp write burst inside otherwise steady read-heavy traffic,
    distinct from MIXED_BURSTY's 50/50 square wave.
    """
    if preset in ("READ_HEAVY", "READ_HEAVY_HOTSPOT"):
        return 0.05
    if preset == "WRITE_HEAVY":
        return 0.95
    if preset == "MIXED_BURSTY":
        now = now if now is not None else time.time()
        phase = int(now // _MIXED_BURSTY_PERIOD_SECONDS) % 2
        return 0.95 if phase == 0 else 0.05
    if preset in ("BALANCED", "HOTSPOT", "RAMP", "HIGH_CONCURRENCY"):
        return 0.5
    if preset == "MODEST_WRITES":
        return 0.25  # between the low and high write-share thresholds: exercises MODERATE
    if preset == "SPIKE":
        now = now if now is not None else time.time()
        into_period = now % _SPIKE_PERIOD_SECONDS
        return 0.9 if into_period < _SPIKE_ON_SECONDS else 0.05
    raise ValueError(f"Unknown WORKLOAD_PRESET {preset!r}")


def pick_key(preset, key_space):
    """Choose a synthetic key for the next op, for the given preset.

    Every preset except HOTSPOT samples uniformly across the whole key
    space. HOTSPOT instead concentrates most ops on a small subset of keys
    (key contention / a "popular document" access pattern), which the
    read/write-ratio-only Decision Engine can't see directly but which
    still stresses replication/locking differently than spreading load
    evenly - worth comparing even though it isn't a Decision Engine input.
    READ_HEAVY_HOTSPOT uses the same key skew with READ_HEAVY's 5% writes: a
    read-heavy collection whose reads keep hitting the few documents that are
    being written. Per-key read-your-own-writes sends most of those reads to the primary.
    """
    if preset not in ("HOTSPOT", "READ_HEAVY_HOTSPOT"):
        return random.randrange(key_space)

    hot_size = max(1, int(key_space * _HOTSPOT_FRACTION))
    if random.random() < _HOTSPOT_HIT_RATE:
        return random.randrange(hot_size)
    return random.randrange(key_space)


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    monitor = AccessPatternMonitor(window_seconds=WINDOW_SECONDS)
    read_own_writes = READ_OWN_WRITES_SECONDS if (MODE == "adaptive_signals" and READ_OWN_WRITES_SECONDS > 0) else None
    client = AdaptiveClient(
        URI, DB_NAME, monitor=monitor, verbose=False, key_field="key", read_own_writes_seconds=read_own_writes,
    )
    client._db.drop_collection(COLLECTION)  # start each run from an empty collection
    # Without this, every read is a full collection scan over a collection that
    # grows for the whole run - measuring scan time, not consistency cost.
    client._db[COLLECTION].create_index("key")

    feedback_loop = None
    if MODE == "static_fast":
        client.set_active_settings(COLLECTION, FAST_SETTINGS)
    elif MODE == "static_moderate":
        client.set_active_settings(COLLECTION, MODERATE_SETTINGS)
    elif MODE == "static_safe":
        client.set_active_settings(COLLECTION, SAFE_SETTINGS)
    elif MODE == "adaptive_signals":
        feedback_loop = FeedbackLoop(
            client, monitor, interval_seconds=INTERVAL_SECONDS, cooldown_seconds=COOLDOWN_SECONDS
        )
        feedback_loop.start()
    else:
        raise ValueError(f"Unknown BENCHMARK_MODE {MODE!r}")

    environment.run_started_at = time.time()
    environment.probe_tally = ProbeTally()
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
        if SWITCH_LOG:
            write_switch_log(
                SWITCH_LOG, environment.shared_feedback_loop.switch_log,
                environment.run_started_at, time.time(),
            )
    if PROBE_LOG and getattr(environment, "probe_tally", None) is not None:
        environment.probe_tally.write(PROBE_LOG)
    client = getattr(environment, "shared_client", None)
    if CLIENT_LOG and client is not None:
        with open(CLIENT_LOG, "w", encoding="utf-8") as f:
            json.dump({"redirected_reads": client.redirected_reads,
                       "read_own_writes_seconds": client.read_own_writes_seconds}, f)
    if client is not None:
        client.close()
    print("[locustfile] test_stop: cleaned up shared client/feedback loop")


class AdaptiveMongoUser(User):
    """One simulated concurrent caller. Reuses the shared client/monitor
    set up in on_test_start rather than opening its own connection -
    correct because pymongo's MongoClient is meant to be shared/pooled,
    and required for the adaptive mode so every user observes and reacts to
    the same aggregate access pattern."""

    wait_time = constant_throughput(OPS_PER_SEC_PER_USER)

    @task
    def read_or_write(self):
        client = self.environment.shared_client
        key = pick_key(PRESET, KEY_SPACE)

        if PROBE_FRACTION and random.random() < PROBE_FRACTION:
            self._probe(client, key)
            return

        op_name = "write" if random.random() < current_write_probability(PRESET) else "read"
        start = time.monotonic()
        exc = None
        try:
            if op_name == "write":
                client.write(COLLECTION, {"key": key, "ts": time.time(), "payload": PAYLOAD})
            else:
                client.read(COLLECTION, {"key": key})
        except Exception as caught:  # noqa: BLE001 - Locust wants failures reported, not raised
            exc = caught
        self._report(op_name, start, exc)

    def _probe(self, client, key):
        """A write followed immediately by a read of that same document. A read
        that does not find it is a stale read, tallied under the settings in force."""
        start = time.monotonic()
        exc = inserted_id = None
        try:
            inserted_id = client.write(COLLECTION, {"key": key, "ts": time.time(), "payload": PAYLOAD}).inserted_id
        except Exception as caught:  # noqa: BLE001
            exc = caught
        self._report("write", start, exc)
        if exc is not None:
            return

        settings = settings_name(client.get_active_settings(COLLECTION))
        start = time.monotonic()
        exc = None
        found = False
        try:
            found = client.read(COLLECTION, {"_id": inserted_id}) is not None
        except Exception as caught:  # noqa: BLE001
            exc = caught
        self._report("probe_read", start, exc)
        if exc is None:
            self.environment.probe_tally.record(settings, fresh=found)

    def _report(self, name, start, exc):
        self.environment.events.request.fire(
            request_type="mongo",
            name=name,
            response_time=(time.monotonic() - start) * 1000,
            response_length=0,
            exception=exc,
            context={},
        )
