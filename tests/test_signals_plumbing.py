"""The plumbing behind the additions, without a database: the monitor's per-settings write
latency, the feedback loop's budget back-off / policy start-up / reasons, the client's per-key
read-your-own-writes memory and failed-operation logging, gevent-proof locks, and the
topology command plan."""

import pytest

from adaptive_layer import _locks
from adaptive_layer.client import AdaptiveClient
from adaptive_layer.decision import FAST_SETTINGS, MODERATE_SETTINGS, SAFE_SETTINGS
from adaptive_layer.feedback import FeedbackLoop
from adaptive_layer.monitor import AccessPatternMonitor, Stats
from adaptive_layer.rules import CollectionPolicy, SignalConfig
from benchmark import topology
from tests.test_feedback import FakeClient, FakeClock, FakeMonitor


# --- monitor: latency per settings name ------------------------------------------------------

def test_write_p95_is_per_settings_name():
    mon = AccessPatternMonitor(window_seconds=30)
    for i in range(20):
        mon.record("c", "write", 100.0 + i * 0.1, latency_ms=float(i + 1), tag="SAFE")
    mon.record("c", "write", 101.0, latency_ms=1.0, tag="MODERATE")
    s = mon.get_stats("c", now=103.0)
    assert s.write_p95("SAFE") == (19.0, 20)  # nearest rank: ceil(0.95 * 20) = 19th value
    assert s.write_p95("MODERATE") == (1.0, 1)
    assert s.write_p95("FAST") is None


def test_monitor_without_latency_gives_the_original_stats():
    mon = AccessPatternMonitor(window_seconds=30)
    mon.record("c", "write", 100.0)
    mon.record("c", "read", 100.5)
    s = mon.get_stats("c", now=101.0)
    assert (s.read_count, s.write_count, s.write_latency) == (1, 1, ())


def test_old_latency_samples_age_out_with_the_window():
    mon = AccessPatternMonitor(window_seconds=10)
    mon.record("c", "write", 100.0, latency_ms=500.0, tag="SAFE")
    mon.record("c", "write", 115.0, latency_ms=5.0, tag="SAFE")
    assert mon.get_stats("c", now=116.0).write_p95("SAFE") == (5.0, 1)


# --- feedback loop ---------------------------------------------------------------------------

def _stats(reads, writes, safe_p95=None, n=30):
    lat = (("SAFE", safe_p95, n),) if safe_p95 is not None else ()
    return Stats(reads, writes, 0.0, 0.0, None, lat)


def test_budget_breach_keeps_safe_off_for_the_backoff():
    clock, client, monitor = FakeClock(1000.0), FakeClient(), FakeMonitor()
    loop = FeedbackLoop(client, monitor, cooldown_seconds=0, clock=clock, signals=SignalConfig(budget_backoff_seconds=60))
    monitor.set_stats("c", _stats(40, 60))  # SAFE, no latency yet
    loop.tick()
    assert client.get_active_settings("c") == SAFE_SETTINGS

    clock.advance(5)
    monitor.set_stats("c", _stats(40, 60, safe_p95=262.0))
    loop.tick()
    assert client.get_active_settings("c") == MODERATE_SETTINGS
    assert "budget" in loop.switch_log[-1].reason

    clock.advance(30)  # SAFE samples have aged out, but the breach is remembered
    monitor.set_stats("c", _stats(40, 60))
    loop.tick()
    assert client.get_active_settings("c") == MODERATE_SETTINGS

    clock.advance(31)  # 61 s after the breach: SAFE may be tried again
    loop.tick()
    assert client.get_active_settings("c") == SAFE_SETTINGS


def test_too_few_safe_samples_do_not_trigger_the_budget():
    clock, client, monitor = FakeClock(1000.0), FakeClient(), FakeMonitor()
    loop = FeedbackLoop(client, monitor, cooldown_seconds=0, clock=clock, signals=SignalConfig())
    monitor.set_stats("c", _stats(40, 60, safe_p95=900.0, n=5))  # only 5 SAFE writes: not evidence
    loop.tick()
    assert client.get_active_settings("c") == SAFE_SETTINGS


def test_budget_respects_the_cooldown():
    clock, client, monitor = FakeClock(1000.0), FakeClient(), FakeMonitor()
    loop = FeedbackLoop(client, monitor, cooldown_seconds=10, clock=clock, signals=SignalConfig())
    monitor.set_stats("c", _stats(40, 60))
    loop.tick()  # -> SAFE
    clock.advance(5)
    monitor.set_stats("c", _stats(40, 60, safe_p95=262.0))
    loop.tick()
    assert client.get_active_settings("c") == SAFE_SETTINGS  # still cooling down
    clock.advance(6)
    loop.tick()
    assert client.get_active_settings("c") == MODERATE_SETTINGS


def test_policy_floor_is_applied_before_any_traffic():
    client = FakeClient()
    FeedbackLoop(client, FakeMonitor(), policies={"orders": CollectionPolicy(floor="SAFE"),
                                                  "logs": CollectionPolicy(ceiling="FAST")})
    assert client.get_active_settings("orders") == SAFE_SETTINGS
    assert client.get_active_settings("logs") == FAST_SETTINGS


