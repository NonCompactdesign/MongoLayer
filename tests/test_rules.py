"""Unit tests for the rules on top of the traffic rule (adaptive_layer/rules.py): hysteresis (8),
the SAFE latency budget (9), per-collection policy (5) and the reasons (10). Pure: Stats are
built by hand, no database."""

import pytest

from adaptive_layer.decision import (
    FAST_SETTINGS,
    MODERATE_SETTINGS,
    SAFE_SETTINGS,
    Thresholds,
    decide,
    decide_explained,
)
from adaptive_layer.monitor import Stats
from adaptive_layer.rules import SIGNALS_THRESHOLDS, CollectionPolicy, SignalConfig, apply_rules


def stats(reads, writes):
    return Stats(read_count=reads, write_count=writes, read_write_ratio=0.0, write_freq=0.0,
                 seconds_since_last_write=None)


READ_HEAVY = stats(95, 5)  # 5% writes -> FAST
WRITE_HEAVY = stats(40, 60)  # 60% writes -> SAFE
CFG = SignalConfig()


# --- 10 reasons ---------------------------------------------------------------------------------

def test_reasons_name_the_share_and_the_threshold():
    assert decide_explained(WRITE_HEAVY) == (SAFE_SETTINGS, "write share 0.60 > 0.40")
    assert decide_explained(READ_HEAVY) == (FAST_SETTINGS, "write share 0.05 < 0.15")
    assert decide_explained(stats(75, 25))[1] == "write share 0.25 between 0.15 and 0.40"
    assert decide_explained(stats(0, 0))[1] == "no operations in the window"


# --- 8 hysteresis -------------------------------------------------------------------------------

def test_hysteresis_keeps_safe_until_the_share_drops_below_high_minus_h():
    h = SIGNALS_THRESHOLDS  # 0.40 / 0.15, hysteresis 0.05
    assert decide(stats(63, 37), h, current=SAFE_SETTINGS) == SAFE_SETTINGS  # 0.37 > 0.35: stay
    assert decide(stats(63, 37), h, current=MODERATE_SETTINGS) == MODERATE_SETTINGS  # but not enter
    assert decide(stats(66, 34), h, current=SAFE_SETTINGS) == MODERATE_SETTINGS  # 0.34: leave


def test_hysteresis_keeps_fast_until_the_share_rises_above_low_plus_h():
    h = SIGNALS_THRESHOLDS
    assert decide(stats(82, 18), h, current=FAST_SETTINGS) == FAST_SETTINGS  # 0.18 < 0.20
    assert decide(stats(82, 18), h, current=MODERATE_SETTINGS) == MODERATE_SETTINGS
    assert decide(stats(79, 21), h, current=FAST_SETTINGS) == MODERATE_SETTINGS
    assert "hysteresis" in decide_explained(stats(82, 18), h, FAST_SETTINGS)[1]


def test_zero_hysteresis_ignores_current_settings():
    assert decide(stats(63, 37), Thresholds(), current=SAFE_SETTINGS) == MODERATE_SETTINGS


@pytest.mark.parametrize("h", [-0.01, 0.125, 0.3])
def test_hysteresis_bands_must_not_overlap(h):
    with pytest.raises(ValueError):
        Thresholds(hysteresis=h)  # (0.40 - 0.15) / 2 = 0.125


# --- 9 latency budget ---------------------------------------------------------------------------

def test_safe_over_budget_steps_down():
    d = apply_rules(WRITE_HEAVY, config=CFG, safe_write_p95_ms=262.0)
    assert d.settings == MODERATE_SETTINGS
    assert d.reasons == ("write share 0.60 > 0.40", "SAFE write p95 262 ms > budget 100 ms -> MODERATE")


def test_safe_within_budget_or_unmeasured_stays():
    assert apply_rules(WRITE_HEAVY, config=CFG, safe_write_p95_ms=40.0).settings == SAFE_SETTINGS
    assert apply_rules(WRITE_HEAVY, config=CFG, safe_write_p95_ms=None).settings == SAFE_SETTINGS
    assert apply_rules(WRITE_HEAVY, config=SignalConfig(write_p95_budget_ms=None), safe_write_p95_ms=900).settings == SAFE_SETTINGS


def test_budget_never_touches_fast_or_moderate():
    assert apply_rules(READ_HEAVY, config=CFG, safe_write_p95_ms=900.0).settings == FAST_SETTINGS
    assert apply_rules(stats(75, 25), config=CFG, safe_write_p95_ms=900.0).settings == MODERATE_SETTINGS


# --- 5 policy -----------------------------------------------------------------------------------

def test_floor_overrides_the_budget():
    d = apply_rules(WRITE_HEAVY, config=CFG, safe_write_p95_ms=500.0, policy=CollectionPolicy(floor="SAFE"))
    assert d.settings == SAFE_SETTINGS
    assert d.reasons[-1] == "policy floor SAFE -> SAFE"


def test_floor_raises_fast_and_ceiling_lowers_safe():
    assert apply_rules(READ_HEAVY, policy=CollectionPolicy(floor="MODERATE")).settings == MODERATE_SETTINGS
    assert apply_rules(WRITE_HEAVY, policy=CollectionPolicy(ceiling="MODERATE")).settings == MODERATE_SETTINGS
    assert apply_rules(READ_HEAVY, policy=CollectionPolicy(ceiling="MODERATE")).settings == FAST_SETTINGS


@pytest.mark.parametrize("kwargs", [{"floor": "SAFE", "ceiling": "FAST"}, {"floor": "STRICT"}])
def test_invalid_policies_are_rejected(kwargs):
    with pytest.raises(ValueError):
        CollectionPolicy(**kwargs)


def test_no_config_means_traffic_rule_only():
    assert apply_rules(WRITE_HEAVY, safe_write_p95_ms=900.0).settings == SAFE_SETTINGS
