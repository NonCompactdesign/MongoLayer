"""Analysis pipeline on a synthetic results tree with known answers: no
database, no Locust. Checks aggregation across repeats, pooled staleness,
time-in-settings and that every output file is produced."""

import csv
import math

import pytest

from benchmark import analyze_results as ar

STATS_HEADER = ["Type", "Name", "Request Count", "Failure Count", "Average Response Time", "Requests/s", "50%", "95%", "99%"]


def _write_stats(path, p50, p95, p99, requests=1000, failures=0, rps=50.0):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(STATS_HEADER)
        w.writerow(["", "Aggregated", requests, failures, p50, rps, p50, p95, p99])


def _write_staleness(path, fast_misses, trials=100):
    rows = [("static_fast", fast_misses), ("static_safe", 0), ("adaptive_hot", 0), ("adaptive_cold", 1)]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["config", "trials", "hits", "misses", "stale_rate", "in_expected_settings"])
        for name, misses in rows:
            w.writerow([name, trials, trials - misses, misses, misses / trials, trials])


def _write_switches(path, fast_s, safe_s):
    total = fast_s + safe_s
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["event", "seconds", "settings", "write_share", "reads", "writes"])
        w.writerow(["start", 0.0, "MODERATE", "", "", ""])
        w.writerow(["switch", 0.0, "FAST", 0.05, 95, 5])
        w.writerow(["switch", fast_s, "SAFE", 0.9, 10, 90])
        w.writerow(["stop", total, "SAFE", "", "", ""])


def _write_probe(path, config, rep):
    """static_fast: 100 FAST probes per repeat, 20 stale in rep 1 and 40 in rep 2.
    adaptive: 60 FAST probes (30 stale) and 40 SAFE probes (0 stale). static_safe: 100 SAFE, 0 stale."""
    rows = {"static_fast": [("FAST", 100, 20 * rep)], "static_safe": [("SAFE", 100, 0)],
            "adaptive": [("FAST", 60, 30), ("SAFE", 40, 0)]}[config]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["settings", "probes", "stale"])
        w.writerows(rows)


@pytest.fixture
def tree(tmp_path):
    """2 latency levels x 2 repeats x 3 configs x 2 presets. p50 for static_fast
    is 10 in rep1 and 20 in rep2 (mean 15) at 0 ms; everything else is 30 / 60 / 90."""
    for latency in (0, 43):
        for rep in (1, 2):
            d = tmp_path / f"latency_{latency}ms" / f"rep{rep}"
            d.mkdir(parents=True)
            for config in ar.CONFIGS:
                for preset in ("READ_HEAVY", "WRITE_HEAVY"):
                    p50 = (10 if rep == 1 else 20) if (config == "static_fast" and latency == 0) else 30
                    _write_stats(d / f"{config}_{preset}_stats.csv", p50, 60, 90)
            for preset in ("READ_HEAVY", "WRITE_HEAVY"):
                _write_switches(d / f"adaptive_{preset}_switches.csv", fast_s=30 * rep, safe_s=70 - 10 * rep)
            _write_staleness(d / "staleness.csv", fast_misses=10 if rep == 1 else 30)
            for config in ar.CONFIGS:
                for preset in ("READ_HEAVY", "WRITE_HEAVY"):
                    _write_probe(d / f"{config}_{preset}_probe.csv", config, rep)
    return tmp_path


def test_rep_dirs_finds_every_latency_and_repeat(tree):
    assert [(lat, rep) for lat, rep, _ in ar.rep_dirs(tree)] == [(0, 1), (0, 2), (43, 1), (43, 2)]


def test_aggregate_benchmark_gives_mean_and_sample_std_over_repeats(tree):
    bench, _ = ar.load_all(tree)
    agg = ar.aggregate_benchmark(bench)
    row = agg[(agg.latency_ms == 0) & (agg.config == "static_fast") & (agg.preset == "READ_HEAVY")].iloc[0]
    assert row.p50_ms == 15.0
    assert row.p50_ms_std == pytest.approx(math.sqrt(50))  # std of [10, 20], ddof=1
    assert row.n_reps == 2
    flat = agg[(agg.latency_ms == 43) & (agg.config == "static_safe")].iloc[0]
    assert flat.p50_ms_std == 0.0


def test_single_repeat_has_nan_std_not_a_crash(tmp_path):
    d = tmp_path / "latency_0ms" / "rep1"
    d.mkdir(parents=True)
    _write_stats(d / "static_fast_READ_HEAVY_stats.csv", 10, 20, 30)
    agg = ar.aggregate_benchmark(ar.load_all(tmp_path)[0])
    assert math.isnan(agg.iloc[0].p50_ms_std)


def test_pool_staleness_pools_trials_and_reports_spread_of_repeat_rates(tree):
    _, stale = ar.load_all(tree)
    pooled = ar.pool_staleness(stale)
    fast = pooled[(pooled.latency_ms == 0) & (pooled.config == "static_fast")].iloc[0]
    assert (fast.trials, fast.misses) == (200, 40)
    assert fast.stale_rate == pytest.approx(0.20)
    assert fast.rep_std == pytest.approx(math.sqrt(0.02))  # std of [0.1, 0.3]
    assert fast.in_expected_settings == 200


