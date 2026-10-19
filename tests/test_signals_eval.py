"""signals_eval plumbing without a database: the plan, the time estimate, resume markers, and the
integrity / expectation checks on a synthetic result tree."""

import json

import pandas as pd
import pytest

from benchmark import signals_eval as ev


def test_default_plan_is_three_setups_without_5_or_43_ms():
    plan = ev.Plan()
    assert plan.setups == ["EQUAL-0", "T1", "T2"] and plan.staleness_setups == ["EQUAL-0", "T1", "T2"]
    assert plan.presets == ev.CORE_PRESETS and len(plan.presets) == 6
    assert plan.duration_scale == 0.6 and len(plan.cells()) == 3 * 5 * 6
    assert plan.params("READ_HEAVY")["duration"] == "180s" and plan.params("SPIKE")["duration"] == "108s"
    _, total = ev.estimate(plan)
    assert 4.3 * 3600 < total < 4.9 * 3600


def test_all_ten_presets_remain_available():
    from benchmark import run_benchmark as rb

    plan = ev.Plan(presets=list(rb.PRESETS))
    assert len(plan.cells()) == 150
    assert 7.3 * 3600 < ev.estimate(plan)[1] < 8.0 * 3600
    assert set(ev.CORE_PRESETS) <= set(rb.PRESETS)


def test_fixed_duration_overrides_and_bad_plans_are_rejected():
    assert ev.Plan(fixed_duration_s=30).params("HIGH_CONCURRENCY")["duration"] == "30s"
    with pytest.raises(ValueError):
        ev.Plan(setups=["T9"])
    with pytest.raises(ValueError):
        ev.Plan(configs=["static_turbo"])
    with pytest.raises(ValueError):
        ev.Plan(repeats=0)


def test_done_markers_make_runs_resumable(tmp_path):
    plan = ev.Plan(setups=["T1"], configs=["static_fast"], presets=["READ_HEAVY", "BALANCED"], run_checks=False,
                   staleness_setups=[])
    cell = plan.cells()[0]
    assert not ev.is_done(tmp_path, *cell)
    prefix = ev.cell_prefix(tmp_path, *cell)
    prefix.parent.mkdir(parents=True)
    ev.Path(f"{prefix}.done").write_text(json.dumps({"summary": {"failures": 0}}))
    assert ev.is_done(tmp_path, *cell)
    assert prefix.parts[-3:] == ("topology_T1", "rep1", "static_fast_READ_HEAVY")
    assert [c for c in plan.cells() if not ev.is_done(tmp_path, *c)] == [plan.cells()[1]]


def _df(rows):
    base = {"failures": 0, "probes": 100, "stale": 0, "stale_pct": 0.0, "read_p50": 2.0, "write_p95": 20.0, "reasons": "",
            "FAST_pct": 0, "MODERATE_pct": 100, "SAFE_pct": 0, "FAST_probes": 0, "FAST_stale": 0}
    return pd.DataFrame([{**base, **r} for r in rows])


MATCHING = [
    {"setup": "T1", "config": "static_fast", "preset": "READ_HEAVY", "stale": 27, "stale_pct": 27.0},
    {"setup": "T1", "config": "adaptive", "preset": "READ_HEAVY", "stale": 14, "stale_pct": 14.0, "FAST_pct": 91},
    {"setup": "T1", "config": "adaptive_signals", "preset": "READ_HEAVY", "FAST_pct": 91, "read_p50": 3.0, "FAST_probes": 90},
    {"setup": "T1", "config": "adaptive_signals", "preset": "BALANCED", "reasons": "traffic x1, budget+hysteresis x1"},
    {"setup": "T2", "config": "adaptive_signals", "preset": "BALANCED", "SAFE_pct": 91, "MODERATE_pct": 9, "reasons": "traffic x1"},
    {"setup": "T1", "config": "static_moderate", "preset": "READ_HEAVY", "read_p50": 88.0},
]


def test_expectations_on_a_result_that_matches_the_design():
    verdicts = {text: v for text, v, _ in ev.expectations(_df(MATCHING))}
    assert verdicts["adaptive_signals: no stale probe while in FAST, in any setup (#6)"] is True
    assert verdicts["T1 READ_HEAVY: adaptive_signals is less stale than adaptive and static_fast"] is True
    assert verdicts["T1 BALANCED: the latency budget takes adaptive_signals out of SAFE"] is True
    assert verdicts["T2 BALANCED: SAFE is within budget, adaptive_signals stays mostly SAFE"] is True
    assert verdicts["EQUAL-0ms BALANCED: SAFE is within budget, adaptive_signals stays mostly SAFE"] is None  # not in this run
    assert all(v is not False for v in verdicts.values())


