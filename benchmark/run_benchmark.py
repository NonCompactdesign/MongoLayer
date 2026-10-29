"""Benchmark Runner (Phase 9): orchestrates every (configuration x workload
preset) combination through Locust, headlessly, capturing Locust's own
per-request-type stats to results/*.csv.

Configurations compared (see PROJECT/vault "Why the Evaluation Is
Structured" for the reasoning): static_fast (w=1/local - fast/stale
baseline), static_safe (majority/majority - safe/slow baseline), adaptive
(this project). Each is driven by the exact same benchmark/locustfile.py
across the exact same eight workload presets (READ_HEAVY, WRITE_HEAVY,
MIXED_BURSTY, BALANCED, HOTSPOT, RAMP, SPIKE, HIGH_CONCURRENCY - see
locustfile.py for what each one drives), so the comparison is fair. Most
presets share one steady load shape (users/spawn_rate/duration); RAMP,
SPIKE, and HIGH_CONCURRENCY each override that shape (see
FULL_OVERRIDES/QUICK_OVERRIDES below) because their signal specifically
depends on a different load shape, not just a different read/write mix.

Every (config x preset) matrix is repeated at each injected inter-node
latency level (see testbed/netem.py for the levels and their sources) and
the whole sweep is repeated --repeats times, to report spread and not a single
run. Results land in results/write_share_eval/latency_<N>ms/rep<k>/ (a new
folder so the earlier single-run results in results/ are never overwritten).
Adaptive runs also write <config>_<preset>_switches.csv: what settings the
feedback loop chose and when. Every run writes <config>_<preset>_probe.csv: the
stale-read tally of the staleness probes mixed into the traffic (benchmark/probe_log.py).

Configurations: static_fast, static_moderate, static_safe (fixed settings) and adaptive_signals
(the adaptive layer: traffic rule + hysteresis + SAFE latency budget + per-key
read-your-own-writes, adaptive_layer/rules.py and client.py).
--topology T1 T2 runs on unequal client/node distances (testbed/topology.py) instead of
the equal --latency levels, after pinning the primary to mongo1 (testbed/cluster.py);
those results land in results/signals_eval/topology_<name>/rep<k>/.

Usage:
    python -m benchmark.run_benchmark --quick                  # fast iteration, 1 repeat
    python -m benchmark.run_benchmark --topology T1 --configs static_fast adaptive_signals \
        --presets READ_HEAVY --duration 60s --repeats 1        # one small targeted run
    python -m benchmark.run_benchmark --latency 0 5            # only these one-way delays (ms)
    python -m benchmark.run_benchmark --repeats 3              # full-length run, 3 repeats (default)
    python -m benchmark.run_benchmark --duration-scale 0.4     # shorter runs (300 s -> 120 s)

Each combination runs as a separate `locust` subprocess rather than being
imported in-process - locust monkey-patches ssl via gevent on import, which
can conflict with pymongo's own ssl usage if both are imported in the same
process (see Phase 0's progress log for this exact gotcha). Shelling out
avoids it entirely and also gets clean process isolation between runs.
"""

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path

from testbed import netem, topology

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCUSTFILE = REPO_ROOT / "benchmark" / "locustfile.py"
RESULTS_DIR = REPO_ROOT / "results"

CONFIGS = ["static_fast", "static_moderate", "static_safe", "adaptive_signals"]
ADAPTIVE_CONFIGS = ("adaptive_signals",)  # the ones with a feedback loop (and a switch log)
PRESETS = [
    "READ_HEAVY", "WRITE_HEAVY", "MIXED_BURSTY",
    "BALANCED", "HOTSPOT", "RAMP", "SPIKE", "HIGH_CONCURRENCY", "MODEST_WRITES", "READ_HEAVY_HOTSPOT",
]

# Baseline load shape for the "steady" presets (READ_HEAVY, WRITE_HEAVY,
# MIXED_BURSTY, BALANCED, HOTSPOT). MIXED_BURSTY oscillates on a 15s period
# (see locustfile.py) - a run needs to be comfortably longer than that to
# show the pattern shift at all; the other steady presets reuse the same
# shape for a like-for-like comparison.
QUICK_DEFAULT = {"users": 10, "spawn_rate": 5, "duration": "20s"}
FULL_DEFAULT = {"users": 50, "spawn_rate": 20, "duration": "300s"}

