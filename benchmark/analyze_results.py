"""Results Analysis (Phase 11): turns the raw CSVs produced by
run_benchmark.py and staleness_test.py into report-ready tables and charts.

Reads, under results/write_share_eval/latency_<N>ms/rep<k>/:
    <config>_<preset>_stats.csv      (one per config x preset, written by Locust)
    <config>_<preset>_probe.csv      (stale-read tally of the staleness probes inside the load test)
    adaptive_<preset>_switches.csv   (what settings the adaptive layer chose and when)
    staleness.csv                    (written by benchmark/staleness_test.py)

Writes, into results/write_share_eval/:
    summary_reps.csv / summary_agg.csv           per-repeat rows / mean and std over repeats
    staleness_reps.csv / staleness_pooled.csv    per-repeat rows / trials pooled over repeats
    settings_timeline.csv / ..._agg.csv          share of run time adaptive spent in FAST/MODERATE/SAFE
    probe_reps.csv / probe_pooled.csv / probe_by_settings.csv   staleness measured inside the load test
    fig_sweep_p50.png, fig_sweep_p95.png, fig_sweep_staleness.png, fig_settings_used.png, fig_probe_staleness.png
    latency_<N>ms/fig_*.png                      per-latency charts (means over repeats)

Usage:
    python benchmark/analyze_results.py
"""

import csv
import os
import re
import sys
from collections import namedtuple
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "results"
EVAL_DIR = RESULTS_DIR / "write_share_eval"

sys.path.insert(0, str(REPO_ROOT))  # so `python benchmark/analyze_results.py` can import the benchmark package
from benchmark.probe_log import read_probe_csv  # noqa: E402
from benchmark.switch_log import time_in_settings  # noqa: E402

CONFIGS = ["static_fast", "static_safe", "adaptive"]
PRESETS = [
    "READ_HEAVY", "WRITE_HEAVY", "MIXED_BURSTY",
    "BALANCED", "HOTSPOT", "RAMP", "SPIKE", "HIGH_CONCURRENCY", "MODEST_WRITES",
]

CONFIG_COLORS = {"static_fast": "#e07b39", "static_safe": "#3b6ea5", "adaptive": "#2ea043"}
CONFIG_LABELS = {"static_fast": "Static Fast (w=1)", "static_safe": "Static Safe (w=majority)", "adaptive": "Adaptive"}


def load_benchmark_summary(results_dir=RESULTS_DIR, verbose=True):
    """Read every <config>_<preset>_stats.csv Aggregated row into one table."""
    rows = []
    for config in CONFIGS:
        for preset in PRESETS:
            stats_path = Path(results_dir) / f"{config}_{preset}_stats.csv"
            if not stats_path.exists():
                if verbose:
                    print(f"  (skip) missing {stats_path.name}")
                continue
            with open(stats_path, newline="", encoding="utf-8") as f:
                csv_rows = list(csv.DictReader(f))
            agg = next((r for r in csv_rows if r["Name"] == "Aggregated"), None)
            if agg is None:
                print(f"  (skip) no Aggregated row in {stats_path.name}")
                continue
            by_op = {r["Name"]: r for r in csv_rows if r["Name"] in ("read", "write")}
            op_cols = {}
            for op in ("read", "write"):
                r = by_op.get(op)
                op_cols[f"{op}_p50_ms"] = float(r["50%"]) if r else float("nan")
                op_cols[f"{op}_p95_ms"] = float(r["95%"]) if r else float("nan")
            rows.append({
                **op_cols,
                "config": config,
                "preset": preset,
                "requests": int(agg["Request Count"]),
                "failures": int(agg["Failure Count"]),
                "req_per_s": float(agg["Requests/s"]),
                "p50_ms": float(agg["50%"]),
                "p95_ms": float(agg["95%"]),
                "p99_ms": float(agg["99%"]),
                "avg_ms": float(agg["Average Response Time"]),
            })
    return pd.DataFrame(rows)


def load_staleness(results_dir=RESULTS_DIR, verbose=True):
    path = Path(results_dir) / "staleness.csv"
    if not path.exists():
        if verbose:
            print(f"  (skip) missing {path.name}")
        return pd.DataFrame()
    return pd.read_csv(path)


