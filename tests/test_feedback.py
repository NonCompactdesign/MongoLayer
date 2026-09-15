"""Unit tests for FeedbackLoop.tick(). Fake client + fake monitor, fake
controllable clock, no real database and no real sleeping - these test the
orchestration logic (when does a switch happen vs. get suppressed), not
threading or real MongoDB behavior. See test_feedback_integration.py for
the real-replica-set, real-threaded end-to-end version.
"""

import pytest

from adaptive_layer.decision import (
    FAST_SETTINGS,
    HIGH_WRITE_THRESHOLD,
    LOW_WRITE_THRESHOLD,
    MODERATE_SETTINGS,
    READ_HEAVY_RATIO_THRESHOLD,
    SAFE_SETTINGS,
)
from adaptive_layer.feedback import FeedbackLoop
from adaptive_layer.monitor import NEUTRAL_STATS, Stats


class FakeClock:
    """A controllable clock: starts at 0, advances only when told to."""

    def __init__(self, start=0.0):
        self._now = start

    def __call__(self):
        return self._now

    def advance(self, seconds):
        self._now += seconds


class FakeMonitor:
    """Stats are set directly per collection rather than derived from
    recorded events - FeedbackLoop doesn't care how a Monitor computes
    stats, only that it can report known_collections() and get_stats()."""

    def __init__(self):
        self._stats = {}

    def set_stats(self, collection, stats):
        self._stats[collection] = stats

    def known_collections(self):
        return list(self._stats.keys())

    def get_stats(self, collection, now=None):
        return self._stats.get(collection, NEUTRAL_STATS)


class FakeClient:
    def __init__(self):
        self._active = {}
        self.set_calls = []  # [(collection, settings), ...] in call order

    def get_active_settings(self, collection):
        return self._active.get(collection, MODERATE_SETTINGS)

    def set_active_settings(self, collection, settings):
        self._active[collection] = settings
        self.set_calls.append((collection, settings))


def hot_stats():
    return Stats(
        read_count=5,
        write_count=50,
        read_write_ratio=0.1,
        write_freq=HIGH_WRITE_THRESHOLD + 1,
        seconds_since_last_write=0.1,
    )


def cold_read_heavy_stats():
    return Stats(
        read_count=100,
        write_count=0,
        read_write_ratio=float("inf"),
        write_freq=LOW_WRITE_THRESHOLD - 0.1,
        seconds_since_last_write=None,
    )


@pytest.fixture
def setup():
    clock = FakeClock(start=1000.0)
    client = FakeClient()
    monitor = FakeMonitor()
    loop = FeedbackLoop(client, monitor, interval_seconds=10, cooldown_seconds=30, clock=clock)
    return clock, client, monitor, loop


def test_tick_applies_first_ever_decision_with_no_cooldown_check(setup):
    """A collection with no prior switch history should switch immediately -
    cooldown only matters once there's been a previous switch to cool down from."""
    clock, client, monitor, loop = setup
    monitor.set_stats("orders", hot_stats())

    loop.tick()

    assert client.get_active_settings("orders") == SAFE_SETTINGS
    assert client.set_calls == [("orders", SAFE_SETTINGS)]


def test_tick_suppresses_switch_within_cooldown(setup):
    clock, client, monitor, loop = setup
    monitor.set_stats("orders", hot_stats())
    loop.tick()  # first switch: MODERATE -> SAFE, recorded at t=1000

    clock.advance(10)  # cooldown is 30s, only 10s have passed
    monitor.set_stats("orders", cold_read_heavy_stats())  # would want to switch to FAST
    loop.tick()

    # still SAFE - the would-be switch to FAST was suppressed by cooldown
    assert client.get_active_settings("orders") == SAFE_SETTINGS
    assert len(client.set_calls) == 1


def test_tick_applies_switch_once_cooldown_elapses(setup):
    clock, client, monitor, loop = setup
    monitor.set_stats("orders", hot_stats())
    loop.tick()  # switch #1 at t=1000

    clock.advance(31)  # cooldown is 30s, 31s have now passed
    monitor.set_stats("orders", cold_read_heavy_stats())
    loop.tick()

    assert client.get_active_settings("orders") == FAST_SETTINGS
    assert [s for _, s in client.set_calls] == [SAFE_SETTINGS, FAST_SETTINGS]


def test_tick_no_change_needed_does_not_touch_cooldown_or_call_set(setup):
    """If decide() agrees with what's already active, tick() shouldn't call
    set_active_settings at all - and critically, shouldn't reset any
    cooldown timer either, since nothing actually changed."""
    clock, client, monitor, loop = setup
    monitor.set_stats("orders", hot_stats())
    loop.tick()  # switches to SAFE at t=1000
    assert len(client.set_calls) == 1

    clock.advance(5)
    monitor.set_stats("orders", hot_stats())  # still hot -> decide() still says SAFE
    loop.tick()

    # decide() == current, so no new set_active_settings call at all
    assert len(client.set_calls) == 1


def test_tick_evaluates_multiple_collections_independently(setup):
    clock, client, monitor, loop = setup
    monitor.set_stats("orders", hot_stats())
    monitor.set_stats("catalog", cold_read_heavy_stats())

    loop.tick()

    assert client.get_active_settings("orders") == SAFE_SETTINGS
    assert client.get_active_settings("catalog") == FAST_SETTINGS
    assert len(client.set_calls) == 2


def test_tick_with_no_known_collections_does_nothing(setup):
    clock, client, monitor, loop = setup
    loop.tick()
    assert client.set_calls == []


def test_tick_moderate_stats_never_triggers_a_switch_from_default(setup):
    """MODERATE_SETTINGS is the client's own default for an unconfigured
    collection, so a moderate decision should produce zero set_active_settings
    calls - there's nothing to change."""
    clock, client, monitor, loop = setup
    from adaptive_layer.monitor import Stats

    monitor.set_stats(
        "misc",
        Stats(read_count=5, write_count=5, read_write_ratio=1.0, write_freq=1.0, seconds_since_last_write=2.0),
    )
    loop.tick()
    assert client.set_calls == []