# Per-preset overrides: RAMP/SPIKE/HIGH_CONCURRENCY each need a different
# load shape than the steady default to actually demonstrate what they're
# testing (see docstring below and the plan this came from).
QUICK_OVERRIDES = {
    "RAMP": {"users": 10, "spawn_rate": 1, "duration": "20s"},
    "SPIKE": {"users": 10, "spawn_rate": 5, "duration": "40s"},
    "HIGH_CONCURRENCY": {"users": 40, "spawn_rate": 20, "duration": "20s"},
}
FULL_OVERRIDES = {
    # Same users/duration as the steady default, but a much slower ramp
    # (spawn_rate=1 instead of 20) so reaching full concurrency takes ~50s
    # instead of ~2.5s - tests whether the FeedbackLoop keeps up while load
    # is still building, not just after it's already steady.
    "RAMP": {"users": 50, "spawn_rate": 1, "duration": "300s"},
    # Same users/spawn_rate as the steady default, longer duration so
    # several 20s spike cycles (see locustfile.py's SPIKE preset) fit in
    # one run.
    "SPIKE": {"users": 50, "spawn_rate": 20, "duration": "180s"},
    # Same shape as the steady default but far more concurrent users, to
    # surface any overhead in the adaptive layer's own bookkeeping
    # (monitor locking, feedback loop polling) that only shows up under load.
    "HIGH_CONCURRENCY": {"users": 200, "spawn_rate": 50, "duration": "300s"},
}


def params_for(preset, quick, duration_scale=1.0):
    """Load shape for one preset; duration_scale shrinks or stretches the run length."""
    overrides = QUICK_OVERRIDES if quick else FULL_OVERRIDES
    default = QUICK_DEFAULT if quick else FULL_DEFAULT
    params = dict(overrides.get(preset, default))
    if duration_scale != 1.0:
        seconds = int(params["duration"].rstrip("s"))
        params["duration"] = f"{max(30, round(seconds * duration_scale))}s"
    return params


EVAL_DIR = RESULTS_DIR / "write_share_eval"


SIGNALS_DIR = RESULTS_DIR / "signals_eval"


def results_dir_for(latency_ms, rep=1):
    return EVAL_DIR / f"latency_{latency_ms}ms" / f"rep{rep}"


def topology_dir_for(name, rep=1):
    return SIGNALS_DIR / f"topology_{name}" / f"rep{rep}"


def _kill_tree(proc):
    """Kill a process and everything it started. On Windows the venv's python.exe is a launcher
    that starts the real interpreter as a child, so killing only `proc` would leave Locust running."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


_current = None  # the Locust process running now (for stop_current)


def stop_current():
    """Kill the Locust run in progress, if any (the dashboard's Stop); run_one then returns None."""
    proc = _current
    if proc is not None and proc.poll() is None:
        _kill_tree(proc)


def _run_with_watchdog(cmd, env, timeout_s):
    """subprocess.run(...) that kills the whole process tree after timeout_s.
    Returns a CompletedProcess, or None if it had to be killed."""
    global _current
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _current = proc
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()
        return None
    finally:
        _current = None
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def run_one(config, preset, params, results_dir=RESULTS_DIR, timeout_s=None):
    """One Locust run. With timeout_s, a run that has not exited by then is killed (a watchdog:
    on 2026-10-19 one 180 s run never exited and blocked an overnight evaluation for 6 hours)."""
    collection = f"bench_{config}_{preset}".lower()
    csv_prefix = Path(results_dir) / f"{config}_{preset}"

    print()
    print("-" * 72)
    print(f"Running config={config!r} preset={preset!r} "
          f"(users={params['users']}, spawn_rate={params['spawn_rate']}, duration={params['duration']})")
    print("-" * 72)

    env = {
        "BENCHMARK_MODE": config,
        "WORKLOAD_PRESET": preset,
        "BENCHMARK_COLLECTION": collection,
    }
    if config in ADAPTIVE_CONFIGS:
        env["BENCHMARK_SWITCH_LOG"] = f"{csv_prefix}_switches.csv"
    if config == "adaptive_signals":
        env["BENCHMARK_CLIENT_LOG"] = f"{csv_prefix}_client.json"
    env["BENCHMARK_PROBE_LOG"] = f"{csv_prefix}_probe.csv"
    cmd = [
        sys.executable, "-m", "locust",
        "-f", str(LOCUSTFILE),
        "--headless",
        "-u", str(params["users"]),
        "-r", str(params["spawn_rate"]),
        "-t", params["duration"],
        "--csv", str(csv_prefix),
        "--csv-full-history",  # read / write rows in _stats_history.csv too (the dashboard's live charts)
        "--only-summary",
        # failed requests are counted from the stats CSV; a non-zero exit then means Locust itself broke
        "--exit-code-on-error", "0",
    ]

    if timeout_s is None:
        result = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env={**os.environ, **env},
            capture_output=True,
            text=True,
        )
    else:
        result = _run_with_watchdog(cmd, {**os.environ, **env}, timeout_s)
        if result is None:
            print(f"  !! locust did not finish within {timeout_s:.0f} s - killed it (and its child processes)")
            return None
    if result.returncode != 0:
        print(f"  !! locust exited with code {result.returncode}")
        print(result.stdout[-2000:])
        print(result.stderr[-2000:])
        return None

    return _summarize_stats_csv(csv_prefix)


