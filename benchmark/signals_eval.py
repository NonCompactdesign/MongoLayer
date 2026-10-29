"""One evaluation run of the signal rules (the full evaluation, ~3 h 45 min with profile FULL).

    python -m benchmark.signals_eval                     # profile FULL
    python -m benchmark.signals_eval --profile SMOKE     # ~14 min, to check the pipeline
    python -m benchmark.signals_eval --tag <tag>         # resume an interrupted run

A run has a Plan (which network setups, configurations, presets, run lengths, repeats) and one
output folder, results/signals_eval/<tag>/:

    run_meta.json                     the plan, git commit, versions, start/end times, phase timings
    run_log.txt                       everything the runs printed
    preflight.json                    the checks made before anything long started
    checks.json                       the live signal-rule scenarios (benchmark/signals_check.py)
    topology_<setup>/rep<k>/          Locust CSVs, probe tallies, switch logs, client counters,
                                      and a <config>_<preset>.done marker per finished run
    staleness_<setup>.csv             the read-after-write staleness experiment per setup
    summary.csv, staleness.csv, expectations.csv, figs/*.png   written by analyse()

Every load-test run leaves a .done marker, so a run that is interrupted (laptop asleep, Docker
restart) continues where it stopped: run it again with the same tag and finished runs are skipped.

Setups are topologies (testbed/topology.py): EQUAL-<ms> are equal-distance levels (one way
between nodes, client equidistant), T1 and T2 the unequal ones. The default run uses EQUAL-0, T1
and T2. EQUAL-5 is left out: with these additions the layer behaves at 5 ms as at 0 ms (SAFE stays
well inside the latency budget), and the 2026-10-19 overnight data already shows what 5 ms changes
(SAFE writes ~5-10 ms slower, static FAST 1-11% stale).
"""

import contextlib
import csv
import io
import json
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from benchmark import run_benchmark as rb
from testbed import cluster, netem, topology

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_ROOT = REPO_ROOT / "results" / "signals_eval"
SETTINGS_NAMES = ("FAST", "MODERATE", "SAFE")

# The default workloads: each shows one addition at work. The other four presets add little here
# (WRITE_HEAVY and HOTSPOT behave like BALANCED, all SAFE; RAMP only tests a slow ramp-up;
# HIGH_CONCURRENCY is a load test of the layer itself) and are in the FULL_ALL profile.
CORE_PRESETS = [
    "READ_HEAVY",          # FAST + per-key read-your-own-writes (#6)
    "READ_HEAVY_HOTSPOT",  # the cost of routing hot keys to the primary (#6)
    "MODEST_WRITES",       # MODERATE
    "BALANCED",            # the SAFE latency budget (#9)
    "MIXED_BURSTY",        # hysteresis and switching (#8, #10)
    "SPIKE",               # short write bursts
]

# Measured on this machine (2026-10-18): a Locust run takes its length plus ~5 s to start and stop.
RUN_OVERHEAD_S = 5.0
SETUP_OVERHEAD_S = 15.0  # apply a topology + pin the primary
CHECKS_S = 140.0  # signals_check, all five scenarios (measured ~2.3 min)
# Staleness experiment per setup: dominated by adaptive_cold's 60 spaced trials (~2 min); the far
# topologies add the slow SAFE / primary round trips (estimate, not yet measured).
STALENESS_S = {"EQUAL-0": 150.0, "EQUAL-5": 170.0, "T1": 330.0, "T2": 240.0}


