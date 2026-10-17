# Running Guide — Locust Benchmarks

A practical, command-by-command guide to running this project's **Locust-based benchmark harness** (headless, produces numbers for the report) — plus exactly where every result file ends up and what's in it.

This guide assumes the one-time setup in [`SHOWCASE.md`](SHOWCASE.md) / [`replica_set_setup.md`](replica_set_setup.md) is already done (repo cloned, venv created, `requirements.txt` installed, hosts file entries added). If not, do that first.

---

## 0. Every session: bring the replica set up

The Locust harness needs the real 3-node replica set running — nothing meaningful happens against a standalone `mongod`.

```bash
docker compose up -d --build
```

(`--build` is needed once, and after any change to `docker/mongo/Dockerfile`: it adds `tc` to the MongoDB image so the benchmark can inject inter-node network latency.)

```bash
docker exec mongo1 mongosh --port 27017 --quiet --eval '
var s = rs.status();
s.members.forEach(function(m) { print(m.name + " -> " + m.stateStr); });
'
```

Expect one `PRIMARY`, two `SECONDARY`. If it errors with `not primary`, retry the same command against `mongo2`/`mongo3` — whichever one actually holds primary right now will answer.

Then activate the virtual environment in every terminal you use for Python commands:

```bash
source .venv/Scripts/activate
```

(Optional sanity check before doing anything else: `python -m pytest tests/ -q` should print all tests passing.)

---

## 1. Scripted console demo

```bash
python demo_app.py
```

Runs three fixed phases (read-heavy → write-burst → mixed) against a throwaway `demo.live_demo` collection and prints the settings adapting to the console. Takes about a minute; cleans up its own collection when done. A saved example transcript is at [`docs/demo_runs/demo_run_2026-09-10.log`](demo_runs/demo_run_2026-09-10.log).

---

## 2. Locust-based benchmark harness

This is the path that produces the numbers for the DA-III report: latency/throughput comparisons across three configurations (`static_fast`, `static_safe`, `adaptive`) and nine workload shapes (`READ_HEAVY`, `WRITE_HEAVY`, `MIXED_BURSTY`, `BALANCED`, `HOTSPOT`, `RAMP`, `SPIKE`, `HIGH_CONCURRENCY`, `MODEST_WRITES`), plus a separate staleness measurement. `experiments.ipynb` at the repo root runs this same harness plus the staleness test and renders tables/charts inline — see its intro cell.

### 2.1 Quick manual sanity run (one config, one preset, ad hoc)

Useful when developing/debugging the workload generator itself, without going through the full orchestrator:

```bash
locust -f benchmark/locustfile.py --headless -u 10 -r 5 -t 30s
```

- `-u 10` — 10 simulated users
- `-r 5` — spawn rate (users/sec)
- `-t 30s` — run for 30 seconds

Reads its behavior from environment variables (all optional, shown with defaults):

```bash
BENCHMARK_MODE=adaptive WORKLOAD_PRESET=MIXED_BURSTY BENCHMARK_COLLECTION=bench BENCHMARK_KEY_SPACE=1000 BENCHMARK_OPS_PER_SEC=5 \
locust -f benchmark/locustfile.py --headless -u 10 -r 5 -t 30s
```

- `BENCHMARK_MODE`: `static_fast` | `static_safe` | `adaptive`
- `WORKLOAD_PRESET`: `READ_HEAVY` (5% writes) | `WRITE_HEAVY` (95% writes) | `MIXED_BURSTY` (oscillates every 15s) | `BALANCED` (50% writes) | `HOTSPOT` (50% writes, ~80% of ops hit a small hot key subset) | `RAMP` (50% writes; signal is in `run_benchmark.py`'s slow spawn rate, not this env var) | `SPIKE` (short write spikes to 90% every 20s) | `HIGH_CONCURRENCY` (50% writes; signal is in `run_benchmark.py`'s much higher user count)
- `BENCHMARK_COLLECTION`: which Mongo collection the traffic hits
- `BENCHMARK_KEY_SPACE`: number of distinct synthetic keys read/written
- `BENCHMARK_OPS_PER_SEC`: per-simulated-user target rate

