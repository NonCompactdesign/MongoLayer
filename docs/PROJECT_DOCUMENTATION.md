# Adaptive Consistency Layer for MongoDB — Project Documentation

> NoSQL Databases (BCSE406L), VIT — DA team project.
> This document covers the problem, the solution, the architecture, the runtime data flow, and
> every file in the repository. The eight additions on branch `feature/adaptive-signals-lite`
> (#1 unequal distances, #5 per-collection policy, #6 read-your-own-writes per key, #8 hysteresis,
> #9 SAFE latency budget, #10 switch reasons, #11 static MODERATE baseline, #12 pinned primary)
> are summarised here and explained in full in [`SIGNAL_RULES.md`](SIGNAL_RULES.md). The full evaluation,
> its results and analysis, and the project's journey are in [`FINAL_REPORT.md`](FINAL_REPORT.md).

---

## 1. The Problem

MongoDB (like most replicated databases) exposes **tunable consistency knobs** on every read and write:

- **Write concern** (`w`): how many replica-set members must acknowledge a write before it
  succeeds (`w=1` = just the primary, `w="majority"` = a majority of the set).
- **Read concern**: what guarantee a read gives about the data it returns (`"local"` = whatever
  the node has; `"majority"` = only data acknowledged by a majority).
- **Read preference**: which node(s) a read may go to (`primary`, `primaryPreferred`,
  `secondary`, `secondaryPreferred`, `nearest`).

There is a hard trade-off behind these knobs: **safety costs latency, and speed costs staleness
risk.** `w="majority"` + primary reads is safe but slow. `w=1` + `nearest` is fast but can return
data a secondary has not replicated yet.

Most applications pick these settings once, by hand, and leave them fixed. Real workloads are not
static. The same collection can be write-heavy during an import, read-heavy during browsing, and
bursty around events. A fixed setting is a compromise that is wrong for part of the time.

**The problem this project addresses:** nothing in the standard MongoDB/pymongo stack *observes*
how a collection is used right now and *adjusts* the knobs automatically.

---

## 2. The Solution

**Adaptive Consistency Layer** is a Python middleware between the application and MongoDB (via
`pymongo`) that closes the loop:

1. **Observe.** Every read and write through the middleware is logged (collection, operation
   type, timestamp, latency, and the settings it ran under), including operations that fail.
2. **Analyse.** A sliding-window monitor turns that stream into per-collection statistics: the
   write share, counts, and the p95 write latency per setting.
3. **Decide.** A pure decision function maps the statistics to one of three configurations:
   - **SAFE** (`w="majority"`, read concern `"majority"`, `primary`): write-heavy collections,
     where correctness matters most.
   - **FAST** (`w=1`, `"local"`, `nearest`): read-heavy collections, where speed matters and there
     are few writes to be stale about.
   - **MODERATE** (`w=1`, `"local"`, `primaryPreferred`): everything in between, and the default.

   The traffic rule: write share > 0.40 → SAFE, < 0.15 → FAST, else MODERATE. With the additions
   turned on it has **hysteresis** (leave SAFE only below 0.35, FAST only above 0.20), a **latency
   budget** (SAFE steps down to MODERATE when its writes' p95 exceeds 100 ms), and a
   **per-collection policy** (floor / ceiling) that has the last word. Every decision records its
   **reasons**.
4. **Act.** A background feedback loop re-evaluates every collection on a timer and pushes changes
   into the client, subject to a cooldown against flapping. The **very next** operation uses the
   new settings, with no application code changes. Independently, the client can route a read of
   a key it wrote moments ago to the primary (**read-your-own-writes per key**), even when the
   collection is FAST.

It is validated by:
- a scripted **console demo** (`demo_app.py`) against a real 3-node replica set;
- **live checks** of each addition (`benchmark/signals_check.py`: 5 scenarios, PASS/FAIL each);
- a **Locust benchmark** comparing five configurations (static FAST / MODERATE / SAFE, adaptive,
  adaptive with the additions) on the same workloads, on equal and unequal network topologies,
  with in-load staleness probes and a switch log, all run from one notebook
  (`signals_evaluation.ipynb`);
- a **staleness experiment** (`benchmark/staleness_test.py`) measuring read-after-write staleness
  per configuration.

---

## 3. System Architecture

### 3.1 Component overview

```
                  ┌──────────────────────────────────────────────┐
                  │                Application code               │
                  │  demo_app.py · benchmark/locustfile.py ·       │
                  │  benchmark/signals_check.py · tests/           │
                  └──────────────────────┬───────────────────────┘
                                         │ write(collection, doc) / read(collection, query)
                                         ▼
                  ┌──────────────────────────────────────────────┐
                  │           AdaptiveClient (client.py)          │
                  │ applies the collection's active Settings      │
                  │ per-key read-your-own-writes (#6)             │
                  │ logs every op (latency, settings), failed too │
                  └───────┬──────────────────────────┬───────────┘
        record(op, latency, │ settings name)          │ pymongo calls        ┌──────────────────┐
                            ▼                          └────────────────────► │ MongoDB rs0      │
          ┌──────────────────────────────┐                                    │ mongo1 (primary, │
          │ AccessPatternMonitor          │                                    │   pinned #12)    │
          │ (monitor.py) sliding window:  │                                    │ mongo2, mongo3   │
          │ write share, counts,          │                                    │ (Docker, tc      │
          │ p95 write latency per setting │                                    │  delays #1)      │
          └──────────────┬───────────────┘                                    └──────────────────┘
                         │ Stats
                         ▼
          ┌──────────────────────────────────────────────────────────────┐
          │ Decision (pure, no I/O):                                       │
          │  traffic rule + hysteresis #8 (decision.py)                    │
          │  → latency budget #9 → policy floor/ceiling #5 (rules.py)      │
          │  = Decision(settings, reasons #10)                             │
          └──────────────────────────────┬───────────────────────────────┘
                                         ▼
          ┌──────────────────────────────────────────────────────────────┐
          │ FeedbackLoop (feedback.py) background thread, every interval: │
          │ decide each collection; apply if changed and cooldown passed; │
          │ log SwitchEvent with its reason                               │
          └──────────────────────────────────────────────────────────────┘
```

`AdaptiveClient`, `AccessPatternMonitor`, the decision engine (`decision.py` + `rules.py`) and
`FeedbackLoop` make up the `adaptive_layer` package: the reusable middleware. Everything else
either drives it or measures it.

### 3.2 Component responsibilities

| Component | File | Responsibility |
|---|---|---|
| **AdaptiveClient** | `adaptive_layer/client.py` | The only thing application code talks to. `write()`/`read()` take no consistency arguments: they apply the collection's active `Settings`. Optionally routes reads of keys it wrote in the last N seconds to the primary. Feeds the monitor with every attempted operation. |
| **AccessPatternMonitor** | `adaptive_layer/monitor.py` | Turns `(collection, op, timestamp[, latency, settings name])` events into a sliding-window `Stats` snapshot. No MongoDB dependency; unit tested with synthetic timestamps. |
| **Decision engine: traffic rule** | `adaptive_layer/decision.py` | Pure `decide()` / `decide_explained()`: write share → SAFE / FAST / MODERATE, with optional hysteresis and a reason string. Defines `Settings` and the three named configurations. |
| **Decision engine: budget + policy** | `adaptive_layer/rules.py` | Pure `apply_rules()`: traffic rule → SAFE latency budget → collection policy, returning a `Decision` with its reasons. `SignalConfig`, `CollectionPolicy`. |
| **FeedbackLoop** | `adaptive_layer/feedback.py` | Background thread closing the loop: decide, apply, cooldown, latency-budget memory, policy start-up settings, switch log with reasons. |
| **Demo app** | `demo_app.py` | Three scripted traffic phases through the real stack, printing the settings adapting live. |
| **Benchmark harness** | `benchmark/` | Locust workload generator, runner (latency levels or topologies, with a watchdog), analysis, staleness experiment, topology / cluster tooling, live checks, the notebook's orchestration. |
| **Tests** | `tests/` | Unit tests with fakes (no DB) and integration tests against the live replica set. |

---

## 4. Data Flow

### 4.1 Single write/read call through `AdaptiveClient`

```
caller            AdaptiveClient                                 Monitor             MongoDB
  │ write(c, doc)      │                                            │                    │
  ├───────────────────►│ settings = get_active_settings(c)          │                    │
  │                    │ insert_one(doc, w=settings.write_concern)  ├───────────────────►│
  │                    │◄────────────────────────────────────────────┼────────────────────┤
  │                    │ (finally, even on error)                    │                    │
  │                    │ remember (key, _id) if read-own-writes on   │                    │
  │                    │   and the write succeeded                   │                    │
  │                    │ record(c, "write", ts, latency, "SAFE"/...) │                    │
  │◄───────────────────┤ InsertOneResult (or the exception)          │                    │
```

`read()` has the same shape with `find_one()`, the active read concern and read preference. One
exception: when read-your-own-writes is on, the collection is FAST (`nearest`), and the query asks
by equality for a key or `_id` this client wrote in the last N seconds, the read goes to `primary`
(counted in `client.redirected_reads`).

### 4.2 One `FeedbackLoop.tick()` cycle

```
now = clock()
for collection in monitor.known_collections():
    stats    = monitor.get_stats(collection, now)
    decision = apply_rules(stats, current, signals, policy, thresholds,
                           safe_write_p95 (held for 60 s after a budget breach))
    last_decisions[collection] = decision
    if decision.settings == current:        continue    # nothing to do, cooldown untouched
    if in cooldown:                         continue    # suppressed
    client.set_active_settings(collection, decision.settings)
    switch_log.append(SwitchEvent(now, collection, current, decision.settings, stats, decision.reason))
```

With no `signals` or `policies` and default thresholds, `apply_rules()` reduces to the traffic
rule: exactly the behaviour of `main`.

---

## 5. Repository Map

```
MongoLayer/
├── README.md                       Entry point / quickstart
├── requirements.txt                 Python dependencies
├── docker-compose.yml               3-node MongoDB replica set (NET_ADMIN for tc)
├── docker/mongo/Dockerfile          mongo:7.0 + iproute2 (tc) + ping
├── .gitignore
├── demo_app.py                      Scripted console demo
├── experiments.ipynb                Evaluation notebook for the traffic rule (latency sweep)
├── signals_evaluation.ipynb         The full evaluation of the additions in one notebook (~4 h 35 min)
│
├── plan/exec_plan.md                Phase-by-phase build plan (DA-I → DA-III)
│
├── docs/
│   ├── PROJECT_DOCUMENTATION.md     This file
│   ├── FINAL_REPORT.md              The final report: journey, additions, evaluation, results, analysis
│   ├── SIGNAL_RULES.md              The eight additions in detail, with live results
│   ├── results/signals/<tag>/       Stored record of each full evaluation run (tables, figures, CSVs)
│   ├── SHOWCASE.md                  Live-demo runbook
│   ├── replica_set_setup.md         One-time replica set setup
│   └── demo_runs/                   Saved demo transcript
│
├── adaptive_layer/                  THE MIDDLEWARE
│   ├── client.py                    AdaptiveClient (+ per-key read-your-own-writes)
│   ├── monitor.py                   AccessPatternMonitor + Stats (+ p95 write latency per setting)
│   ├── decision.py                  Settings, Thresholds (+ hysteresis), decide()/decide_explained()
│   ├── rules.py                     SignalConfig, CollectionPolicy, Decision, apply_rules()
│   ├── feedback.py                  FeedbackLoop, SwitchEvent
│   └── _locks.py                    native_lock(): plain OS locks even under gevent (Locust)
│
├── benchmark/
│   ├── locustfile.py                Locust workload (5 modes, 10 presets)
│   ├── run_benchmark.py             Runner: configs x presets x latency levels or topologies, watchdog
│   ├── analyze_results.py           Tables + charts for the latency sweep
│   ├── staleness_test.py            Read-after-write staleness per configuration
│   ├── netem.py                     Equal inter-node delay (tc netem)
│   ├── topology.py                  Per-link delays: T1, T2, EQUAL-<ms>
│   ├── cluster.py                   Pin the primary, disable chained replication
│   ├── signals_check.py             5 live scenarios, one per addition
│   ├── signals_report.py            Summary table of the topology load tests
│   ├── signals_eval.py              Orchestration behind signals_evaluation.ipynb (plan, checks, resume, analysis)
│   ├── run_notebook.py              Run a notebook headlessly, saving after every cell
│   ├── switch_log.py                Switch log CSV (with reasons) + time in settings
│   └── probe_log.py                 In-load staleness probe tally
│
├── tests/                           112 tests (see §6)
└── results/                         Raw benchmark output (gitignored)
```

---

## 6. File-by-File Reference

### Root

**`README.md`**: the one-line pitch, links to the plan and the docs, and the quickstart.

**`requirements.txt`**: `pymongo`, `pytest`, `pandas`, `matplotlib`, `locust`, `ipykernel`,
`nbclient` (the last two run the evaluation notebook headlessly).

**`docker-compose.yml`** + **`docker/mongo/Dockerfile`**: three `mongo:7.0`-based services
(`mongo1:27017`, `mongo2:27018`, `mongo3:27019`), replica set `rs0`, named volumes, one bridge
network. The image adds `iproute2` (for `tc netem` and the `ifb` device) and `ping`, and each
container gets `NET_ADMIN` so delays can be injected.

**`.gitignore`**: Python/OS cruft, local tooling, and raw benchmark output (`results/*.csv`,
`results/latency_*/`, `results/write_share_eval/`, `results/signals_eval/`, `results/final_eval/`).

**`signals_evaluation.ipynb`**: the full evaluation of the additions in one notebook: settings
cell (profiles `FULL` ≈ 4 h 35 min with the 6 core workloads, `FULL_ALL` ≈ 7 h 40 min with all 10,
`SMOKE` ≈ 15 min, `CUSTOM`), plan and time estimate, preflight checks, the 5 live checks, 90
load-test runs (EQUAL-0, T1, T2 × 5 configurations × 6 workloads), the staleness
experiment, then integrity and pre-stated expectations, per-setup tables, figures and a stored
record. Resumable; each run has a watchdog. See [`SIGNAL_RULES.md`](SIGNAL_RULES.md) §7.

**`demo_app.py`**: the scripted console demo: an 8 s window, 2 s interval, 3 s cooldown, three
phases (read-heavy → FAST, write burst → SAFE, mixed → MODERATE), with pauses so each phase's
window is clean. Runs the traffic rule alone (the additions have their own live checks).

### `adaptive_layer/`: the middleware

**`client.py`**: `AdaptiveClient(uri, db_name, monitor=None, verbose=True, key_field="_id", read_own_writes_seconds=None)`.
- `set_active_settings` / `get_active_settings`: the single mutation point. Unconfigured
  collections default to `MODERATE_SETTINGS`.
- `write(collection, doc)`: `WriteConcern(w=...)` from the active settings, then `insert_one`. In
  a `finally` block it remembers the key/`_id` (if read-your-own-writes is on and the write
  succeeded) and logs the operation **whether or not it failed**.
- `read(collection, query)`: active read concern and read preference, except that a FAST read
  whose equality query hits a recently written key or `_id` goes to the primary
  (`redirected_reads` counts these). Logged in a `finally` too.
- `_log_op(...)`: prints (if `verbose`) and calls `monitor.record(collection, op, ts, latency_ms=, tag=<settings name>)`.

**`monitor.py`**: `AccessPatternMonitor(window_seconds=30)`, thread-safe (one `native_lock`).
- `Stats`: `read_count`, `write_count`, `read_write_ratio`, `write_freq`,
  `seconds_since_last_write`, `write_share` (property), and `write_latency`
  (`(settings name, p95 ms, n)` per name; `write_p95(name)` looks one up).
- `record(collection, op_type, timestamp, latency_ms=None, tag=None)`: callers passing only the
  first three get exactly the original statistics.
- `get_stats(collection, now=None)`, `known_collections()`, and pruning on every call as before.

**`decision.py`**: the traffic rule, pure.
- `Settings(write_concern, read_concern, read_preference)`; `SAFE_SETTINGS`, `FAST_SETTINGS`,
  `MODERATE_SETTINGS`; `LEVELS` (weakest → strongest), `BY_NAME`, `settings_name()`.
- `Thresholds(high_write_share=0.40, low_write_share=0.15, hysteresis=0.0)`, validated (the
  hysteresis bands must not overlap).
- `decide_explained(stats, thresholds, current=None) -> (Settings, reason)` and `decide()` (the
  same without the reason). An empty window gives MODERATE. Comparisons are strict; with
  hysteresis, the current SAFE/FAST setting is kept while the share stays beyond `high − h` / `low + h`.

**`rules.py`**: pure. `SIGNALS_THRESHOLDS` (= `Thresholds(hysteresis=0.05)`),
`SignalConfig(write_p95_budget_ms=100, budget_min_samples=20, budget_backoff_seconds=60)`,
`CollectionPolicy(floor=None, ceiling=None)` with `clamp()`, `Decision(settings, reasons)` with
`.reason`, and `apply_rules(stats, current, config, policy, thresholds, safe_write_p95_ms)`:
traffic rule → budget (SAFE only) → policy clamp. `config=None` skips the budget.

**`feedback.py`**: `FeedbackLoop(client, monitor, interval_seconds=30, cooldown_seconds=60, clock=time.time, thresholds=DEFAULT_THRESHOLDS, signals=None, policies=None)`.
- `decide(collection, now=None)` → `(stats, Decision)` without applying it; `tick()`; `run()`,
  `start()`, `stop()` as before.
- `SwitchEvent(timestamp, collection, old, new, stats, reason="")`; `switch_log`;
  `last_decisions` (latest `Decision` per collection).
- A SAFE latency-budget breach is remembered for `budget_backoff_seconds`. Policies with a
  floor/ceiling are applied at construction.

**`_locks.py`**: `native_lock()` returns the original OS lock even when gevent has monkey-patched
`threading` (as Locust does). A gevent-patched lock broke while Locust stopped its users and hung
an overnight run for 6 hours (2026-10-19); the locked sections only touch dicts and deques, so a
plain lock is safe under gevent's cooperative switching.

### `benchmark/`: evaluation harness

Run every module with `python -m benchmark.<name>` (running the file directly breaks the package imports).

**`locustfile.py`**: one shared `AdaptiveClient` / monitor / feedback loop per Locust run,
configured by environment variables:
- `BENCHMARK_MODE`: `static_fast`, `static_moderate`, `static_safe`, `adaptive` (traffic rule,
  15/5/10 s window/interval/cooldown), `adaptive_signals` (the same plus `SIGNALS_THRESHOLDS`,
  `SignalConfig()`, and read-your-own-writes for `BENCHMARK_READ_OWN_WRITES` seconds, default 5,
  keyed on the `key` field).
- `WORKLOAD_PRESET`: `READ_HEAVY` (5% writes), `WRITE_HEAVY` (95%), `MIXED_BURSTY` (95% ↔ 5% every
  15 s), `BALANCED` / `HOTSPOT` / `RAMP` / `HIGH_CONCURRENCY` (50%), `MODEST_WRITES` (25%),
  `SPIKE` (5% with a 3 s 90% burst every 20 s), `READ_HEAVY_HOTSPOT` (5% writes, 80% of operations
  on 5% of the keys).
- 2% of iterations are staleness probes (write, then read the same `_id`); with
  read-your-own-writes on, those probe reads go to the primary by design.
- Optional outputs: `BENCHMARK_SWITCH_LOG`, `BENCHMARK_PROBE_LOG`, `BENCHMARK_CLIENT_LOG` (JSON
  with `redirected_reads`).

**`run_benchmark.py`**: runs every (config × preset) as a separate `locust` subprocess (Locust's
gevent patches conflict with pymongo in one process). `CONFIGS` (5), `PRESETS` (10).
`--latency 0 5` (equal delays, the default; 1 / 33 / 43 also accepted; results in
`results/write_share_eval/latency_<N>ms/rep<k>/`) or `--topology T1 T2 EQUAL-0` (pins the primary
first; results in `results/signals_eval/topology_<name>/rep<k>/`). Also `--configs`, `--presets`,
`--duration 60s`, `--duration-scale`, `--repeats`, `--quick`. Delays are always cleared afterwards.
`run_one(..., timeout_s=)` kills a Locust run (and its child processes) that overruns, and Locust
runs with `--exit-code-on-error 0`, so a non-zero exit means Locust itself broke, not that a request failed.

**`analyze_results.py`**: tables and charts for the latency sweep: per-repeat and aggregated
latency, staleness, probes, and time-in-settings. Config names are matched by the longest prefix,
so `adaptive_signals_*` files are never read as `adaptive`.

**`staleness_test.py`**: five scenarios at each latency level: `static_fast`, `static_moderate`,
`static_safe`, `adaptive_hot` (warmed into SAFE), `adaptive_cold` (warmed into FAST, kept there
with filler reads). Each trial writes a marker and reads it straight back, recording the settings
in force.

**`netem.py`**: equal one-way inter-node delay (`apply_latency(ms)`; default levels 0 / 5 ms,
1 / 33 / 43 ms on request, with sources), `clear()` (also removes topology ingress shaping and
the `ifb0` device), `measure_rtt_ms()`.

**`topology.py`**: per-link delays (node ↔ node, and both directions of client ↔ node through the
nodes themselves). `T1`, `T2`, `equal(ms)`, `by_name()`, `plan_commands()` (pure, tested),
`apply()`, `clear()`, `applied()` context manager, `measure_client_rtts()`. CLI:
`python -m benchmark.topology T1|T2|EQUAL-<ms>|clear`.

**`cluster.py`**: `pin_primary(node="mongo1")`: priority 10 for mongo1, `chainingAllowed=false`,
then waits for mongo1 primary, two healthy secondaries and direct replication. Also
`node_states()`, `sync_sources()`. CLI: `python -m benchmark.cluster`.

**`signals_check.py`**: five live scenarios (C1 distances, C2 own writes, C3 policy, C4 budget,
C5 hysteresis) with a short 6 / 1 / 3 s window / interval / cooldown, each printing PASS/FAIL and
writing `results/signals_eval/checks.json`; each also checks that every switch has a reason.

**`signals_report.py`**: reads `results/signals_eval/.../topology_*/rep*/`, prints a markdown table
(read/write p50/p95, failures, stale probes overall and per setting, time in each setting,
switches, most common switch reasons, own-write reads sent to the primary) and writes `summary.csv`.

**`signals_eval.py`**: everything `signals_evaluation.ipynb` calls. `Plan` (setups, configs,
presets, `duration_scale`, `fixed_duration_s`, repeats, live checks, staleness setups and trials;
the defaults are the `FULL` run), `estimate()` (from the measured ~5 s per-run overhead),
`preflight()`, `run_live_checks()`, `run_load_tests()` (one `.done` marker per run so an
interrupted run resumes; a watchdog 120 s past each run's length and one retry),
`run_staleness()`, `analyse()` (summary / staleness tables, `integrity()`, `expectations()`),
five figures, `store_record()`, and `write_meta()` / `Log`.

**`run_notebook.py`**: `python -m benchmark.run_notebook <notebook>` executes a notebook with its
own kernel and saves it after every cell, so a long run that stops still has every finished cell's output.

**`switch_log.py`**: `write_switch_log()` (CSV: `event, seconds, settings, write_share, reads, writes, reason`)
and `time_in_settings()`.

**`probe_log.py`**: `ProbeTally` (per-settings probe and stale counts) and `read_probe_csv()`.

### `tests/` (112 tests; `python -m pytest -q`)

| File | What it covers |
|---|---|
| `test_decision.py` | traffic rule branches, boundaries, scale invariance, thresholds validation |
| `test_rules.py` | hysteresis and reasons, the latency budget, policy clamp, `config=None` = traffic rule |
| `test_signals_plumbing.py` | monitor p95 per setting, feedback-loop budget back-off / cooldown / policy start-up / reasons, client key memory, redirect counting and failed-write logging, native locks, topology command plans |
| `test_monitor.py` | sliding window counts, pruning, validation |
| `test_feedback.py` | tick orchestration with fake clock / client / monitor |
| `test_switch_log.py`, `test_probe_log.py` | CSV writers and readers |
| `test_run_benchmark.py`, `test_analyze_results.py` | runner plumbing and analysis on synthetic result trees |
| `test_signals_eval.py` | the default plan and its estimate, resume markers, the watchdog (kills a hung process tree) and retry, integrity and expectation checks on synthetic results |
| `test_client_smoke.py`, `test_feedback_integration.py` | **live**: client and feedback loop against the real replica set |

---

## Appendix: the three named consistency configurations

| Name | Write concern | Read concern | Read preference | Traffic rule picks it when | Additions that change it |
|---|---|---|---|---|---|
| **SAFE** | `"majority"` | `"majority"` | `primary` | write share > 0.40 (stay while > 0.35 with hysteresis) | budget: SAFE write p95 > 100 ms → MODERATE (held 60 s) |
| **MODERATE** | `1` | `"local"` | `primaryPreferred` | otherwise, including no traffic | – (the fall-back) |
| **FAST** | `1` | `"local"` | `nearest` | write share < 0.15 (stay while < 0.20) | per key in the client: reads of this client's writes from the last 5 s go to the primary |

A collection policy (`floor` / `ceiling`) is applied after all of the above.
