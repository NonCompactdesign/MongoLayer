"""Unit tests for AccessPatternMonitor. No database, no real sleep() - all
timestamps are synthetic floats, which is the entire point of keeping the
Monitor decoupled from MongoDB (see the module docstring in monitor.py)."""

import math

import pytest

from adaptive_layer.monitor import NEUTRAL_STATS, AccessPatternMonitor


def test_unknown_collection_returns_neutral_stats():
    mon = AccessPatternMonitor(window_seconds=30)
    assert mon.get_stats("never_seen") == NEUTRAL_STATS


def test_basic_counts_and_ratio():
    mon = AccessPatternMonitor(window_seconds=30)
    t0 = 1000.0
    mon.record("orders", "read", t0)
    mon.record("orders", "read", t0 + 1)
    mon.record("orders", "read", t0 + 2)
    mon.record("orders", "write", t0 + 3)

    stats = mon.get_stats("orders", now=t0 + 4)
    assert stats.read_count == 3
    assert stats.write_count == 1
    assert stats.read_write_ratio == 3.0
    assert stats.write_freq == pytest.approx(1 / 30)
    assert stats.seconds_since_last_write == pytest.approx(1.0)


def test_read_only_ratio_is_infinite():
    mon = AccessPatternMonitor(window_seconds=30)
    t0 = 1000.0
    mon.record("catalog", "read", t0)
    stats = mon.get_stats("catalog", now=t0 + 1)
    assert stats.write_count == 0
    assert math.isinf(stats.read_write_ratio)


def test_no_writes_yet_means_seconds_since_last_write_is_none():
    mon = AccessPatternMonitor(window_seconds=30)
    mon.record("catalog", "read", 1000.0)
    stats = mon.get_stats("catalog", now=1001.0)
    assert stats.seconds_since_last_write is None


def test_pruning_drops_events_older_than_window():
    mon = AccessPatternMonitor(window_seconds=30)
    t0 = 1000.0
    mon.record("sessions", "write", t0)  # will age out
    mon.record("sessions", "write", t0 + 40)  # still fresh at t0+45

    stats = mon.get_stats("sessions", now=t0 + 45)
    # only the second write should still be inside the 30s window
    assert stats.write_count == 1
    assert stats.seconds_since_last_write == pytest.approx(5.0)


def test_pruning_can_empty_a_collection_back_to_neutral():
    mon = AccessPatternMonitor(window_seconds=10)
    t0 = 1000.0
    mon.record("sessions", "write", t0)

    # far enough in the future that the one event has aged out entirely
    stats = mon.get_stats("sessions", now=t0 + 100)
    assert stats == NEUTRAL_STATS


def test_record_prunes_immediately_not_just_on_get_stats():
    """Memory shouldn't grow unbounded just because get_stats() is never
    called - record() itself should prune too."""
    mon = AccessPatternMonitor(window_seconds=5)
    t0 = 1000.0
    mon.record("orders", "write", t0)
    mon.record("orders", "write", t0 + 100)  # far past the window

    # internal deque should have dropped the first event already
    assert len(mon._events["orders"]) == 1


def test_invalid_op_type_raises():
    mon = AccessPatternMonitor()
    with pytest.raises(ValueError):
        mon.record("orders", "delete", 1000.0)


def test_zero_or_negative_window_rejected():
    with pytest.raises(ValueError):
        AccessPatternMonitor(window_seconds=0)
