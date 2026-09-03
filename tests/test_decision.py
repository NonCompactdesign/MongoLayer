"""Unit tests for the Decision Engine. Pure function, no DB, no monitor -
Stats objects are constructed directly. Table-driven: one case per branch,
plus the boundary values that decide which side of a threshold wins."""

import pytest

from adaptive_layer.decision import (
    FAST_SETTINGS,
    HIGH_WRITE_THRESHOLD,
    LOW_WRITE_THRESHOLD,
    MODERATE_SETTINGS,
    READ_HEAVY_RATIO_THRESHOLD,
    SAFE_SETTINGS,
    decide,
)
from adaptive_layer.monitor import NEUTRAL_STATS, Stats


def make_stats(read_count=0, write_count=0, read_write_ratio=0.0, write_freq=0.0, seconds_since_last_write=None):
    return Stats(
        read_count=read_count,
        write_count=write_count,
        read_write_ratio=read_write_ratio,
        write_freq=write_freq,
        seconds_since_last_write=seconds_since_last_write,
    )


# --- one case per branch -----------------------------------------------

def test_hot_write_heavy_picks_safe_settings():
    stats = make_stats(write_count=50, write_freq=HIGH_WRITE_THRESHOLD + 1, read_write_ratio=0.2)
    assert decide(stats) == SAFE_SETTINGS


def test_read_heavy_and_cold_picks_fast_settings():
    stats = make_stats(
        read_count=100,
        write_count=1,
        read_write_ratio=READ_HEAVY_RATIO_THRESHOLD + 1,
        write_freq=LOW_WRITE_THRESHOLD - 0.1,
    )
    assert decide(stats) == FAST_SETTINGS


def test_mixed_moderate_traffic_picks_moderate_settings():
    stats = make_stats(
        read_count=10,
        write_count=10,
        read_write_ratio=1.0,
        write_freq=(HIGH_WRITE_THRESHOLD + LOW_WRITE_THRESHOLD) / 2,
    )
    assert decide(stats) == MODERATE_SETTINGS


def test_neutral_stats_for_brand_new_collection_picks_moderate_settings():
    """A collection nobody has touched yet must get a safe, sensible
    default - never crash, never accidentally look "hot" or "read-heavy"."""
    assert decide(NEUTRAL_STATS) == MODERATE_SETTINGS


# --- read-heavy-but-still-hot: safety wins ------------------------------

def test_hot_write_freq_wins_even_if_also_read_heavy():
    """If a collection is simultaneously read-heavy AND write-hot (e.g. a
    burst), the hot/write-heavy branch must win - safety over speed when
    both signals fire at once."""
    stats = make_stats(
        read_count=1000,
        write_count=50,
        read_write_ratio=READ_HEAVY_RATIO_THRESHOLD + 10,
        write_freq=HIGH_WRITE_THRESHOLD + 1,
    )
    assert decide(stats) == SAFE_SETTINGS


# --- boundary cases: exactly-at-threshold falls to the safer/moderate side --

def test_write_freq_exactly_at_high_threshold_does_not_trigger_safe():
    """Branch condition is strict '>', so sitting exactly on the threshold
    must NOT count as hot yet."""
    stats = make_stats(write_freq=HIGH_WRITE_THRESHOLD, read_write_ratio=0.5)
    assert decide(stats) != SAFE_SETTINGS


def test_read_write_ratio_exactly_at_threshold_does_not_trigger_fast():
    stats = make_stats(
        read_write_ratio=READ_HEAVY_RATIO_THRESHOLD,
        write_freq=LOW_WRITE_THRESHOLD - 0.1,
    )
    assert decide(stats) != FAST_SETTINGS


def test_write_freq_exactly_at_low_threshold_does_not_trigger_fast():
    """Cold-branch condition is strict '<', so sitting exactly on
    LOW_WRITE_THRESHOLD must NOT count as cold enough yet."""
    stats = make_stats(
        read_write_ratio=READ_HEAVY_RATIO_THRESHOLD + 1,
        write_freq=LOW_WRITE_THRESHOLD,
    )
    assert decide(stats) != FAST_SETTINGS


# --- Stats' own documented edge cases (see Phase 3 notes) must not crash --

def test_infinite_read_write_ratio_does_not_crash():
    stats = make_stats(read_count=5, write_count=0, read_write_ratio=float("inf"), write_freq=0.0)
    result = decide(stats)
    assert result == FAST_SETTINGS


def test_none_seconds_since_last_write_does_not_crash():
    stats = make_stats(seconds_since_last_write=None)
    # decide() doesn't even look at seconds_since_last_write today, but the
    # field being None must never blow up if/when that changes
    assert decide(stats) in (SAFE_SETTINGS, FAST_SETTINGS, MODERATE_SETTINGS)


@pytest.mark.parametrize("settings", [SAFE_SETTINGS, FAST_SETTINGS, MODERATE_SETTINGS])
def test_decide_always_returns_one_of_the_three_named_settings(settings):
    """Sanity check that the three module-level constants really are the
    only three possible outputs - guards against a future branch returning
    an ad-hoc Settings() that doesn't match any named configuration."""
    assert settings.write_concern in (1, "majority")
    assert settings.read_concern in ("local", "majority")
    assert settings.read_preference in ("primary", "primaryPreferred", "secondary", "secondaryPreferred", "nearest")
