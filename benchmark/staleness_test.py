"""Staleness Measurement (Phase 10): directly measure read-after-write
staleness per configuration, rather than only inferring it from latency
numbers - see the project vault's Design Rationale for why this needs its
own explicit experiment.

Method: write a unique marker, immediately issue a secondaryPreferred-style
read for it (whatever read preference the config's active settings specify),
and check whether the read actually returned that marker. A "miss" means
the read landed on a replica that hadn't caught up yet.

Four scenarios measured:
  static_fast    - FAST_SETTINGS permanently active, no adaptation
  static_safe    - SAFE_SETTINGS permanently active, no adaptation
  adaptive_hot   - real FeedbackLoop, warmed into its SAFE_SETTINGS branch
                   (write-heavy: high write share) before measuring
  adaptive_cold  - real FeedbackLoop, warmed into its FAST_SETTINGS branch
                   (read-heavy: low write share) before measuring, and kept
                   there throughout measurement via filler reads between
                   trials (a bare write+read trial loop is 50% writes, which
                   never satisfies the read-heavy branch on its own - see
                   decide()'s LOW_WRITE_SHARE)

Each trial also records which settings were actually in force when its read
ran, so the output states how many trials really ran under the intended
settings instead of leaving that to inference (in_expected_settings).

Expectation (from the project's design rationale): adaptive_hot's staleness
rate should land close to static_safe's, and adaptive_cold's should land
close to static_fast's - i.e. the adaptive layer's rate sits between the
two static baselines *in the direction its current settings would predict*,
not literally "always in the middle."

Repeated at each injected inter-node latency level (benchmark/netem.py) and
for --repeats repeats; results land in
results/write_share_eval/latency_<N>ms/rep<k>/staleness.csv.

Usage:
    python -m benchmark.staleness_test
    python -m benchmark.staleness_test --quick          # smaller trial counts, 1 repeat
    python -m benchmark.staleness_test --latency 0 43   # only these one-way delays (ms)
    python -m benchmark.staleness_test --repeats 3      # default
"""

import argparse
import csv
import time
import uuid
from pathlib import Path

from adaptive_layer.client import AdaptiveClient
from benchmark import netem
from benchmark.run_benchmark import results_dir_for
from adaptive_layer.decision import FAST_SETTINGS, SAFE_SETTINGS
from adaptive_layer.feedback import FeedbackLoop
from adaptive_layer.monitor import AccessPatternMonitor

URI = "mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0"
DB_NAME = "staleness_test"
RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"

WINDOW_SECONDS = 6
INTERVAL_SECONDS = 2
COOLDOWN_SECONDS = 3

FULL_TRIALS = 200
QUICK_TRIALS = 40
COLD_TRIAL_SPACING_SECONDS = 2  # gives secondaries time to catch up between isolated writes
COLD_FILLER_READS_PER_TRIAL = 9  # 1 write per 11 operations (~9% writes), comfortably under LOW_WRITE_SHARE (0.15)


