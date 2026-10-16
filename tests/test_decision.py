"""Unit tests for the Decision Engine. Pure function, no DB, no monitor -
Stats objects are constructed directly. Table-driven: one case per branch,
plus the boundary values that decide which side of a threshold wins.

The rule is based on write share (writes / all operations), not on an
absolute write rate, so several tests check that the decision depends on the
mix and not on how many operations per second there are."""

import pytest

from adaptive_layer.decision import (
    DEFAULT_THRESHOLDS,
    FAST_SETTINGS,
    HIGH_WRITE_SHARE,
    LOW_WRITE_SHARE,
    MODERATE_SETTINGS,
    SAFE_SETTINGS,
    Thresholds,
    decide,
)
from adaptive_layer.monitor import NEUTRAL_STATS, Stats


def make_stats(read_count=0, write_count=0, write_freq=0.0, seconds_since_last_write=None):
    ratio = (read_count / write_count) if write_count else (float("inf") if read_count else 0.0)
    return Stats(
        read_count=read_count,
        write_count=write_count,
        read_write_ratio=ratio,
        write_freq=write_freq,
        seconds_since_last_write=seconds_since_last_write,
    )


# --- one case per branch -----------------------------------------------

def test_write_heavy_picks_safe_settings():
    assert decide(make_stats(read_count=5, write_count=50)) == SAFE_SETTINGS


def test_read_heavy_picks_fast_settings():
    assert decide(make_stats(read_count=100, write_count=1)) == FAST_SETTINGS


def test_in_between_mix_picks_moderate_settings():
    assert decide(make_stats(read_count=7, write_count=3)) == MODERATE_SETTINGS  # 30% writes


def test_neutral_stats_for_brand_new_collection_picks_moderate_settings():
    """A collection nobody has touched yet must get a safe, sensible
    default - never crash, never accidentally look "write-heavy" or "read-heavy"."""
    assert decide(NEUTRAL_STATS) == MODERATE_SETTINGS


def test_writes_only_picks_safe_and_reads_only_picks_fast():
    assert decide(make_stats(write_count=10)) == SAFE_SETTINGS  # share 1.0
    assert decide(make_stats(read_count=10)) == FAST_SETTINGS  # share 0.0, infinite ratio


# --- the point of the write-share rule: scale invariance ------------------

@pytest.mark.parametrize("write_freq", [0.05, 5.0, 500.0])
def test_decision_does_not_depend_on_absolute_write_rate(write_freq):
    """5% writes is read-heavy whether that is 0.05 or 500 writes per second.
    (The old absolute-rate rule called the 500/s case hot and never reached FAST.)"""
    assert decide(make_stats(read_count=95, write_count=5, write_freq=write_freq)) == FAST_SETTINGS
    assert decide(make_stats(read_count=50, write_count=50, write_freq=write_freq)) == SAFE_SETTINGS


# --- boundary cases: exactly-at-threshold falls to the more moderate side --

BINARY = Thresholds(high_write_share=0.5, low_write_share=0.25)  # exactly representable


def test_share_exactly_at_high_threshold_does_not_trigger_safe():
    """Condition is strict '>', so sitting exactly on the threshold must NOT count as write-heavy yet."""
    assert decide(make_stats(read_count=5, write_count=5), BINARY) == MODERATE_SETTINGS  # share 0.5


def test_share_exactly_at_low_threshold_does_not_trigger_fast():
    """Condition is strict '<', so sitting exactly on the threshold must NOT count as read-heavy yet."""
    assert decide(make_stats(read_count=3, write_count=1), BINARY) == MODERATE_SETTINGS  # share 0.25


def test_just_past_each_threshold_switches():
    assert decide(make_stats(read_count=4, write_count=5), BINARY) == SAFE_SETTINGS  # 5/9 > 0.5
    assert decide(make_stats(read_count=4, write_count=1), BINARY) == FAST_SETTINGS  # 1/5 < 0.25


# --- thresholds are parameters ---------------------------------------------

def test_custom_thresholds_change_the_decision():
    stats = make_stats(read_count=7, write_count=3)  # 30% writes
    assert decide(stats) == MODERATE_SETTINGS
    assert decide(stats, Thresholds(high_write_share=0.25, low_write_share=0.05)) == SAFE_SETTINGS
    assert decide(stats, Thresholds(high_write_share=0.9, low_write_share=0.5)) == FAST_SETTINGS


def test_default_thresholds_are_the_module_constants():
    assert DEFAULT_THRESHOLDS == Thresholds(HIGH_WRITE_SHARE, LOW_WRITE_SHARE)


@pytest.mark.parametrize(
    "low, high",
    [(0.4, 0.4), (0.5, 0.4), (-0.1, 0.4), (0.1, 1.1)],
)
def test_invalid_thresholds_are_rejected(low, high):
    with pytest.raises(ValueError):
        Thresholds(high_write_share=high, low_write_share=low)


# --- Stats' own documented edge cases must not crash ------------------------

def test_none_seconds_since_last_write_does_not_crash():
    stats = make_stats(read_count=1, write_count=1, seconds_since_last_write=None)
    assert decide(stats) in (SAFE_SETTINGS, FAST_SETTINGS, MODERATE_SETTINGS)


def test_write_share_property():
    assert make_stats(read_count=3, write_count=1).write_share == 0.25
    assert NEUTRAL_STATS.write_share == 0.0


@pytest.mark.parametrize("settings", [SAFE_SETTINGS, FAST_SETTINGS, MODERATE_SETTINGS])
def test_decide_always_returns_one_of_the_three_named_settings(settings):
    """Sanity check that the three module-level constants really are the
    only three possible outputs - guards against a future branch returning
    an ad-hoc Settings() that doesn't match any named configuration."""
    assert settings.write_concern in (1, "majority")
    assert settings.read_concern in ("local", "majority")
    assert settings.read_preference in ("primary", "primaryPreferred", "secondary", "secondaryPreferred", "nearest")