def _grouped_bar(ax, df, value_col, ylabel, title):
    presets_present = [p for p in PRESETS if p in df["preset"].unique()]
    configs_present = [c for c in CONFIGS if c in df["config"].unique()]
    n_configs = len(configs_present)
    width = 0.8 / max(n_configs, 1)
    x = range(len(presets_present))

    for i, config in enumerate(configs_present):
        values = []
        for preset in presets_present:
            match = df[(df["config"] == config) & (df["preset"] == preset)]
            values.append(match[value_col].iloc[0] if not match.empty else 0)
        offsets = [xi + (i - (n_configs - 1) / 2) * width for xi in x]
        ax.bar(offsets, values, width=width, label=CONFIG_LABELS.get(config, config),
               color=CONFIG_COLORS.get(config, None))

    ax.set_xticks(list(x))
    ax.set_xticklabels([p.replace("_", " ").title() for p in presets_present])
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(fontsize=8)
    ax.grid(axis="y", linestyle="--", alpha=0.4)


def make_latency_chart(df, out_path):
    if df.empty:
        print("  (skip) latency chart: no benchmark data")
        return
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    _grouped_bar(axes[0], df, "p50_ms", "Latency (ms)", "p50 Latency")
    _grouped_bar(axes[1], df, "p95_ms", "Latency (ms)", "p95 Latency")
    _grouped_bar(axes[2], df, "p99_ms", "Latency (ms)", "p99 Latency")
    fig.suptitle("Latency by Configuration and Workload", fontsize=13, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"  wrote {out_path}")


def make_throughput_chart(df, out_path):
    if df.empty:
        print("  (skip) throughput chart: no benchmark data")
        return
    fig, ax = plt.subplots(figsize=(7, 4.5))
    _grouped_bar(ax, df, "req_per_s", "Requests / second", "Throughput by Configuration and Workload")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"  wrote {out_path}")


def make_staleness_chart(df, out_path):
    if df.empty:
        print("  (skip) staleness chart: no staleness data")
        return
    order = ["static_fast", "static_safe", "adaptive_hot", "adaptive_cold"]
    df = df.set_index("config").reindex([c for c in order if c in df["config"].tolist() or c in df.index]).reset_index() \
        if "config" in df.columns else df
    labels = [c for c in order if c in df["config"].values]
    values = [df[df["config"] == c]["stale_rate"].iloc[0] * 100 for c in labels]
    colors = ["#e07b39", "#3b6ea5", "#c0392b", "#2ea043"][: len(labels)]

    fig, ax = plt.subplots(figsize=(7, 4.5))
    bars = ax.bar([l.replace("_", " ").title() for l in labels], values, color=colors)
    for bar, v in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{v:.1f}%",
                ha="center", va="bottom", fontsize=9)
    ax.set_ylabel("Stale-read rate (%)")
    ax.set_title("Read-After-Write Staleness by Configuration")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"  wrote {out_path}")


def make_overhead_chart(df, out_path):
    """Approximate 'adaptive layer overhead': how much p50 latency the
    adaptive configuration adds/saves relative to the static baseline that
    matches what its Decision Engine *should* pick for that workload
    (static_fast for READ_HEAVY, static_safe for WRITE_HEAVY, the average of
    both for MIXED_BURSTY, since it oscillates between both regimes)."""
    if df.empty:
        print("  (skip) overhead chart: no benchmark data")
        return
    baseline_for_preset = {"READ_HEAVY": "static_fast", "WRITE_HEAVY": "static_safe"}
    presets_present = [p for p in PRESETS if p in df["preset"].unique()]
    labels, overhead_pct = [], []
    for preset in presets_present:
        adaptive_row = df[(df["config"] == "adaptive") & (df["preset"] == preset)]
        if adaptive_row.empty:
            continue
        adaptive_p50 = adaptive_row["p50_ms"].iloc[0]

        if preset in baseline_for_preset:
            base_row = df[(df["config"] == baseline_for_preset[preset]) & (df["preset"] == preset)]
            if base_row.empty:
                continue
            baseline_p50 = base_row["p50_ms"].iloc[0]
        else:
            fast_row = df[(df["config"] == "static_fast") & (df["preset"] == preset)]
            safe_row = df[(df["config"] == "static_safe") & (df["preset"] == preset)]
            if fast_row.empty or safe_row.empty:
                continue
            baseline_p50 = (fast_row["p50_ms"].iloc[0] + safe_row["p50_ms"].iloc[0]) / 2

        if baseline_p50 == 0:
            continue
        labels.append(preset.replace("_", " ").title())
        overhead_pct.append((adaptive_p50 - baseline_p50) / baseline_p50 * 100)

    if not labels:
        print("  (skip) overhead chart: not enough matching data")
        return

    fig, ax = plt.subplots(figsize=(7, 4.5))
    colors = ["#c0392b" if v > 0 else "#2ea043" for v in overhead_pct]
    bars = ax.bar(labels, overhead_pct, color=colors)
    for bar, v in zip(bars, overhead_pct):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{v:+.1f}%",
                ha="center", va="bottom" if v >= 0 else "top", fontsize=9)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel("p50 latency vs. matching static baseline (%)")
    ax.set_title("Adaptive Layer Overhead vs. the Workload-Appropriate Static Baseline")
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"  wrote {out_path}")