def test_expectations_flag_a_result_that_contradicts_the_design():
    df = _df([
        {"setup": "T1", "config": "adaptive_signals", "preset": "READ_HEAVY", "FAST_pct": 91, "FAST_probes": 90, "FAST_stale": 3},
        {"setup": "T2", "config": "adaptive_signals", "preset": "BALANCED", "reasons": "budget+hysteresis x1", "SAFE_pct": 20},
        {"setup": "T1", "config": "static_safe", "preset": "BALANCED", "stale": 2},
    ])
    verdicts = {text: v for text, v, _ in ev.expectations(df)}
    assert verdicts["adaptive_signals: no stale probe while in FAST, in any setup (#6)"] is False
    assert verdicts["T2 BALANCED: SAFE is within budget, adaptive_signals stays mostly SAFE"] is False
    assert verdicts["static_moderate and static_safe are never stale"] is False


def test_integrity_reports_missing_runs_failures_and_checks(tmp_path):
    plan = ev.Plan(setups=["T1"], configs=["static_fast"], presets=["READ_HEAVY"], staleness_setups=["T1"])
    df = _df([{"setup": "T1", "config": "static_fast", "preset": "READ_HEAVY", "failures": 3}])
    rows = {name: ok for name, ok, _ in ev.integrity(tmp_path, plan, df, {"S1": {"pass": True}})}
    assert rows["every planned load-test run finished"] is False
    assert rows["no failed requests"] is False
    assert rows["live checks all PASS"] is False  # only 1 of 5 scenarios
    assert rows["staleness experiment done for every setup"] is False


# --- watchdog and retry (2026-10-19: one run never exited and blocked the evaluation for 6 h) -------

def test_watchdog_kills_a_hung_process_and_its_children():
    import sys
    import time

    from benchmark import run_benchmark as rb

    # a parent that starts a child, both sleeping far past the timeout (like the venv launcher + locust)
    code = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
    t0 = time.monotonic()
    assert rb._run_with_watchdog([sys.executable, "-c", code], None, timeout_s=2) is None
    assert time.monotonic() - t0 < 20
    done = rb._run_with_watchdog([sys.executable, "-c", "print('ok')"], None, timeout_s=20)
    assert done.returncode == 0 and done.stdout.strip() == "ok"


def _fake_env(monkeypatch, outcomes):
    """Replace everything that touches Docker or Locust; run_one returns `outcomes` in turn."""
    calls = []
    monkeypatch.setattr(ev.cluster, "pin_primary", lambda *a, **k: None)
    monkeypatch.setattr(ev.topology, "apply", lambda *a, **k: None)
    monkeypatch.setattr(ev.topology, "clear", lambda *a, **k: None)

    def run_one(config, preset, params, results_dir, timeout_s=None):
        calls.append((config, preset, timeout_s))
        return outcomes.pop(0)

    monkeypatch.setattr(ev.rb, "run_one", run_one)
    return calls


OK = {"requests": 10, "failures": 0, "p50_ms": 1.0}


def test_a_hung_run_is_retried_once_then_recorded(monkeypatch, tmp_path, capsys):
    plan = ev.Plan(setups=["T1"], configs=["static_fast"], presets=["READ_HEAVY", "BALANCED"], run_checks=False,
                   staleness_setups=[])
    calls = _fake_env(monkeypatch, [None, OK, None, None])  # READ_HEAVY: hang then ok; BALANCED: fails twice
    ev.run_load_tests(tmp_path, plan, ev.Log(tmp_path / "log.txt"))

    assert [c[:2] for c in calls] == [("static_fast", "READ_HEAVY")] * 2 + [("static_fast", "BALANCED")] * 2
    assert calls[0][2] == 180 + ev.WATCHDOG_GRACE_S  # the watchdog is set from the run length
    rh, bal = (json.loads(ev.Path(f"{ev.cell_prefix(tmp_path, *c)}.done").read_text()) for c in plan.cells())
    assert (rh["summary"], rh["attempts"]) == (OK, 2)
    assert (bal["summary"], bal["attempts"]) == (None, 2)
    rows = {n: ok for n, ok, _ in ev.integrity(tmp_path, plan, pd.DataFrame(), {})}
    assert rows["no Locust errors or hung runs"] is False and rows["no run needed a retry"] is False
