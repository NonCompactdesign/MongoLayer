"""Benchmark Runner (Phase 9): orchestrates every (configuration x workload
preset) combination through Locust, headlessly, capturing Locust's own
per-request-type stats to results/*.csv.

Configurations compared (see PROJECT/vault "Why the Evaluation Is
Structured" for the reasoning): static_fast (w=1/local - fast/stale
baseline), static_safe (majority/majority - safe/slow baseline), adaptive
(this project). Each is driven by the exact same benchmark/locustfile.py
across the exact same three workload presets, so the comparison is fair.

Usage:
    python benchmark/run_benchmark.py --quick   # fast iteration while developing
    python benchmark/run_benchmark.py           # full-length run for real numbers (Phase 11)

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

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCUSTFILE = REPO_ROOT / "benchmark" / "locustfile.py"
RESULTS_DIR = REPO_ROOT / "results"

CONFIGS = ["static_fast", "static_safe", "adaptive"]
PRESETS = ["READ_HEAVY", "WRITE_HEAVY", "MIXED_BURSTY"]

# MIXED_BURSTY oscillates on a 15s period (see locustfile.py) - a run needs
# to be comfortably longer than that to show the pattern shift at all.
QUICK = {"users": 5, "spawn_rate": 5, "duration": "20s"}
FULL = {"users": 20, "spawn_rate": 10, "duration": "60s"}


def run_one(config, preset, params):
    collection = f"bench_{config}_{preset}".lower()
    csv_prefix = RESULTS_DIR / f"{config}_{preset}"

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
    cmd = [
        sys.executable, "-m", "locust",
        "-f", str(LOCUSTFILE),
        "--headless",
        "-u", str(params["users"]),
        "-r", str(params["spawn_rate"]),
        "-t", params["duration"],
        "--csv", str(csv_prefix),
        "--only-summary",
    ]

    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quick", action="store_true",
        help="short duration/low load, for fast iteration while developing (not for real numbers)",
    )
    args = parser.parse_args()
    params = QUICK if args.quick else FULL

    RESULTS_DIR.mkdir(exist_ok=True)

    print(f"Benchmark runner: {'QUICK' if args.quick else 'FULL'} mode "
          f"({len(CONFIGS)} configs x {len(PRESETS)} presets = {len(CONFIGS) * len(PRESETS)} runs)")

    results = {}
    for config in CONFIGS:
        for preset in PRESETS:
            results[(config, preset)] = run_one(config, preset, params)

    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    header = f"{'config':<12} {'preset':<14} {'reqs':>6} {'fail':>5} {'req/s':>8} {'p50':>6} {'p95':>6} {'p99':>6}"
    print(header)
    for (config, preset), summary in results.items():
        if summary is None:
            print(f"{config:<12} {preset:<14} {'FAILED':>6}")
            continue
        print(
            f"{config:<12} {preset:<14} {summary['requests']:>6} {summary['failures']:>5} "
            f"{summary['req_per_s']:>8.2f} {summary['p50_ms']:>6.0f} {summary['p95_ms']:>6.0f} {summary['p99_ms']:>6.0f}"
        )

    any_failed = any(s is None or s["failures"] > 0 for s in results.values())
    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