def _wait_until_settings(client, collection, expected, timeout=15, poll_interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get_active_settings(collection) == expected:
            return True
        time.sleep(poll_interval)
    return False


def _one_trial(client, collection, seq):
    """Write a unique marker, immediately read it back. Returns (fresh,
    settings): fresh is True if the read found the marker and False if it
    missed (stale); settings are the active settings the read ran under."""
    marker = f"{seq}-{uuid.uuid4()}"
    client.write(collection, {"marker": marker, "seq": seq})
    settings = client.get_active_settings(collection)
    doc = client.read(collection, {"marker": marker})
    return (doc is not None and doc.get("marker") == marker), settings


def measure_static(config_name, settings, collection, trials):
    print(f"\n--- {config_name} ({trials} trials, no warmup needed - settings are fixed) ---")
    client = AdaptiveClient(URI, DB_NAME, verbose=False)
    client.set_active_settings(collection, settings)
    try:
        hits = in_expected = 0
        for i in range(trials):
            fresh, used = _one_trial(client, collection, i)
            hits += fresh
            in_expected += used == settings
    finally:
        client._db.drop_collection(collection)
        client.close()
    return _report(config_name, trials, hits, in_expected)


def measure_adaptive_hot(collection, trials, warmup_writes=60):
    print(f"\n--- adaptive_hot ({trials} trials) ---")
    print(f"Warming up: {warmup_writes} writes to push the write share above HIGH_WRITE_SHARE...")
    monitor = AccessPatternMonitor(window_seconds=WINDOW_SECONDS)
    client = AdaptiveClient(URI, DB_NAME, monitor=monitor, verbose=False)
    loop = FeedbackLoop(client, monitor, interval_seconds=INTERVAL_SECONDS, cooldown_seconds=COOLDOWN_SECONDS)
    loop.start()
    try:
        for i in range(warmup_writes):
            client.write(collection, {"warmup": i})
        if not _wait_until_settings(client, collection, SAFE_SETTINGS):
            print("  !! never reached SAFE_SETTINGS during warmup - measuring anyway, "
                  "but treat this result with suspicion")
        else:
            print("  reached SAFE_SETTINGS, measuring (trials themselves keep it hot)...")

        hits = in_expected = 0
        for i in range(trials):
            fresh, used = _one_trial(client, collection, i)
            hits += fresh
            in_expected += used == SAFE_SETTINGS
    finally:
        loop.stop()
        client._db.drop_collection(collection)
        client.close()
    return _report("adaptive_hot", trials, hits, in_expected)


def measure_adaptive_cold(collection, trials, warmup_reads=30):
    print(f"\n--- adaptive_cold ({trials} trials, spaced {COLD_TRIAL_SPACING_SECONDS}s apart - this takes a while) ---")
    print(f"Warming up: seed write + {warmup_reads} reads to push the write share below LOW_WRITE_SHARE...")
    monitor = AccessPatternMonitor(window_seconds=WINDOW_SECONDS)
    client = AdaptiveClient(URI, DB_NAME, monitor=monitor, verbose=False)
    loop = FeedbackLoop(client, monitor, interval_seconds=INTERVAL_SECONDS, cooldown_seconds=COOLDOWN_SECONDS)
    loop.start()
    try:
        client.write(collection, {"seed": True})
        for _ in range(warmup_reads):
            client.read(collection, {})
        if not _wait_until_settings(client, collection, FAST_SETTINGS):
            print("  !! never reached FAST_SETTINGS during warmup - measuring anyway, "
                  "but treat this result with suspicion")
        else:
            print("  reached FAST_SETTINGS, measuring (with filler reads to stay cold)...")

        hits = in_expected = 0
        for i in range(trials):
            fresh, used = _one_trial(client, collection, i)
            hits += fresh
            in_expected += used == FAST_SETTINGS
            for _ in range(COLD_FILLER_READS_PER_TRIAL):
                client.read(collection, {})
            time.sleep(COLD_TRIAL_SPACING_SECONDS)
    finally:
        loop.stop()
        client._db.drop_collection(collection)
        client.close()
    return _report("adaptive_cold", trials, hits, in_expected)


def _report(config_name, trials, hits, in_expected):
    misses = trials - hits
    stale_rate = misses / trials
    print(f"  {config_name}: {hits}/{trials} fresh, {misses}/{trials} stale ({stale_rate:.1%} stale rate); "
          f"{in_expected}/{trials} trials ran under the intended settings")
    return {
        "config": config_name, "trials": trials, "hits": hits, "misses": misses,
        "stale_rate": stale_rate, "in_expected_settings": in_expected,
    }


def run_all(trials):
    """The four staleness scenarios at whatever latency is currently injected."""
    results = [
        measure_static("static_fast", FAST_SETTINGS, "staleness_static_fast", trials),
        measure_static("static_safe", SAFE_SETTINGS, "staleness_static_safe", trials),
        measure_adaptive_hot("staleness_adaptive_hot", trials),
        # adaptive_cold is paced (COLD_TRIAL_SPACING_SECONDS per trial) - use fewer
        # trials by default even in "full" mode, or this scenario alone dominates
        # total runtime for no real statistical benefit.
        measure_adaptive_cold("staleness_adaptive_cold", min(trials, 60)),
    ]
    return results


def run_suite(latency_ms, trials, rep=1):
    """Run all scenarios at one latency level and write its staleness.csv.
    Always clears the injected latency afterwards."""
    netem.apply_latency(latency_ms)
    try:
        results = run_all(trials)
    finally:
        netem.clear()

    out_dir = results_dir_for(latency_ms, rep)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "staleness.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["config", "trials", "hits", "misses", "stale_rate", "in_expected_settings"])
        writer.writeheader()
        writer.writerows(results)
    print(f"\nWrote {out_path}")
    return results


def print_summary(latency_ms, results, rep=1):
    print("\n" + "=" * 60)
    print(f"SUMMARY - inter-node one-way delay {latency_ms} ms, repeat {rep}")
    print("=" * 60)
    print(f"{'config':<16} {'trials':>7} {'stale':>7} {'stale_rate':>11} {'intended settings':>18}")
    for r in results:
        print(f"{r['config']:<16} {r['trials']:>7} {r['misses']:>7} {r['stale_rate']:>10.1%} "
              f"{r['in_expected_settings']:>12}/{r['trials']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="fewer trials, for fast iteration")
    parser.add_argument(
        "--latency", nargs="+", type=int, default=netem.LATENCY_LEVELS_MS,
        help="one-way inter-node delays in ms to sweep (default: %(default)s)",
    )
    parser.add_argument("--repeats", type=int, default=None, help="repeats of the whole sweep (default: 3, or 1 with --quick)")
    args = parser.parse_args()
    trials = QUICK_TRIALS if args.quick else FULL_TRIALS
    repeats = args.repeats if args.repeats is not None else (1 if args.quick else 3)

    for rep in range(1, repeats + 1):
        for latency_ms in args.latency:
            print(f"\n##### repeat {rep}/{repeats}, latency {latency_ms} ms one-way "
                  f"({netem.LATENCY_LABELS.get(latency_ms, 'custom')}) #####")
            print_summary(latency_ms, run_suite(latency_ms, trials, rep), rep)

    print("\nNote: adaptive_cold's trials are deliberately spaced out, so it is NOT directly comparable "
          "to static_fast; adaptive_hot and static_safe are both under sustained write pressure and are. "
          "The 'intended settings' column says how many trials really ran under the settings each "
          "scenario is named for.")


if __name__ == "__main__":
    main()