This ad hoc form prints Locust's live stats to the console but does **not** write any CSVs unless you add `--csv <prefix>` yourself.

### 2.2 Full orchestrated run (24 combinations per latency level, writes CSVs)

This is the normal way to run the benchmark. It drives all 3 configurations x 9 presets **at each injected inter-node latency level** (default one-way delays 0, 5 and 43 ms; 1 and 33 ms are also available, see `benchmark/netem.py` for what each models and where the values come from) and repeats the whole sweep `--repeats` times (default 3; 1 with `--quick`) so spread can be reported. Results go to `results/write_share_eval/latency_<N>ms/rep<k>/`, a folder separate from any earlier results. Latency is injected with `tc netem` on node-to-node traffic only, applied identically to all three configurations, and always cleared afterwards. Use `--latency 0 43` to sweep only some levels and `--duration-scale 0.4` to shorten every run (300 s becomes 120 s).

**Runtime** (run length only): sum of the per-preset durations x 3 configurations x levels x repeats. At full length (300 s, SPIKE 180 s) with 9 presets, 3 levels and 3 repeats that is about 19 hours; with `--duration-scale 0.4` about 8 hours. The notebook prints its own estimate before running.

```bash
python benchmark/run_benchmark.py --quick
```

- `--quick`: short/low-load params per preset (`params_for(preset, quick=True)` in `run_benchmark.py`) — for fast iteration while developing, **not** for real report numbers.
- Omit `--quick` for the full-length run used for actual DA-III numbers. Most presets (`READ_HEAVY`/`WRITE_HEAVY`/`MIXED_BURSTY`/`BALANCED`/`HOTSPOT`) share one steady load shape (50 users, spawn rate 20, 300s); `RAMP`/`SPIKE`/`HIGH_CONCURRENCY` each override that shape (slow ramp, longer duration, much higher concurrency respectively) — see `FULL_OVERRIDES`/`QUICK_OVERRIDES` in `run_benchmark.py` for the exact numbers.

```bash
python benchmark/run_benchmark.py            # 3 repeats, 3 latency levels, full length
```

What it does, per combination (`static_fast`/`static_safe`/`adaptive` × the 9 presets):
1. Sets `BENCHMARK_MODE`, `WORKLOAD_PRESET`, and a unique `BENCHMARK_COLLECTION` (e.g. `bench_adaptive_mixed_bursty`) as environment variables.
2. Shells out to `locust -f benchmark/locustfile.py --headless -u N -r N -t Xs --csv results/write_share_eval/latency_<N>ms/rep<k>/<config>_<preset> --only-summary` as a subprocess (deliberately a separate process per run — see [`PROJECT_DOCUMENTATION.md`](PROJECT_DOCUMENTATION.md) for why).
3. Reads back Locust's own generated `_stats.csv`, prints a one-line summary (request count, failures, req/s, p50/p95/p99 latency) to the console as a sanity check.
4. For `adaptive` runs, writes `<config>_<preset>_switches.csv` at the end: every settings change the feedback loop made, with its time and the write share that triggered it.

After each latency level and repeat it prints an aligned summary table for that sweep's 27 runs, and exits with status `1` if anything failed (useful if you ever wire this into CI).

### 2.3 Staleness measurement (separate from the Locust harness)

```bash
python -m benchmark.staleness_test
```

or, for fewer trials / faster iteration:

```bash
python -m benchmark.staleness_test --quick
```

Both also accept `--latency <ms> [<ms> ...]` to sweep only some latency levels (default: 0, 5, 43) and `--repeats N` (default 3; 1 with `--quick`).

**Must** be run as `python -m benchmark.staleness_test` (module form) — running it as `python benchmark/staleness_test.py` breaks its internal package imports.