METRICS = ["p50_ms", "p95_ms", "p99_ms", "req_per_s", "avg_ms",
           "read_p50_ms", "read_p95_ms", "write_p50_ms", "write_p95_ms"]
SETTING_COLORS = {"FAST": "#e07b39", "MODERATE": "#9aa0a6", "SAFE": "#3b6ea5"}

# Lines that coincide (adaptive vs static_safe often do) must stay visible: each
# series gets its own marker and line style plus a small horizontal offset.
SERIES_STYLE = {
    "static_fast": ("o", "-", -0.6),
    "static_safe": ("s", "--", 0.0),
    "adaptive": ("^", ":", 0.6),
    "adaptive_hot": ("^", ":", 0.6),
    "adaptive_cold": ("v", "-.", 1.2),
}


def rep_dirs(root=EVAL_DIR, levels=None, max_rep=None):
    """[(latency_ms, rep, dir)] for every <root>/latency_<N>ms/rep<k> folder, sorted.
    levels / max_rep restrict it to the latency levels and repeats a run actually used,
    so leftovers from an earlier smoke run are ignored."""
    found = []
    for d in Path(root).glob("latency_*ms/rep*"):
        match = re.fullmatch(r"latency_(\d+)ms", d.parent.name)
        rep = re.fullmatch(r"rep(\d+)", d.name)
        if not (match and rep and d.is_dir()):
            continue
        latency_ms, rep_no = int(match.group(1)), int(rep.group(1))
        if (levels is None or latency_ms in levels) and (max_rep is None or rep_no <= max_rep):
            found.append((latency_ms, rep_no, d))
    return sorted(found)


def load_all(root=EVAL_DIR, levels=None, max_rep=None):
    """Benchmark and staleness tables for every latency level and repeat,
    with latency_ms and rep columns."""
    bench_parts, stale_parts = [], []
    for latency_ms, rep, d in rep_dirs(root, levels, max_rep):
        b = load_benchmark_summary(d, verbose=False)
        if not b.empty:
            bench_parts.append(b.assign(latency_ms=latency_ms, rep=rep))
        s_ = load_staleness(d, verbose=False)
        if not s_.empty:
            stale_parts.append(s_.assign(latency_ms=latency_ms, rep=rep))
    bench = pd.concat(bench_parts, ignore_index=True) if bench_parts else pd.DataFrame()
    stale = pd.concat(stale_parts, ignore_index=True) if stale_parts else pd.DataFrame()
    return bench, stale


def aggregate_benchmark(bench_all):
    """Mean and sample standard deviation across repeats per (latency, preset,
    config); the std columns are NaN when there is a single repeat."""
    if bench_all.empty:
        return pd.DataFrame()
    g = bench_all.groupby(["latency_ms", "preset", "config"])
    out = g[METRICS].mean()
    for m in METRICS:
        out[m + "_std"] = g[m].std(ddof=1)
    out["requests"] = g["requests"].sum()
    out["failures"] = g["failures"].sum()
    out["n_reps"] = g.size()
    return out.reset_index()