def test_switch_timelines_give_share_of_time_per_setting(tree):
    tl = ar.load_switch_timelines(tree)
    # rep 1: 30 s FAST then 60 s SAFE = 90 s run; rep 2: 60 s FAST then 50 s SAFE = 110 s run
    r1 = tl[(tl.latency_ms == 0) & (tl.rep == 1) & (tl.preset == "READ_HEAVY")].iloc[0]
    assert r1.FAST_pct == pytest.approx(100 * 30 / 90)
    assert r1.SAFE_pct == pytest.approx(100 * 60 / 90)
    assert r1.MODERATE_pct == 0.0 and r1.switches == 2
    r2 = tl[(tl.latency_ms == 0) & (tl.rep == 2) & (tl.preset == "READ_HEAVY")].iloc[0]
    assert r2.FAST_pct == pytest.approx(100 * 60 / 110)
    for row in tl.itertuples():
        assert row.FAST_pct + row.MODERATE_pct + row.SAFE_pct == pytest.approx(100.0)

    agg = ar.aggregate_timelines(tl)
    mean = agg[(agg.latency_ms == 0) & (agg.preset == "READ_HEAVY")].iloc[0]
    assert mean.FAST_pct == pytest.approx((100 * 30 / 90 + 100 * 60 / 110) / 2)


def test_run_analysis_writes_every_table_and_figure(tree):
    ar.run_analysis(tree, verbose=False)

    for name in ["summary_reps", "summary_agg", "staleness_reps", "staleness_pooled", "settings_timeline", "settings_timeline_agg",
                 "probe_reps", "probe_pooled", "probe_by_settings"]:
        assert (tree / f"{name}.csv").stat().st_size > 0, name
    for name in ["fig_sweep_p50", "fig_sweep_p95", "fig_sweep_staleness", "fig_settings_used", "fig_probe_staleness"]:
        assert (tree / f"{name}.png").stat().st_size > 0, name
    for lat in (0, 43):
        for name in ["fig_latency_percentiles", "fig_throughput", "fig_adaptive_overhead", "fig_staleness"]:
            assert (tree / f"latency_{lat}ms" / f"{name}.png").stat().st_size > 0, (lat, name)


def test_levels_and_max_rep_filters_ignore_leftovers(tree):
    assert [(lat, rep) for lat, rep, _ in ar.rep_dirs(tree, levels=[43])] == [(43, 1), (43, 2)]
    assert [(lat, rep) for lat, rep, _ in ar.rep_dirs(tree, max_rep=1)] == [(0, 1), (43, 1)]
    bench, stale = ar.load_all(tree, levels=[0], max_rep=1)
    assert set(bench.latency_ms) == {0} and set(bench.rep) == {1} and set(stale.rep) == {1}


def test_read_and_write_latency_are_parsed_separately(tmp_path):
    d = tmp_path / "latency_0ms" / "rep1"
    d.mkdir(parents=True)
    with open(d / "static_fast_READ_HEAVY_stats.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(STATS_HEADER)
        w.writerow(["mongo", "read", 950, 0, 5, 47.0, 5, 9, 12])
        w.writerow(["mongo", "write", 50, 0, 150, 2.5, 150, 170, 180])
        w.writerow(["", "Aggregated", 1000, 0, 12, 49.5, 6, 160, 175])
    row = ar.load_benchmark_summary(d, verbose=False).iloc[0]
    assert (row.read_p50_ms, row.write_p50_ms, row.write_p95_ms) == (5.0, 150.0, 170.0)
    assert row.p50_ms == 6.0


def test_missing_read_or_write_rows_become_nan_not_errors(tree):
    bench, _ = ar.load_all(tree)
    assert bench.read_p50_ms.isna().all()
    agg = ar.aggregate_benchmark(bench)
    assert agg.write_p95_ms.isna().all()


def test_probes_are_pooled_over_repeats_and_split_by_settings(tree):
    pr = ar.load_probes(tree)
    pooled = ar.pool_probes(pr)
    fast = pooled[(pooled.latency_ms == 0) & (pooled.config == "static_fast") & (pooled.preset == "READ_HEAVY")].iloc[0]
    assert (fast.probes, fast.stale) == (200, 60)  # 20 + 40 stale of 100 + 100
    assert fast.stale_pct == pytest.approx(30.0)
    adaptive = pooled[(pooled.latency_ms == 0) & (pooled.config == "adaptive") & (pooled.preset == "READ_HEAVY")].iloc[0]
    assert (adaptive.probes, adaptive.stale) == (200, 60)

    split = ar.probes_by_settings(pr)
    a_fast = split[(split.config == "adaptive") & (split.settings == "FAST") & (split.latency_ms == 0) & (split.preset == "READ_HEAVY")].iloc[0]
    a_safe = split[(split.config == "adaptive") & (split.settings == "SAFE") & (split.latency_ms == 0) & (split.preset == "READ_HEAVY")].iloc[0]
    assert a_fast.stale_pct == pytest.approx(50.0)  # 60 of 120 FAST probes
    assert a_safe.stale_pct == 0.0 and a_safe.probes == 80


def test_probe_files_without_any_probes_do_not_break_the_analysis(tmp_path):
    d = tmp_path / "latency_0ms" / "rep1"
    d.mkdir(parents=True)
    _write_stats(d / "static_fast_READ_HEAVY_stats.csv", 10, 20, 30)
    (d / "static_fast_READ_HEAVY_probe.csv").write_text("settings,probes,stale\n", encoding="utf-8")
    assert ar.load_probes(tmp_path).empty
    assert ar.pool_probes(ar.load_probes(tmp_path)).empty