def test_traffic_only_loop_still_logs_a_reason():
    clock, client, monitor = FakeClock(1000.0), FakeClient(), FakeMonitor()
    loop = FeedbackLoop(client, monitor, clock=clock)
    monitor.set_stats("c", _stats(40, 60))
    loop.tick()
    assert loop.switch_log[0].reason == "write share 0.60 > 0.40"
    assert loop.last_decisions["c"].settings == SAFE_SETTINGS


# --- client: per-key read-your-own-writes memory (no server needed) ------------------------

def test_client_remembers_written_keys_for_the_window(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("adaptive_layer.client.time.monotonic", lambda: now[0])
    c = AdaptiveClient("mongodb://localhost:1/?serverSelectionTimeoutMS=1", "x", verbose=False,
                       key_field="key", read_own_writes_seconds=5)
    try:
        c._remember("c", {"key": 7, "_id": "abc"})
        assert c._recently_written("c", {"key": 7})
        assert c._recently_written("c", {"_id": "abc"})
        assert not c._recently_written("c", {"key": 8})
        assert not c._recently_written("c", {"key": {"$in": [7]}})  # operators are not equality
        assert not c._recently_written("other", {"key": 7})
        now[0] = 105.5
        assert not c._recently_written("c", {"key": 7})
        c._remember("c", {"key": 9})
        assert len(c._recent["c"]) == 1  # old entries pruned on the next write
    finally:
        c.close()


def test_only_reads_that_would_leave_the_primary_are_redirected(monkeypatch):
    seen = []

    class Coll:
        def __init__(self, read_preference=None):
            self.rp = read_preference

        def find_one(self, query):
            seen.append(self.rp.mongos_mode if self.rp is not None else None)

    c = AdaptiveClient("mongodb://localhost:1/?serverSelectionTimeoutMS=1", "x", verbose=False,
                       key_field="key", read_own_writes_seconds=5)
    monkeypatch.setattr(c._db, "get_collection", lambda name, read_preference=None, **k: Coll(read_preference))
    try:
        c._remember("c", {"key": 7})
        c.set_active_settings("c", FAST_SETTINGS)
        c.read("c", {"key": 7})  # own recent write: primary
        c.read("c", {"key": 8})  # anything else: nearest
        c.set_active_settings("c", MODERATE_SETTINGS)
        c.read("c", {"key": 7})  # primaryPreferred reads the primary anyway: not counted
        assert seen == ["primary", "nearest", "primaryPreferred"]
        assert c.redirected_reads == 1
    finally:
        c.close()


def test_client_logs_a_failed_write_before_raising(monkeypatch):
    from pymongo.errors import AutoReconnect

    mon = AccessPatternMonitor(window_seconds=30)
    c = AdaptiveClient("mongodb://localhost:1/?serverSelectionTimeoutMS=1", "x", monitor=mon, verbose=False,
                       key_field="key", read_own_writes_seconds=5)

    class Coll:
        def insert_one(self, doc):
            raise AutoReconnect("primary stepped down")

    monkeypatch.setattr(c._db, "get_collection", lambda *a, **k: Coll())
    try:
        with pytest.raises(AutoReconnect):
            c.write("c", {"key": 1})
        assert mon.get_stats("c").write_count == 1  # still counted as a write
        assert not c._recently_written("c", {"key": 1})  # but not remembered: it may not exist
    finally:
        c.close()


# --- locks under gevent (2026-10-19: a gevent-patched lock hung a Locust run for 6 h) --------

def test_native_lock_is_a_plain_lock_without_gevent():
    lock = _locks.native_lock()
    with lock:
        assert lock.locked()
    assert not lock.locked()


# --- topology plan ---------------------------------------------------------------------------

IPS = {"mongo1": "172.18.0.2", "mongo2": "172.18.0.3", "mongo3": "172.18.0.4"}


def test_t1_delays_peers_and_both_directions_of_the_far_client_links():
    plan = topology.plan_commands(topology.T1, IPS, "172.18.0.1")
    m1 = "\n".join(plan["mongo1"])
    assert "netem delay 43ms" in m1 and "match ip dst 172.18.0.3/32" in m1  # peers: half of 86
    assert "match ip dst 172.18.0.1/32" in m1 and "netem delay 42.5ms" in m1  # replies to client
    assert "ifb0 root netem delay 42.5ms" in m1  # requests from client
    m2 = "\n".join(plan["mongo2"])
    assert "172.18.0.1" not in m2 and "ifb0" not in m2  # the client sits next to mongo2


def test_equal_topology_has_no_client_shaping_and_zero_means_nothing():
    plan = topology.plan_commands(topology.equal(5), IPS, "172.18.0.1")
    assert all("ifb0" not in "\n".join(c) for c in plan.values())
    assert "netem delay 5ms" in "\n".join(plan["mongo1"])
    assert topology.plan_commands(topology.equal(0), IPS, "172.18.0.1") == {n: [] for n in IPS}


def test_topologies_by_name():
    assert topology.by_name("T2") is topology.T2
    assert topology.by_name("EQUAL-5").rtt("mongo1", "mongo3") == 10.0
    with pytest.raises(ValueError):
        topology.by_name("T9")