def pool_staleness(stale_all):
    """Pool trials across repeats per (latency, scenario). rep_std is the
    standard deviation of the per-repeat stale rates (NaN for one repeat)."""
    if stale_all.empty:
        return pd.DataFrame()
    g = stale_all.groupby(["latency_ms", "config"])
    out = g[["trials", "hits", "misses"]].sum()
    if "in_expected_settings" in stale_all.columns:
        out["in_expected_settings"] = g["in_expected_settings"].sum()
    out["stale_rate"] = out["misses"] / out["trials"]
    out["rep_std"] = g["stale_rate"].std(ddof=1)
    out["n_reps"] = g.size()
    return out.reset_index()


def load_switch_timelines(root=EVAL_DIR, levels=None, max_rep=None):
    """Seconds (and share of run time) adaptive spent in each named setting,
    per (latency, repeat, preset), from the <config>_<preset>_switches.csv files."""
    rows = []
    for latency_ms, rep, d in rep_dirs(root, levels, max_rep):
        for path in sorted(d.glob("adaptive_*_switches.csv")):
            preset = path.name[len("adaptive_"):-len("_switches.csv")]
            t = time_in_settings(path)
            total = sum(t[n] for n in ("FAST", "MODERATE", "SAFE")) or 1.0
            rows.append({
                "latency_ms": latency_ms, "rep": rep, "preset": preset, "switches": t["switches"],
                **{f"{n}_pct": 100.0 * t[n] / total for n in ("FAST", "MODERATE", "SAFE")},
            })
    return pd.DataFrame(rows)


def load_probes(root=EVAL_DIR, levels=None, max_rep=None):
    """One row per (latency, repeat, config, preset, settings in force) from the
    <config>_<preset>_probe.csv files: probes issued and how many read back stale."""
    rows = []
    for latency_ms, rep, d in rep_dirs(root, levels, max_rep):
        for path in sorted(d.glob("*_probe.csv")):
            base = path.name[: -len("_probe.csv")]
            config = next((c for c in CONFIGS if base.startswith(c + "_")), None)
            if config is None:
                continue
            for settings, (probes, stale) in read_probe_csv(path).items():
                rows.append({"latency_ms": latency_ms, "rep": rep, "config": config,
                             "preset": base[len(config) + 1:], "settings": settings,
                             "probes": probes, "stale": stale})
    return pd.DataFrame(rows)


def _with_stale_pct(df):
    df["stale_pct"] = 100.0 * df["stale"] / df["probes"].where(df["probes"] > 0)
    return df


def pool_probes(pr):
    """Probes pooled over repeats and settings: per (latency, config, preset)."""
    if pr.empty:
        return pd.DataFrame()
    return _with_stale_pct(pr.groupby(["latency_ms", "config", "preset"])[["probes", "stale"]].sum().reset_index())


def probes_by_settings(pr):
    """Probes pooled over repeats, split by the settings in force: for adaptive runs
    this separates the stale rate while in FAST from the rate while in SAFE."""
    if pr.empty:
        return pd.DataFrame()
    return _with_stale_pct(pr.groupby(["latency_ms", "config", "preset", "settings"])[["probes", "stale"]].sum().reset_index())


def aggregate_timelines(tl):
    if tl.empty:
        return pd.DataFrame()
    cols = ["FAST_pct", "MODERATE_pct", "SAFE_pct", "switches"]
    return tl.groupby(["latency_ms", "preset"])[cols].mean().reset_index()