def _summarize_stats_csv(csv_prefix):
    stats_path = Path(f"{csv_prefix}_stats.csv")
    if not stats_path.exists():
        print(f"  !! expected {stats_path} was not produced")
        return None

    with open(stats_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    aggregated = next((r for r in rows if r["Name"] == "Aggregated"), None)
    if aggregated is None:
        print("  !! no 'Aggregated' row found in stats CSV")
        return None

    summary = {
        "requests": int(aggregated["Request Count"]),
        "failures": int(aggregated["Failure Count"]),
        "req_per_s": float(aggregated["Requests/s"]),
        "p50_ms": float(aggregated["50%"]),
        "p95_ms": float(aggregated["95%"]),
        "p99_ms": float(aggregated["99%"]),
    }
    print(
        f"  requests={summary['requests']} failures={summary['failures']} "
        f"req/s={summary['req_per_s']:.2f} "
        f"p50={summary['p50_ms']:.0f}ms p95={summary['p95_ms']:.0f}ms p99={summary['p99_ms']:.0f}ms"
    )
    if summary["failures"] > 0:
        print(f"  !! {summary['failures']} failed requests - inspect {csv_prefix}_failures.csv")
    return summary


def run_suite(latency_ms, quick, presets=PRESETS, configs=CONFIGS, rep=1, duration_scale=1.0,
              topo=None, duration=None):
    """Run every (config x preset) at one injected latency level, or on topology `topo`
    when given. Always clears the injected delays afterwards so the cluster is never left
    slowed. `duration` ("60s") overrides every run's length."""
    results_dir = topology_dir_for(topo.name, rep) if topo is not None else results_dir_for(latency_ms, rep)
    results_dir.mkdir(parents=True, exist_ok=True)
    if topo is not None:
        topology.apply(topo)
    else:
        netem.apply_latency(latency_ms)
    try:
        results = {}
        for config in configs:
            for preset in presets:
                params = params_for(preset, quick, duration_scale)
                if duration:
                    params["duration"] = duration
                results[(config, preset)] = run_one(config, preset, params, results_dir)
        return results
    finally:
        netem.clear()


def print_summary(latency_ms, results, rep=1, label=None):
    print()
    print("=" * 72)
    print(f"SUMMARY - {label or f'inter-node one-way delay {latency_ms} ms'}, repeat {rep}")
    print("=" * 72)
    print(f"{'config':<17} {'preset':<19} {'reqs':>6} {'fail':>5} {'req/s':>8} {'p50':>6} {'p95':>6} {'p99':>6}")
    for (config, preset), summary in results.items():
        if summary is None:
            print(f"{config:<17} {preset:<19} {'FAILED':>6}")
            continue
        print(
            f"{config:<17} {preset:<19} {summary['requests']:>6} {summary['failures']:>5} "
            f"{summary['req_per_s']:>8.2f} {summary['p50_ms']:>6.0f} {summary['p95_ms']:>6.0f} {summary['p99_ms']:>6.0f}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quick", action="store_true",
        help="short duration/low load, for fast iteration while developing (not for real numbers)",
    )
    parser.add_argument(
        "--latency", nargs="+", type=int, default=netem.LATENCY_LEVELS_MS,
        help="one-way inter-node delays in ms to sweep (default: %(default)s)",
    )
    parser.add_argument("--repeats", type=int, default=None, help="repeats of the whole sweep (default: 3, or 1 with --quick)")
    parser.add_argument("--duration-scale", type=float, default=1.0, help="multiply every run length (default: 1.0)")
    parser.add_argument("--duration", default=None, help='fixed length for every run, e.g. "60s" (overrides the tables)')
    parser.add_argument("--topology", nargs="+", default=None,
                        help="run on these topologies (T1, T2, EQUAL-<ms>) instead of the --latency levels")
    parser.add_argument("--configs", nargs="+", default=CONFIGS, choices=CONFIGS, help="configurations to run")
    parser.add_argument("--presets", nargs="+", default=PRESETS, choices=PRESETS, help="workload presets to run")
    args = parser.parse_args()
    repeats = args.repeats if args.repeats is not None else (1 if args.quick else 3)

    if args.topology:
        from testbed import cluster
        cluster.pin_primary()  # "far from the primary" needs a known primary
        levels = [(None, topology.by_name(name)) for name in args.topology]
    else:
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        levels = [(ms, None) for ms in args.latency]
    total = repeats * len(levels) * len(args.configs) * len(args.presets)
    print(f"Benchmark runner: {'QUICK' if args.quick else 'FULL'} mode - {repeats} repeats x {len(levels)} network "
          f"levels x {len(args.configs)} configs x {len(args.presets)} presets = {total} runs")

    any_failed = False
    for rep in range(1, repeats + 1):
        for latency_ms, topo in levels:
            label = (f"topology {topo.name} ({topo.description})" if topo is not None else
                     f"latency {latency_ms} ms one-way ({netem.LATENCY_LABELS.get(latency_ms, 'custom')})")
            print(f"\n##### repeat {rep}/{repeats}, {label} #####")
            results = run_suite(latency_ms, args.quick, presets=args.presets, configs=args.configs, rep=rep,
                                duration_scale=args.duration_scale, topo=topo, duration=args.duration)
            print_summary(latency_ms, results, rep, label)
            any_failed |= any(v is None or v["failures"] > 0 for v in results.values())

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
