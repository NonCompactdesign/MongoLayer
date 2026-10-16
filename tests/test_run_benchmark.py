"""Runner plumbing, no database and no Locust: run_one / netem are replaced
with fakes so these check folder layout, parameters and cleanup only."""

import pytest

from benchmark import netem, run_benchmark as rb


def test_params_for_scales_duration_and_does_not_mutate_the_tables():
    assert rb.params_for("READ_HEAVY", quick=False)["duration"] == "300s"
    assert rb.params_for("READ_HEAVY", quick=False, duration_scale=0.4)["duration"] == "120s"
    assert rb.params_for("SPIKE", quick=False, duration_scale=0.4)["duration"] == "72s"
    assert rb.params_for("READ_HEAVY", quick=False, duration_scale=0.01)["duration"] == "30s"  # floor
    assert rb.FULL_DEFAULT["duration"] == "300s"


def test_results_dir_layout_is_per_latency_and_repeat_under_a_separate_root():
    d = rb.results_dir_for(43, rep=2)
    assert d == rb.RESULTS_DIR / "write_share_eval" / "latency_43ms" / "rep2"
    assert d != rb.results_dir_for(43, rep=1)


def test_default_sweep_is_three_latency_levels_and_modest_writes_is_a_preset():
    assert netem.LATENCY_LEVELS_MS == [0, 5, 43]
    assert "MODEST_WRITES" in rb.PRESETS and len(rb.PRESETS) == 9


def test_run_suite_runs_every_combination_and_always_clears_latency(monkeypatch, tmp_path):
    calls, events = [], []
    monkeypatch.setattr(rb, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(netem, "apply_latency", lambda ms: events.append(("apply", ms)))
    monkeypatch.setattr(netem, "clear", lambda: events.append(("clear",)))
    monkeypatch.setattr(rb, "run_one", lambda config, preset, params, results_dir: calls.append((config, preset, results_dir)) or {"failures": 0})

    results = rb.run_suite(5, quick=True, presets=["READ_HEAVY", "SPIKE"], rep=3)

    assert len(results) == 3 * 2
    assert {c[2] for c in calls} == {tmp_path / "latency_5ms" / "rep3"}
    assert (tmp_path / "latency_5ms" / "rep3").is_dir()
    assert events == [("apply", 5), ("clear",)]


def test_run_suite_clears_latency_even_if_a_run_raises(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(rb, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(netem, "apply_latency", lambda ms: events.append("apply"))
    monkeypatch.setattr(netem, "clear", lambda: events.append("clear"))

    def boom(*args, **kwargs):
        raise RuntimeError("locust blew up")

    monkeypatch.setattr(rb, "run_one", boom)
    with pytest.raises(RuntimeError):
        rb.run_suite(43, quick=True, presets=["READ_HEAVY"])
    assert events == ["apply", "clear"]


@pytest.mark.parametrize("config, expects_log", [("adaptive", True), ("static_fast", False), ("static_safe", False)])
def test_only_adaptive_runs_get_a_switch_log_path(monkeypatch, tmp_path, config, expects_log):
    seen = {}

    class Done:
        returncode = 0

    def fake_run(cmd, cwd, env, capture_output, text):
        seen.update(env)
        return Done()

    monkeypatch.setattr(rb.subprocess, "run", fake_run)
    monkeypatch.setattr(rb, "_summarize_stats_csv", lambda prefix: None)

    rb.run_one(config, "READ_HEAVY", {"users": 1, "spawn_rate": 1, "duration": "30s"}, tmp_path)

    assert ("BENCHMARK_SWITCH_LOG" in seen) is expects_log
    if expects_log:
        assert seen["BENCHMARK_SWITCH_LOG"].endswith("adaptive_READ_HEAVY_switches.csv")
    # every run, adaptive or not, writes its staleness-probe tally
    assert seen["BENCHMARK_PROBE_LOG"].endswith(f"{config}_READ_HEAVY_probe.csv")