def make_latency_sweep_chart(df, metric, ylabel, out_path):
    """One panel per preset: x = injected one-way latency, one line per config.
    If df has a <metric>_std column (aggregated over repeats) it is drawn as error bars."""
    if df.empty:
        print("  (skip) sweep chart: no benchmark data")
        return
    std_col = metric + "_std"
    presets_present = [p for p in PRESETS if p in df["preset"].unique()]
    ncols = 4
    nrows = -(-len(presets_present) // ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.4 * nrows), squeeze=False, sharex=True)
    for ax, preset in zip(axes.flat, presets_present):
        for config in CONFIGS:
            sub = df[(df["preset"] == preset) & (df["config"] == config)].sort_values("latency_ms")
            if sub.empty:
                continue
            yerr = sub[std_col].fillna(0) if std_col in sub.columns else None
            marker, style, dodge = SERIES_STYLE[config]
            ax.errorbar(sub["latency_ms"] + dodge, sub[metric], yerr=yerr, marker=marker, linestyle=style,
                        capsize=3, markersize=5, label=CONFIG_LABELS[config], color=CONFIG_COLORS[config])
        ax.set_title(preset.replace("_", " ").title(), fontsize=10)
        ax.grid(linestyle="--", alpha=0.4)
    for ax in list(axes.flat)[len(presets_present):]:
        ax.axis("off")
    for ax in axes[-1]:
        ax.set_xlabel("one-way inter-node delay (ms)")
    for ax in axes[:, 0]:
        ax.set_ylabel(ylabel)
    axes.flat[0].legend(fontsize=8)
    fig.suptitle(f"{ylabel} vs. injected inter-node latency (mean, error bars = std over repeats)",
                 fontsize=13, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"  wrote {out_path}")


def make_staleness_sweep_chart(df, out_path):
    """Pooled stale-read rate vs latency; error bars = std of the per-repeat rates."""
    if df.empty:
        print("  (skip) staleness sweep chart: no staleness data")
        return
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    colors = {"static_fast": "#e07b39", "static_safe": "#3b6ea5", "adaptive_hot": "#c0392b", "adaptive_cold": "#2ea043"}
    for config, color in colors.items():
        sub = df[df["config"] == config].sort_values("latency_ms")
        if sub.empty:
            continue
        yerr = sub["rep_std"].fillna(0) * 100 if "rep_std" in sub.columns else None
        marker, style, dodge = SERIES_STYLE[config]
        ax.errorbar(sub["latency_ms"] + dodge, sub["stale_rate"] * 100, yerr=yerr, marker=marker, linestyle=style,
                    capsize=3, markersize=5, label=config.replace("_", " ").title(), color=color)
    ax.set_xlabel("one-way inter-node delay (ms)")
    ax.set_ylabel("Stale-read rate (%)")
    ax.set_title("Read-After-Write Staleness vs. Injected Latency")
    ax.grid(linestyle="--", alpha=0.4)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"  wrote {out_path}")


def make_settings_timeline_chart(tl_agg, out_path):
    """Share of run time adaptive spent in FAST / MODERATE / SAFE, per preset,
    one panel per latency level (mean over repeats)."""
    if tl_agg.empty:
        print("  (skip) settings chart: no switch logs")
        return
    levels = sorted(tl_agg["latency_ms"].unique())
    presets = [p for p in PRESETS if p in tl_agg["preset"].unique()]
    fig, axes = plt.subplots(1, len(levels), figsize=(5.2 * len(levels), 0.55 * len(presets) + 2.2),
                             squeeze=False, sharey=True)
    for ax, lat in zip(axes[0], levels):
        sub = tl_agg[tl_agg["latency_ms"] == lat].set_index("preset").reindex(presets)
        left = pd.Series(0.0, index=presets)
        for name in ("FAST", "MODERATE", "SAFE"):
            vals = sub[f"{name}_pct"].fillna(0)
            ax.barh(range(len(presets)), vals, left=left, color=SETTING_COLORS[name], label=name)
            left = left + vals
        ax.set_yticks(range(len(presets)))
        ax.set_yticklabels([p.replace("_", " ").title() for p in presets])
        ax.invert_yaxis()
        ax.set_xlim(0, 100)
        ax.set_xlabel("% of run time")
        ax.set_title(f"{lat} ms one-way", fontsize=10)
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=3, fontsize=9, frameon=False)
    fig.suptitle("Settings the adaptive layer actually used", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"  wrote {out_path}")


def make_probe_chart(probes_pooled, out_path):
    """Stale-read rate of the in-load probes, grouped by preset, one panel per latency level."""
    if probes_pooled.empty:
        print("  (skip) probe chart: no probe data")
        return
    levels = sorted(probes_pooled["latency_ms"].unique())
    fig, axes = plt.subplots(1, len(levels), figsize=(6.2 * len(levels), 4.5), squeeze=False, sharey=True)
    for ax, lat in zip(axes[0], levels):
        _grouped_bar(ax, probes_pooled[probes_pooled["latency_ms"] == lat], "stale_pct", "Stale probe reads (%)", f"{lat} ms one-way")
        ax.tick_params(axis="x", labelrotation=45)
        for bar in ax.patches:  # label every bar, so a true 0% is not mistaken for missing data
            height = bar.get_height()
            if height == height:  # skip NaN (no probes)
                ax.text(bar.get_x() + bar.get_width() / 2, height, f"{height:.0f}%", ha="center", va="bottom", fontsize=7)
    handles, labels = axes[0][0].get_legend_handles_labels()
    for ax in axes[0]:
        ax.get_legend().remove()
    fig.legend(handles, labels, loc="lower center", ncol=3, fontsize=9, frameon=False)
    fig.suptitle("Staleness measured inside the load test (write then immediate read)", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"  wrote {out_path}")