@dataclass
class Plan:
    setups: list = field(default_factory=lambda: ["EQUAL-0", "T1", "T2"])
    configs: list = field(default_factory=lambda: list(rb.CONFIGS))
    presets: list = field(default_factory=lambda: list(CORE_PRESETS))
    duration_scale: float = 0.6  # 300 s runs -> 180 s
    fixed_duration_s: int | None = None  # overrides every run length (smoke tests)
    repeats: int = 1
    run_checks: bool = True
    staleness_setups: list = field(default_factory=lambda: ["EQUAL-0", "T1", "T2"])
    staleness_trials: int = 200

    def __post_init__(self):
        for s in list(self.setups) + list(self.staleness_setups):
            topology.by_name(s)  # raises on an unknown setup
        bad = [c for c in self.configs if c not in rb.CONFIGS] + [p for p in self.presets if p not in rb.PRESETS]
        if bad:
            raise ValueError(f"unknown configs/presets: {bad}")
        if self.repeats < 1:
            raise ValueError("repeats must be >= 1")

    def params(self, preset):
        """Locust parameters for one preset: the full-length tables scaled, or a fixed length."""
        p = rb.params_for(preset, quick=False, duration_scale=self.duration_scale)
        if self.fixed_duration_s:
            p["duration"] = f"{int(self.fixed_duration_s)}s"
        return p

    def cells(self):
        """Every (setup, rep, config, preset) load-test run, in the order they are run."""
        return [(s, r, c, p) for s in self.setups for r in range(1, self.repeats + 1)
                for c in self.configs for p in self.presets]


def _seconds(duration):
    return int(str(duration).rstrip("s"))


def estimate(plan):
    """[(phase, seconds)] and the total, from the measured per-run overhead."""
    rows = []
    if plan.run_checks:
        rows.append((f"live checks ({len(_scenarios())} scenarios)", CHECKS_S))
    for setup in plan.setups:
        per_rep = sum(_seconds(plan.params(p)["duration"]) + RUN_OVERHEAD_S for p in plan.presets) * len(plan.configs)
        rows.append((f"load tests {setup} ({len(plan.configs)} configs x {len(plan.presets)} presets"
                     f" x {plan.repeats} rep)", per_rep * plan.repeats + SETUP_OVERHEAD_S))
    for setup in plan.staleness_setups:
        scale = min(1.0, plan.staleness_trials / 200)
        rows.append((f"staleness {setup}", STALENESS_S.get(setup, 300.0) * max(scale, 0.5) + SETUP_OVERHEAD_S))
    return rows, sum(s for _, s in rows)


def _scenarios():
    from benchmark import signals_check
    return signals_check.SCENARIOS