- Full mode: 200 trials per scenario (60 for the deliberately-paced `adaptive_cold` scenario).
- `--quick`: 40 trials per scenario (still capped at 60 for `adaptive_cold`).
- Measures 4 scenarios in order: `static_fast`, `static_safe`, `adaptive_hot` (warms the feedback loop into `SAFE_SETTINGS` first), `adaptive_cold` (warms it into `FAST_SETTINGS` first; each trial is followed by 9 filler reads and a 2 s pause to stay read-heavy). Every trial records which settings were actually in force, reported as `in_expected_settings`, so it is visible how many trials really ran under the intended settings.
- Prints a per-scenario stale-read rate, a summary table, and a written interpretation of what the numbers mean (see [`PROJECT_DOCUMENTATION.md`](PROJECT_DOCUMENTATION.md#adaptive_layer----the-middleware-package) for the full reasoning). Takes noticeably longer than the other scenarios because `adaptive_cold`'s trials are deliberately spaced ~2s apart.

### 2.4 Stopping / cleanup

Locust runs and the staleness test are one-shot scripts — they exit on their own when done, and each cleans up its own throwaway collection in a `finally` block. No background process to stop afterward.

If a run is killed mid-way and leaves data behind, it's harmless (own database, not reused elsewhere) but can be wiped manually:

```bash
docker exec mongo1 mongosh --port 27017 --eval 'db.getSiblingDB("<db_name>").dropDatabase()'
```

(`benchmark` for the Locust harness, `staleness_test` for the staleness script — retry against `mongo2`/`mongo3` if you get `not primary`.)

---

## 3. Where and how results are stored

Everything lands in the `results/` directory at the repo root. The directory itself is tracked in git via a placeholder file, but its actual contents are **not** — they're regenerated by every run.

```
results/
├── write_share_eval/                         the write-share evaluation (gitignored)
│   ├── latency_0ms/rep1/                     one folder per (latency, repeat); also latency_5ms, latency_43ms; rep1..rep3
│   │   ├── static_fast_READ_HEAVY_stats.csv      )  Locust output, one triplet per (config, preset)
│   │   ├── static_fast_READ_HEAVY_stats_history.csv )  (stats / stats_history / failures)
│   │   ├── static_fast_READ_HEAVY_failures.csv   )
│   │   ├── adaptive_READ_HEAVY_switches.csv      settings the adaptive layer chose, and when
│   │   └── staleness.csv                         written by staleness_test.py
│   ├── latency_0ms/fig_*.png ...                 per-latency charts (means over repeats)
│   ├── summary_reps.csv, summary_agg.csv         per-repeat rows / mean and std over repeats
│   ├── staleness_reps.csv, staleness_pooled.csv  per-repeat rows / trials pooled over repeats
│   ├── settings_timeline.csv, ..._agg.csv        share of run time adaptive spent in FAST / MODERATE / SAFE
│   └── fig_sweep_p50.png, fig_sweep_p95.png, fig_sweep_staleness.png, fig_settings_used.png
└── (earlier single-run results, if present, sit directly under results/ and are never overwritten)
```

`.gitignore` excludes `results/*.csv`, `results/*.png`, `results/latency_*/` and `results/write_share_eval/` so raw run output never gets committed. This means **`results/` is always empty right after a fresh `git clone`** until you run something.

### 3.1 Locust benchmark output (`benchmark/run_benchmark.py`)

For each `(config, preset)` combination at each latency level and repeat, Locust itself (invoked with `--csv results/write_share_eval/latency_<N>ms/rep<k>/<config>_<preset>`) writes three files:

| File | Contents |
|---|---|
| `<config>_<preset>_stats.csv` | One row per request type (`mongo:read`, `mongo:write`, plus an `Aggregated` total row) — request count, failure count, median/average/min/max response time, requests/s, failures/s, and the p50/p66/p75/p80/p90/p95/p98/p99/p99.9/p99.99/p100 response-time percentiles. This is the file `run_benchmark.py` itself parses (the `Aggregated` row) to print its console summary. |
| `<config>_<preset>_stats_history.csv` | The same kind of stats, but as a time series — one row every few seconds throughout the run. This is what a latency-over-time or throughput-over-time chart would be built from (relevant for visualizing `MIXED_BURSTY`'s oscillation). |
| `<config>_<preset>_failures.csv` | One row per distinct failure (if any occurred) — request type, name, error message, and occurrence count. Empty (header only) on a clean run. |

Naming examples: `results/static_fast_READ_HEAVY_stats.csv`, `results/adaptive_HOTSPOT_failures.csv`, etc. — `<config>` is one of `static_fast`/`static_safe`/`adaptive`, `<preset>` is one of the 8 names in `PRESETS` in `run_benchmark.py`.

The console output of `run_benchmark.py` itself (per-run summary lines plus the final aligned table) is **not saved to a file** — redirect it yourself if you want a saved copy, e.g.:

```bash
python benchmark/run_benchmark.py --quick 2>&1 | tee results/run_benchmark_console.log
```

(`run_benchmark_console.log` would not be gitignored automatically since it doesn't match `results/*.csv`/`results/*.png` — if you save one and want to keep it out of git, add it to `.gitignore` or avoid committing it manually.)

### 3.2 Staleness output (`benchmark/staleness_test.py`)

Writes one file per latency level and repeat: **`results/write_share_eval/latency_<N>ms/rep<k>/staleness.csv`**, with one row per scenario:

| Column | Meaning |
|---|---|
| `config` | `static_fast` \| `static_safe` \| `adaptive_hot` \| `adaptive_cold` |
| `trials` | number of write+read trials run for that scenario |
| `hits` | trials where the read-after-write returned fresh data |
| `misses` | trials where it returned stale/missing data |
| `stale_rate` | `misses / trials`, as a float (e.g. `0.035` = 3.5%) |
| `in_expected_settings` | trials whose read ran under the settings the scenario is named for (all of them for the static scenarios) |

This file is overwritten (not appended) every time the script runs — re-running it replaces the previous results.

### 3.3 Results analysis (`benchmark/analyze_results.py`)

```bash
python benchmark/analyze_results.py
```

Reads every repeat under `results/write_share_eval/`, aggregates it with `pandas` (mean and standard deviation over repeats for latency; trials pooled over repeats for staleness; share of run time per setting from the switch logs) and writes `summary_reps.csv`, `summary_agg.csv`, `staleness_reps.csv`, `staleness_pooled.csv`, `settings_timeline.csv` and `settings_timeline_agg.csv`. It draws the per-latency charts (`fig_latency_percentiles.png`, `fig_throughput.png`, `fig_adaptive_overhead.png`, `fig_staleness.png`) and the cross-latency charts (`fig_sweep_p50.png`, `fig_sweep_p95.png`, `fig_sweep_staleness.png`, `fig_settings_used.png`). `experiments.ipynb` calls these same functions and displays the results inline.

### 3.4 Demo app — no result files

`python demo_app.py` writes nothing to `results/`. It's a live/scripted demonstration, not a benchmark run — its "output" is the console transcript, which you'd capture yourself with `tee` if you wanted a saved copy, as was done for [`docs/demo_runs/demo_run_2026-09-10.log`](demo_runs/demo_run_2026-09-10.log).

---

## 4. Shutting down

```bash
docker compose down       # stop containers, keep replica set data in the named volumes
docker compose down -v    # stop AND wipe all data (only if you want a clean slate — re-run rs.initiate() after this)
```

---

## 5. Quick reference — command cheat sheet

| Goal | Command |
|---|---|
| Bring replica set up | `docker compose up -d` |
| Verify replica set health | see §0 |
| Scripted console demo | `python demo_app.py` |
| Ad hoc Locust sanity check | `locust -f benchmark/locustfile.py --headless -u 10 -r 5 -t 30s` |
| Full benchmark (fast/dev, all latency levels) | `python benchmark/run_benchmark.py --quick` |
| Full benchmark (real numbers) | `python benchmark/run_benchmark.py` |
| Staleness measurement | `python -m benchmark.staleness_test` (or `--quick`) |
| Aggregate + chart results | `python benchmark/analyze_results.py` |
| Everything above, inline in one notebook | `experiments.ipynb` (repo root) |
| Run all tests | `python -m pytest tests/ -q` |
| Tear down (keep data) | `docker compose down` |
| Tear down (wipe data) | `docker compose down -v` |