def make_sweep_charts(bench_agg, stale_pooled, tl_agg, out_dir=EVAL_DIR):
    """The cross-latency figures: p50, p95, staleness vs latency, and settings used."""
    out_dir = Path(out_dir)
    make_latency_sweep_chart(bench_agg, "p50_ms", "p50 latency (ms)", out_dir / "fig_sweep_p50.png")
    make_latency_sweep_chart(bench_agg, "p95_ms", "p95 latency (ms)", out_dir / "fig_sweep_p95.png")
    make_staleness_sweep_chart(stale_pooled, out_dir / "fig_sweep_staleness.png")
    make_settings_timeline_chart(tl_agg, out_dir / "fig_settings_used.png")


def make_level_charts(bench_agg, stale_pooled, level_dir):
    """The four per-latency figures (means over repeats) into results/.../latency_<N>ms/."""
    level_dir = Path(level_dir)
    make_latency_chart(bench_agg, level_dir / "fig_latency_percentiles.png")
    make_throughput_chart(bench_agg, level_dir / "fig_throughput.png")
    make_overhead_chart(bench_agg, level_dir / "fig_adaptive_overhead.png")
    make_staleness_chart(stale_pooled, level_dir / "fig_staleness.png")


Analysis = namedtuple(
    "Analysis",
    "bench_reps bench_agg stale_reps stale_pooled timelines timelines_agg probes probes_pooled probes_by_settings",
)


def run_analysis(root=EVAL_DIR, verbose=True, levels=None, max_rep=None):
    """Load everything under root (optionally only some levels / the first max_rep
    repeats), write the aggregate CSVs and all figures. Returns an Analysis of the tables."""
    root = Path(root)
    bench_all, stale_all = load_all(root, levels, max_rep)
    bench_agg, stale_pooled = aggregate_benchmark(bench_all), pool_staleness(stale_all)
    tl = load_switch_timelines(root, levels, max_rep)
    tl_agg = aggregate_timelines(tl)
    probes = load_probes(root, levels, max_rep)
    probes_pooled, probes_split = pool_probes(probes), probes_by_settings(probes)

    for name, df in [("summary_reps", bench_all), ("summary_agg", bench_agg), ("staleness_reps", stale_all),
                     ("staleness_pooled", stale_pooled), ("settings_timeline", tl), ("settings_timeline_agg", tl_agg),
                     ("probe_reps", probes), ("probe_pooled", probes_pooled), ("probe_by_settings", probes_split)]:
        if not df.empty:
            df.to_csv(root / f"{name}.csv", index=False)

    for latency_ms in sorted(bench_agg["latency_ms"].unique()) if not bench_agg.empty else []:
        if verbose:
            print(f"--- latency {latency_ms} ms ---")
        make_level_charts(
            bench_agg[bench_agg["latency_ms"] == latency_ms],
            stale_pooled[stale_pooled["latency_ms"] == latency_ms] if not stale_pooled.empty else pd.DataFrame(),
            root / f"latency_{latency_ms}ms",
        )
    if verbose:
        print("--- sweep across latency levels ---")
    make_sweep_charts(bench_agg, stale_pooled, tl_agg, root)
    make_probe_chart(probes_pooled, root / "fig_probe_staleness.png")
    return Analysis(bench_all, bench_agg, stale_all, stale_pooled, tl, tl_agg, probes, probes_pooled, probes_split)


def main():
    if not rep_dirs():
        print(f"no {EVAL_DIR}{os.sep}latency_<N>ms{os.sep}rep<k> folders found - run benchmark/run_benchmark.py first")
        return
    run_analysis()
    print("\nDone.")


if __name__ == "__main__":
    main()