def fmt_hours(seconds):
    h, m = divmod(int(round(seconds / 60)), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


class Log:
    """Writes to run_log.txt; `say` also prints the line."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, text):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")

    def say(self, text):
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] {text}", flush=True)
        self.write(f"[{stamp}] {text}")


def out_dir_for(tag):
    return EVAL_ROOT / tag


def _git_commit():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True)
        return out.stdout.strip() + ("+uncommitted" if dirty.stdout.strip() else "")
    except OSError:
        return "unknown"


def write_meta(out, plan, **extra):
    path = Path(out) / "run_meta.json"
    meta = json.loads(path.read_text()) if path.exists() else {}
    meta.setdefault("plan", asdict(plan))
    meta.setdefault("git_commit", _git_commit())
    meta.setdefault("python", sys.version.split()[0])
    meta.setdefault("platform", platform.platform())
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(meta.get(k), dict):
            meta[k].update(v)
        else:
            meta[k] = v
    path.write_text(json.dumps(meta, indent=2, default=str))
    return meta


# --- checks before the long part --------------------------------------------------------------

def preflight(out):
    """[(check, ok, detail)]. Nothing long should start unless every one is ok."""
    results = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - a check that crashes is a failed check
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append((name, bool(ok), str(detail)))

    def containers():
        out_ = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
        running = set(out_.stdout.split())
        missing = [n for n in netem.NODES if n not in running]
        return not missing, "running: " + ", ".join(sorted(running & set(netem.NODES))) + (f"; missing {missing}" if missing else "")

    def clean_network():
        netem.clear()
        dirty = [n for n in netem.NODES if "netem" in netem._exec(n, f"tc qdisc show dev {netem.IFACE}").stdout]
        return not dirty, "no injected delays" if not dirty else f"delays left on {dirty}"

    def replica_set():
        cluster.pin_primary()
        return True, f"states {cluster.node_states()}, sync sources {cluster.sync_sources()}"

    def tc_works():
        topology.apply(topology.T1)
        try:
            rtts = topology.measure_client_rtts(samples=3)
        finally:
            topology.clear()
        ok = rtts["mongo2"] < 20 and rtts["mongo1"] > 60
        return ok, "T1 client round trips " + ", ".join(f"{n} {ms:.1f} ms" for n, ms in rtts.items())

    def disk():
        import shutil
        free_gb = shutil.disk_usage(REPO_ROOT).free / 1e9
        return free_gb > 2, f"{free_gb:.1f} GB free"

    check("Docker containers mongo1-3 running", containers)
    check("no leftover network delays", clean_network)
    check("replica set healthy, mongo1 pinned, direct replication", replica_set)
    check("topology T1 can be applied and measured", tc_works)
    check("disk space", disk)
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "preflight.json").write_text(json.dumps(
        [{"check": n, "ok": ok, "detail": d} for n, ok, d in results], indent=2))
    return results


# --- the phases -----------------------------------------------------------------------------------

def run_live_checks(out, log):
    from benchmark import signals_check

    path = Path(out) / "checks.json"
    results = json.loads(path.read_text()) if path.exists() else {}
    cluster.pin_primary()
    try:
        for name, fn in signals_check.SCENARIOS.items():
            if name in results:
                log.say(f"check {name}: already done ({'PASS' if results[name]['pass'] else 'FAIL'})")
                continue
            started = time.monotonic()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok, detail = fn()
            log.write(buf.getvalue())
            detail["seconds"] = round(time.monotonic() - started, 1)
            results[name] = {"pass": ok, **detail}
            path.write_text(json.dumps(results, indent=2, default=str))
            log.say(f"check {name} ({fn.__name__}): {'PASS' if ok else 'FAIL'} in {detail['seconds']:.0f} s")
    finally:
        netem.clear()
        cluster.pin_primary()
    return results


def cell_prefix(out, setup, rep, config, preset):
    return Path(out) / f"topology_{topology.by_name(setup).name}" / f"rep{rep}" / f"{config}_{preset}"


def is_done(out, setup, rep, config, preset):
    return Path(f"{cell_prefix(out, setup, rep, config, preset)}.done").exists()


# Watchdog: a run that has not exited WATCHDOG_GRACE_S after its planned length is killed and
# retried once; if the retry fails too, it is recorded as failed and the evaluation moves on.
WATCHDOG_GRACE_S = 120.0
RETRIES = 1


def watchdog_seconds(params):
    return _seconds(params["duration"]) + WATCHDOG_GRACE_S


def run_load_tests(out, plan, log):
    """Every load-test run not done yet (each finished run leaves a .done marker, so a rerun with
    the same tag resumes)."""
    cells = plan.cells()
    todo = [c for c in cells if not is_done(out, *c)]
    log.say(f"load tests: {len(cells)} runs planned, {len(cells) - len(todo)} already done, {len(todo)} to go")
    started = time.monotonic()
    done_now = 0
    for setup in plan.setups:
        mine = [c for c in todo if c[0] == setup]
        if not mine:
            continue
        topo = topology.by_name(setup)
        cluster.pin_primary()
        topology.apply(topo)
        log.say(f"setup {topo.name}: {topo.description}")
        try:
            for (_, rep, config, preset) in mine:
                prefix = cell_prefix(out, setup, rep, config, preset)
                prefix.parent.mkdir(parents=True, exist_ok=True)
                params = plan.params(preset)
                t0 = time.monotonic()
                summary, attempts = None, 0
                while summary is None and attempts < 1 + RETRIES:
                    attempts += 1
                    if attempts > 1:
                        log.say(f"  {topo.name} {config} {preset}: attempt {attempts - 1} failed or hung - retrying once")
                        cluster.pin_primary()  # a killed run can leave the set mid-election; re-check it
                        topology.apply(topo)
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        summary = rb.run_one(config, preset, params, prefix.parent, timeout_s=watchdog_seconds(params))
                    log.write(buf.getvalue())
                Path(f"{prefix}.done").write_text(json.dumps({"summary": summary, "params": params, "attempts": attempts,
                                                               "seconds": round(time.monotonic() - t0, 1)}))
                done_now += 1
                left = len(todo) - done_now
                rate = (time.monotonic() - started) / done_now
                state = (f"{summary['requests']} req, {summary['failures']} fail, p50 {summary['p50_ms']:.0f} ms"
                         if summary else "FAILED after a retry (hung or Locust error, see run_log.txt)")
                if summary and attempts > 1:
                    state += " (on the retry)"
                log.say(f"[{done_now}/{len(todo)}] {topo.name} rep{rep} {config:<16} {preset:<18} {state}"
                        f"  | ~{fmt_hours(left * rate)} left")
        finally:
            topology.clear()
    return done_now


def run_staleness(out, plan, log):
    from benchmark import staleness_test

    for setup in plan.staleness_setups:
        path = Path(out) / f"staleness_{topology.by_name(setup).name}.csv"
        if path.exists():
            log.say(f"staleness {setup}: already done")
            continue
        cluster.pin_primary()
        buf = io.StringIO()
        t0 = time.monotonic()
        with topology.applied(topology.by_name(setup)), contextlib.redirect_stdout(buf):
            rows = staleness_test.run_all(plan.staleness_trials)
        log.write(buf.getvalue())
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        log.say(f"staleness {setup}: " + ", ".join(f"{r['config']} {100 * r['stale_rate']:.0f}%" for r in rows)
                + f" ({time.monotonic() - t0:.0f} s)")


# --- analysis ---------------------------------------------------------------------------------------

def load_summary(out):
    import pandas as pd

    from benchmark import signals_report

    df = pd.DataFrame(signals_report.summarise(root=out))
    if df.empty:
        return df
    df["setup"] = df["topology"]
    return df


def load_staleness(out):
    import pandas as pd

    frames = []
    for path in sorted(Path(out).glob("staleness_*.csv")):
        d = pd.read_csv(path)
        d.insert(0, "setup", path.stem[len("staleness_"):])
        d["stale_pct"] = (100 * d["stale_rate"]).round(1)
        frames.append(d)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def integrity(out, plan, df, checks):
    """Checks that the run itself is complete and sound: [(check, ok, detail)]."""
    rows = []
    missing = [c for c in plan.cells() if not is_done(out, *c)]
    rows.append(("every planned load-test run finished", not missing,
                 f"{len(plan.cells()) - len(missing)}/{len(plan.cells())}" + (f"; missing e.g. {missing[:3]}" if missing else "")))
    errors = [c for c in plan.cells() if is_done(out, *c)
              and json.loads(Path(f"{cell_prefix(out, *c)}.done").read_text())["summary"] is None]
    rows.append(("no Locust errors or hung runs", not errors,
                 f"{len(errors)} runs failed even after a retry: {[c[2] + ' ' + c[3] + ' @ ' + c[0] for c in errors[:5]]}"
                 if errors else "none"))
    retried = [c for c in plan.cells() if is_done(out, *c)
               and json.loads(Path(f"{cell_prefix(out, *c)}.done").read_text()).get("attempts", 1) > 1]
    rows.append(("no run needed a retry", not retried,
                 "none" if not retried else
                 f"{len(retried)} hung or failed once, then finished on the retry: "
                 f"{[c[2] + ' ' + c[3] + ' @ ' + c[0] for c in retried[:5]]}"))
    fails = int(df["failures"].sum()) if not df.empty else 0
    rows.append(("no failed requests", fails == 0, f"{fails} failed requests in total"))
    unprobed = 0 if df.empty else int((df["probes"] == 0).sum())
    rows.append(("every run has staleness probes", not df.empty and unprobed == 0,
                 "no results" if df.empty else ("all runs probed" if unprobed == 0 else f"{unprobed} runs without probes")))
    if plan.run_checks:
        failed = [k for k, v in (checks or {}).items() if not v.get("pass")]
        rows.append(("live checks all PASS", bool(checks) and not failed and len(checks) == len(_scenarios()),
                     f"{sum(v['pass'] for v in checks.values())}/{len(checks)} PASS" if checks else "not run"))
    stale_missing = [s for s in plan.staleness_setups
                     if not (Path(out) / f"staleness_{topology.by_name(s).name}.csv").exists()]
    rows.append(("staleness experiment done for every setup", not stale_missing,
                 "all done" if not stale_missing else f"missing {stale_missing}"))
    return [(n, None if ok is None else bool(ok), d) for n, ok, d in rows]


def _cell(df, setup, config, preset):
    m = df[(df["setup"] == setup) & (df["config"] == config) & (df["preset"] == preset)]
    return None if m.empty else m.iloc[0]


def _value(df, setup, config, preset, column):
    row = _cell(df, setup, config, preset)
    v = None if row is None else row.get(column)
    return float("nan") if v is None or v != v else float(v)


def expectations(df):
    """What the design predicts, stated before the run. [(expectation, verdict, detail)];
    verdict is True / False, or None when the run did not include the cells it needs."""
    rows = []

    def add(text, verdict, detail):
        rows.append((text, None if verdict is None else bool(verdict), detail))

    # #6: per-key read-your-own-writes removes the probes' own-write staleness wherever FAST is used
    sig_all = df[df["config"] == "adaptive_signals"] if not df.empty else df
    if sig_all.empty:
        add("adaptive_signals: no stale probe while in FAST, in any setup (#6)", None, "no data")
    else:
        fast_stale = sig_all.get("FAST_stale")
        n = int(fast_stale.fillna(0).sum()) if fast_stale is not None else None
        add("adaptive_signals: no stale probe while in FAST, in any setup (#6)", None if n is None else n == 0,
            "no data" if n is None else f"{n} stale of {int(sig_all['FAST_probes'].fillna(0).sum())} probes taken in FAST")
    rh = {c: _cell(df, "T1", c, "READ_HEAVY") for c in ("static_fast", "adaptive_signals")} if not df.empty else {}
    if all(v is not None for v in rh.values()) and rh:
        sig, fast = rh["adaptive_signals"], rh["static_fast"]
        add("T1 READ_HEAVY: adaptive_signals uses FAST most of the run", sig["FAST_pct"] > 50, f"FAST {sig['FAST_pct']:.0f}% of the run")
        add("T1 READ_HEAVY: adaptive_signals is less stale than static_fast", sig["stale_pct"] < fast["stale_pct"],
            f"stale {sig['stale_pct']}% vs static_fast {fast['stale_pct']}%")
        add("T1 READ_HEAVY: adaptive_signals reads stay local (median < 20 ms)", sig["read_p50"] < 20,
            f"read p50 {sig['read_p50']:.0f} ms (static_moderate reads the 87 ms primary)")
    else:
        add("T1 READ_HEAVY comparisons", None, "no data")
    # #9: the budget fires where SAFE is expensive (T1, ~260 ms) and not where it is cheap
    for setup, should in (("T1", True), ("T2", False), ("EQUAL-0ms", False)):
        bal = _cell(df, setup, "adaptive_signals", "BALANCED") if not df.empty else None
        text = (f"{setup} BALANCED: the latency budget takes adaptive_signals out of SAFE" if should else
                f"{setup} BALANCED: SAFE is within budget, adaptive_signals stays mostly SAFE")
        if bal is None:
            add(text, None, "no data")
        elif should:
            add(text, "budget" in str(bal.get("reasons", "")), f"switch reasons: {bal.get('reasons', '-')}")
        else:
            add(text, "budget" not in str(bal.get("reasons", "")) and (bal.get("SAFE_pct") or 0) > 50,
                f"SAFE {bal.get('SAFE_pct', 0):.0f}% of the run, write p95 {bal.get('write_p95', float('nan')):.0f} ms")
    static_safe_stale = df[df["config"].isin(["static_moderate", "static_safe"])]["stale"].sum() if not df.empty else None
    add("static_moderate and static_safe are never stale", None if static_safe_stale is None else static_safe_stale == 0,
        f"{static_safe_stale} stale probes" if static_safe_stale is not None else "no data")
    return rows


def _save(fig, out, name):
    figs = Path(out) / "figs"
    figs.mkdir(parents=True, exist_ok=True)
    fig.savefig(figs / name, dpi=130, bbox_inches="tight")
    return figs / name


COLORS = {"static_fast": "#e07b39", "static_moderate": "#8e6bbf", "static_safe": "#3b6ea5",
          "adaptive_signals": "#0f7b6c"}
SETTING_COLORS = {"FAST": "#e07b39", "MODERATE": "#9aa0a6", "SAFE": "#3b6ea5"}


def figure_metric(df, out, column, label, name, log_scale=False, value_labels=False):
    """One panel per setup: grouped bars of `column` per preset, one bar per config."""
    import matplotlib.pyplot as plt
    import numpy as np

    setups = list(dict.fromkeys(df["setup"]))
    presets = [p for p in rb.PRESETS if p in set(df["preset"])]
    configs = [c for c in rb.CONFIGS if c in set(df["config"])]
    fig, axes = plt.subplots(len(setups), 1, figsize=(13, 3.2 * len(setups)), squeeze=False)
    width = 0.8 / len(configs)
    for ax, setup in zip(axes[:, 0], setups):
        for i, config in enumerate(configs):
            vals = [_value(df, setup, config, p, column) for p in presets]
            xs = np.arange(len(presets)) + (i - (len(configs) - 1) / 2) * width
            ax.bar(xs, vals, width, label=config, color=COLORS[config])
            if value_labels:
                for x, v in zip(xs, vals):
                    if v == v:  # not NaN
                        ax.text(x, v, f"{v:.0f}", ha="center", va="bottom", fontsize=6.5)
        ax.set_title(setup, fontsize=10, loc="left")
        ax.set_ylabel(label)
        ax.set_xticks(range(len(presets)))
        ax.set_xticklabels([p.replace("_", " ").title() for p in presets], fontsize=8)
        if log_scale:
            ax.set_yscale("log")
        else:
            ax.set_ylim(0, max(5.0, ax.get_ylim()[1] * 1.1))
        ax.grid(axis="y", linestyle="--", alpha=0.4)
    axes[0, 0].legend(ncol=len(configs), fontsize=8, loc="upper left", bbox_to_anchor=(0, 1.32))
    fig.tight_layout()
    _save(fig, out, name)
    return fig


def figure_settings(df, out):
    """Share of each adaptive_signals run spent in FAST / MODERATE / SAFE."""
    import matplotlib.pyplot as plt

    ad = df[df["config"] == "adaptive_signals"].copy()
    setups = list(dict.fromkeys(ad["setup"]))
    fig, axes = plt.subplots(1, len(setups), figsize=(4.4 * len(setups), 0.32 * len(ad) / max(1, len(setups)) + 1.8),
                             squeeze=False, sharey=True)
    presets = [p for p in rb.PRESETS if p in set(ad["preset"])]
    labels = [p.replace("_", " ").title() for p in presets]
    for ax, setup in zip(axes[0], setups):
        left = [0.0] * len(labels)
        for name in SETTINGS_NAMES:
            vals = []
            for p in presets:
                r = _cell(ad, setup, "adaptive_signals", p)
                vals.append(0.0 if r is None else float(r.get(f"{name}_pct", 0) or 0))
            ax.barh(range(len(labels)), vals, left=left, color=SETTING_COLORS[name], label=name)
            left = [a + b for a, b in zip(left, vals)]
        ax.set_title(setup, fontsize=10)
        ax.set_xlim(0, 100)
        ax.set_xlabel("% of run time")
        ax.set_yticks(range(len(labels)))
        ax.set_yticklabels(labels, fontsize=7)
        ax.invert_yaxis()
    handles, names = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, names, fontsize=8, loc="upper center", ncol=3, bbox_to_anchor=(0.5, 1.06))
    fig.tight_layout()
    _save(fig, out, "settings_used.png")
    return fig


def figure_staleness(st, out):
    import matplotlib.pyplot as plt
    import numpy as np

    setups = list(dict.fromkeys(st["setup"]))
    configs = list(dict.fromkeys(st["config"]))
    fig, ax = plt.subplots(figsize=(10, 3.6))
    width = 0.8 / len(configs)
    colors = {**COLORS, "adaptive_hot": "#c0392b", "adaptive_cold": "#2ea043"}
    for i, c in enumerate(configs):
        vals = [st[(st.setup == s) & (st.config == c)]["stale_pct"].sum() for s in setups]
        ax.bar(np.arange(len(setups)) + (i - (len(configs) - 1) / 2) * width, vals, width, label=c, color=colors.get(c))
    ax.set_xticks(range(len(setups)))
    ax.set_xticklabels(setups)
    ax.set_ylabel("stale reads (%)")
    ax.set_title("Read-after-write staleness experiment", fontsize=10, loc="left")
    ax.legend(fontsize=8, ncol=len(configs))
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    fig.tight_layout()
    _save(fig, out, "staleness_experiment.png")
    return fig


def analyse(out, plan):
    """Load everything, write the tables and figures, return what main() prints."""
    out = Path(out)
    df = load_summary(out)
    st = load_staleness(out)
    checks = json.loads((out / "checks.json").read_text()) if (out / "checks.json").exists() else {}
    if not df.empty:
        df.to_csv(out / "summary.csv", index=False)
    if not st.empty:
        st.to_csv(out / "staleness.csv", index=False)
    integ = integrity(out, plan, df, checks)
    expect = expectations(df) if not df.empty else []
    with open(out / "expectations.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["kind", "item", "verdict", "detail"])
        w.writerows([("integrity", n, ok, d) for n, ok, d in integ] + [("expectation", n, v, d) for n, v, d in expect])
    return {"summary": df, "staleness": st, "checks": checks, "integrity": integ, "expectations": expect}


# --- command line ---------------------------------------------------------------------------------

PROFILES = {
    "FULL": dict(),  # the Plan defaults: 3 setups x 5 configs x 6 core workloads, 0.6 x full length, 1 repeat
    "FULL_ALL": dict(presets=list(rb.PRESETS)),  # the same with all 10 workloads (~6 h 10 min)
    "SMOKE": dict(setups=["EQUAL-0", "T1"], presets=["READ_HEAVY", "BALANCED"], fixed_duration_s=30,
                  staleness_setups=["EQUAL-0"], staleness_trials=20),
}


def write_figures(out, res):
    """The run's figures in figs/, from what analyse() returned."""
    import matplotlib

    matplotlib.use("Agg")  # files only, no window
    import matplotlib.pyplot as plt

    df, st = res["summary"], res["staleness"]
    if not df.empty:
        figure_metric(df, out, "read_p50", "read p50 (ms, log)", "read_p50.png", log_scale=True)
        figure_metric(df, out, "write_p50", "write p50 (ms, log)", "write_p50.png", log_scale=True)
        figure_metric(df, out, "stale_pct", "stale probes (%)", "stale_probes.png", value_labels=True)
        figure_settings(df, out)
    if not st.empty:
        figure_staleness(st, out)
    plt.close("all")


def main(argv=None):
    import argparse
    import datetime as dt

    from benchmark import signals_report

    ap = argparse.ArgumentParser(description="Run the full evaluation of the signal rules (see the module docstring).")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="FULL")
    ap.add_argument("--tag", help="resume this run (results/signals_eval/<tag>/) instead of starting a new one")
    ap.add_argument("--continue-on-check-fail", action="store_true",
                    help="start the load tests even if a live check fails")
    args = ap.parse_args(argv)

    plan = Plan(**PROFILES[args.profile])
    tag = args.tag or f"{args.profile}_{dt.datetime.now():%Y-%m-%d_%H%M}"
    out = out_dir_for(tag)
    out.mkdir(parents=True, exist_ok=True)
    log = Log(out / "run_log.txt")
    rows, total = estimate(plan)
    done = sum(is_done(out, *c) for c in plan.cells())
    write_meta(out, plan, tag=tag, profile=args.profile, started=dt.datetime.now().isoformat(timespec="seconds"))
    log.say(f"run {tag} -> {out}")
    log.say(f"{len(plan.cells())} load-test runs planned, {done} already done; estimate "
            + ", ".join(f"{n} {fmt_hours(s)}" for n, s in rows) + f"; total {fmt_hours(total)} "
            f"(done around {dt.datetime.now() + dt.timedelta(seconds=total):%a %H:%M})")

    t0 = time.monotonic()
    pre = preflight(out)
    for name, ok, detail in pre:
        log.say(f"preflight {'ok  ' if ok else 'FAIL'} {name}: {detail}")
    write_meta(out, plan, phase_seconds={"preflight": round(time.monotonic() - t0)})
    if not all(ok for _, ok, _ in pre):
        log.say("preflight failed: fix the check(s) above before the long runs")
        return 1

    t0 = time.monotonic()
    checks = run_live_checks(out, log) if plan.run_checks else {}
    write_meta(out, plan, phase_seconds={"live_checks": round(time.monotonic() - t0)})
    if checks and not all(v["pass"] for v in checks.values()) and not args.continue_on_check_fail:
        log.say("a live check failed: not starting the load tests (--continue-on-check-fail to go on anyway)")
        return 1

    t0 = time.monotonic()
    ran = run_load_tests(out, plan, log)
    write_meta(out, plan, phase_seconds={"load_tests": round(time.monotonic() - t0)})
    log.say(f"load tests finished: {ran} runs this session in {fmt_hours(time.monotonic() - t0)}")

    t0 = time.monotonic()
    run_staleness(out, plan, log)
    write_meta(out, plan, phase_seconds={"staleness": round(time.monotonic() - t0)},
               finished=dt.datetime.now().isoformat(timespec="seconds"))

    res = analyse(out, plan)
    write_figures(out, res)
    verdict ={True: "yes", False: "NO", None: "not in this run"}
    print("\nIntegrity:")
    for name, ok, detail in res["integrity"]:
        print(f"  {verdict[ok]:<15} {name}: {detail}")
    print("\nExpectations (fixed before the run):")
    for name, held, detail in res["expectations"]:
        print(f"  {verdict[held]:<15} {name}: {detail}")
    summary = signals_report.summarise(out)
    if summary:
        print("\n" + signals_report.to_markdown(summary))
    if not res["staleness"].empty:
        print("\nStaleness experiment (stale reads %):")
        print(res["staleness"].pivot_table(index="config", columns="setup", values="stale_pct", sort=False).round(1))
    write_meta(out, plan, analysed=dt.datetime.now().isoformat(timespec="seconds"))
    print(f"\nResults, tables and figures: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
