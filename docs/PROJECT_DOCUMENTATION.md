# Adaptive Consistency Layer for MongoDB — Project Documentation

> NoSQL Databases (BCSE406L), VIT — DA team project.
> This document explains the problem being solved, the solution built, the system architecture, how every file in the repository maps into that architecture, the runtime data flow, and a description of every individual file in the project. It describes the code as it is now (refreshed 2026-10-16); it does not contain evaluation results; the current evaluation's output goes to `results/write_share_eval/`.

---

## 1. The Problem

MongoDB (like most distributed/replicated databases) exposes **tunable consistency knobs** on every read and write:

- **Write concern** (`w`) — how many replica-set members must acknowledge a write before it's considered successful (`w=1` = just the primary, `w="majority"` = a majority of the set).
- **Read concern** — what guarantee a read gives about the data it returns (`"local"` = whatever the node has, possibly stale/uncommitted from the cluster's perspective; `"majority"` = only data acknowledged by a majority).
- **Read preference** — which node(s) a read is allowed to be routed to (`primary`, `primaryPreferred`, `secondary`, `secondaryPreferred`, `nearest`).

There is a hard, well-known trade-off behind these knobs: **safety costs latency, and speed costs staleness risk.** `w="majority"` + `read_preference="primary"` is safe but slow (every write waits on multiple nodes; every read is funneled through one node). `w=1` + `read_preference="nearest"` is fast but can return stale or even not-yet-durable data if a secondary hasn't replicated the latest write yet. How large that cost is depends on how far apart the nodes are: on one machine a majority write is nearly free; across regions it dominates.

In almost every real application, these settings are chosen **once, by hand, and left fixed** for the whole application (or at best, fixed per collection at the code level). But real workloads are not static — the same collection can be write-heavy during a bulk import, read-heavy during normal browsing, and bursty around specific events. A single fixed setting is necessarily a compromise: too safe (and slow) for the quiet periods, or too fast (and risky) for the hot periods.

**The problem this project addresses:** nothing in the standard MongoDB/pymongo stack *observes* how a collection is actually being used right now and *automatically* adjusts the consistency knobs in response. That adaptation has to be hand-tuned or left as a static, one-size-fits-none compromise.

---

## 2. The Solution

**Adaptive Consistency Layer** is a Python middleware that sits between the application and MongoDB (via `pymongo`) and closes the loop automatically:

1. **Observe** — every read/write that flows through the middleware is logged with its collection name, operation type, and timestamp.
2. **Analyze** — a sliding-window monitor turns that stream of logged operations into per-collection access-pattern statistics (read count, write count, read:write ratio, write frequency, time since last write, and the derived **write share** = writes / all operations).
3. **Decide** — a pure, rule-based decision function maps the **write share** of the window to one of three named consistency configurations:
   - **SAFE** (`w="majority"`, read concern `"majority"`, read preference `primary`) — when writes are a large share of recent traffic (over 40%), where correctness matters more than speed.
   - **FAST** (`w=1`, read concern `"local"`, read preference `nearest`) — when writes are a small share (under 15%), where speed matters more and there is little to be stale about.
   - **MODERATE** (`w=1`, read concern `"local"`, read preference `primaryPreferred`) — the balanced default for everything in between, and for any collection with no observed traffic yet.
4. **Act** — a background feedback loop re-evaluates every known collection on a timer and, subject to a cooldown (to prevent rapid oscillation/"flapping" under bursty load), pushes the new settings into the client. The **very next** operation on that collection automatically uses the new settings — no application code changes required. Every real switch is recorded (time, old and new settings, and the stats that triggered it).

The rule uses the write **share**, not an absolute writes-per-second rate, on purpose: an absolute rate does not scale with load (the original rule called anything above 5 writes/s "hot", so a busy but 95%-read collection was always SAFE). The thresholds are parameters (`Thresholds`) and were fixed before the evaluation they are used in.

The system is validated in four ways:
- A **live demo app** (`demo_app.py`) that shows the settings visibly changing in response to real traffic against a real 3-node MongoDB replica set.
- **Automated tests** (71): unit tests with fake clocks/clients (no database) and integration tests against the real replica set.
- A **benchmark harness** (`benchmark/`, built on Locust) that drives the same read/write workloads (nine traffic presets) against three configurations — `static_fast`, `static_safe`, and `adaptive` — at several **injected inter-node network latencies** (`tc netem`), with repeats, and compares latency and throughput. Adaptive runs also log which settings they actually used.
- **Staleness measurement** of read-after-write freshness per configuration: an isolated experiment (`benchmark/staleness_test.py`) and probes mixed into the load tests themselves (`benchmark/probe_log.py`), so the freshness cost of FAST decisions is measured under real traffic.

---

## 3. System Architecture

### 3.1 Component overview

```
                              ┌───────────────────────────────────────────┐
                              │              Application code             │
                              │   demo_app.py ·                           │
                              │   benchmark/locustfile.py · tests/        │
                              └───────────────────┬───────────────────────┘
                                                   │ .write(collection, doc)
                                                   │ .read(collection, query)
                                                   ▼
                              ┌───────────────────────────────────────────┐
                              │        AdaptiveClient (client.py)         │
                              │  thin pymongo proxy/interceptor:          │
                              │  - looks up per-collection active         │
                              │    Settings and applies them to the       │
                              │    underlying pymongo call                │
                              │  - logs every op (console + Monitor)      │
                              └───────┬───────────────────────┬───────────┘
                                      │ record(op)             │ actual pymongo
                                      ▼                        │ read/write
                      ┌───────────────────────────┐            ▼
                      │  AccessPatternMonitor      │   ┌──────────────────┐
                      │      (monitor.py)          │   │  MongoDB replica │
                      │  sliding-window per-        │   │  set (rs0):      │
                      │  collection stats:          │   │  mongo1 (27017)  │
                      │  read_count, write_count,   │   │  mongo2 (27018)  │
                      │  read_write_ratio,           │   │  mongo3 (27019)  │
                      │  write_freq,                 │   │  via Docker      │
                      │  seconds_since_last_write    │   │  Compose         │
                      └──────────────┬──────────────┘   └──────────────────┘
                                     │ get_stats(collection)
                                     ▼
                      ┌───────────────────────────┐
                      │      Decision Engine        │
                      │      (decision.py)          │
                      │  pure function:              │
                      │  Stats -> Settings           │
                      │  by write share              │
                      │  (SAFE / FAST / MODERATE)    │
                      └──────────────┬──────────────┘
                                     │ decide(stats, thresholds)
                                     ▼
                      ┌───────────────────────────┐
                      │       FeedbackLoop           │
                      │      (feedback.py)           │
                      │  background thread; every     │
                      │  interval_seconds, for every   │
                      │  known collection: decide(),   │
                      │  then (if changed AND cooldown  │
                      │  elapsed) call                  │
                      │  client.set_active_settings()   │
                      │  and append to switch_log       │
                      └──────────────────────────────┘
```

`AdaptiveClient`, `AccessPatternMonitor`, `Decision Engine`, and `FeedbackLoop` together make up the `adaptive_layer` package — the reusable middleware. Everything else in the repo (`demo_app.py`, `benchmark/`, `tests/`, `experiments.ipynb`) either *drives* this middleware to prove it works, or *measures* how well it works.

### 3.2 Component responsibilities

| Component | File | Responsibility |
|---|---|---|
| **AdaptiveClient** | `adaptive_layer/client.py` | The only thing application code talks to. Wraps `pymongo.MongoClient`. Exposes `write()`/`read()` with *no* consistency arguments — it looks up each collection's currently active `Settings` internally and applies them to the underlying pymongo call. Also the single mutation point (`set_active_settings`) that the feedback loop uses to change behavior live. |
| **AccessPatternMonitor** | `adaptive_layer/monitor.py` | Turns a stream of `(collection, op_type, timestamp)` events into a live, sliding-window `Stats` snapshot per collection. Fully decoupled from MongoDB — unit-testable with synthetic timestamps. |
| **Decision Engine** | `adaptive_layer/decision.py` | A pure function `decide(stats, thresholds) -> Settings`. No I/O, no state. Encodes the three-branch policy on the window's write share (high → SAFE, low → FAST, otherwise → MODERATE). |
| **FeedbackLoop** | `adaptive_layer/feedback.py` | The orchestrator that closes the loop: on a timer, pulls stats from the Monitor, runs the Decision Engine, and pushes changed decisions into the Client — subject to a cooldown that prevents rapid flapping — and records each real switch in `switch_log`. Runs as a background thread via `start()`/`stop()`; `tick()` is exposed separately for deterministic testing. |
| **Demo app** | `demo_app.py` | Scripts three traffic phases (read-heavy, write-burst, mixed) through the real stack against the real replica set and prints the settings adapting live — a console-based proof. |
| **Benchmark harness** | `benchmark/` | Locust-based load generator (nine traffic presets, staleness probes), a runner that drives the three configurations across injected network latencies with repeats, latency injection (`netem.py`), the staleness experiment, switch-log and probe-log recording, and the analysis code. |
| **Experiment notebook** | `experiments.ipynb` | Runs the whole evaluation from one place and shows the tables and charts inline. |
| **Tests** | `tests/` | 71 tests: unit tests (Monitor, Decision Engine, FeedbackLoop with fakes, switch/probe logs, runner and analysis plumbing — no DB) and integration tests (Client and FeedbackLoop against the real replica set). |

---

## 4. Data Flow

### 4.1 Single write/read call through `AdaptiveClient`

```
caller                AdaptiveClient                     Monitor            MongoDB
  │                          │                               │                  │
  │  write(coll, doc)        │                               │                  │
  ├─────────────────────────►│                               │                  │
  │                          │ settings = get_active_settings(coll)             │
  │                          │   (defaults to MODERATE_SETTINGS                 │
  │                          │    if coll was never configured)                 │
  │                          │                               │                  │
  │                          │ insert_one(doc, write_concern=settings.write_concern)
  │                          ├──────────────────────────────────────────────────►│
  │                          │◄──────────────────────────────────────────────────┤
  │                          │ _log_op(coll, "write", ts, latency)              │
  │                          ├──────────────────────────────►│                  │
  │                          │                          record(coll,"write",ts) │
  │◄─────────────────────────┤                               │                  │
  │  InsertOneResult         │                               │                  │
```

`read()` follows the identical shape, using `find_one()` with `read_concern`/`read_preference` built from the collection's active settings instead of `write_concern`.

### 4.2 One `FeedbackLoop.tick()` cycle

```
FeedbackLoop.tick()
  now = clock()
  for collection in monitor.known_collections():
      stats     = monitor.get_stats(collection, now=now)
      decision  = decide(stats, thresholds)          # Decision Engine — pure function
      current   = client.get_active_settings(collection)

      if decision == current:
          continue                                    # nothing to do, cooldown untouched

      last_switch = last_switch_time[collection]
      if last_switch is not None and (now - last_switch) < cooldown_seconds:
          continue                                    # would switch, but suppressed by cooldown

      client.set_active_settings(collection, decision) # <-- the only mutation
      last_switch_time[collection] = now
      switch_log.append(SwitchEvent(now, collection, current, decision, stats))
```

`run()` simply calls `tick()`, sleeps `interval_seconds`, and repeats until `stop()` is called — driven by a background `threading.Thread` started from `start()`.

### 4.3 End-to-end flow (demo)

```
 ┌────────────┐   write()/read()   ┌────────────────┐  record()  ┌──────────────────────┐
 │ Traffic     │ ─────────────────►│ AdaptiveClient  │──────────► │ AccessPatternMonitor  │
 │ source      │                   │                 │            │ (sliding window)      │
 │ (demo_app)  │◄──────────────────┤                 │            └──────────┬────────────┘
 │              │  applies active   └───────┬─────────┘                       │ get_stats()
 │              │  settings for the │       │ actual pymongo call             ▼
 └────────────┘  collection         │       ▼                        ┌──────────────┐
                                     │  MongoDB replica set (rs0)     │ decide()      │
                                     │  mongo1 / mongo2 / mongo3      │ (Decision     │
                                     └─────────────────────────────┐ │  Engine)      │
                                                                    │ └──────┬───────┘
                              set_active_settings(collection, new) │        │
                              ◄───────────────────────────────────┼────────┘
                                        FeedbackLoop.tick()  (background thread,
                                        every interval_seconds, gated by cooldown)
```

### 4.4 Evaluation flow

```
experiments.ipynb  (or: python benchmark/run_benchmark.py, python -m benchmark.staleness_test)
  for each repeat, for each injected one-way latency (0 / 5 / 43 ms by default):
      netem.apply_latency(ms)            # tc netem on node-to-node traffic only, verified with ping
      for each config (static_fast / static_safe / adaptive) x each traffic preset:
          locust (own subprocess) -> <config>_<preset>_stats.csv
                                     adaptive_<preset>_switches.csv   (what settings, when)
                                     <config>_<preset>_probe.csv      (stale probe reads, by settings)
      staleness_test scenarios (static_fast / static_safe / adaptive_hot / adaptive_cold) -> staleness.csv
      netem.clear()                       # always, even on failure
  analyze_results: aggregate over repeats (mean, std; pooled staleness) -> CSVs and charts
```

---

## 5. Repository Map

```
MongoLayer/
├── README.md                       Entry point / quickstart
├── requirements.txt                 Python dependencies
├── docker-compose.yml               3-node MongoDB replica set definition (custom image, NET_ADMIN)
├── .gitignore
├── experiments.ipynb                Runs the evaluation and shows tables/charts (unexecuted template)
│
├── docker/
│   └── mongo/
│       └── Dockerfile               mongo:7.0 + iproute2 (tc) + iputils-ping, for latency injection
│
├── plan/
│   └── exec_plan.md                 Phase-by-phase build plan (DA-I → DA-III) and the 2026-10-16 re-plan
│
├── docs/
│   ├── replica_set_setup.md         One-time replica set setup notes
│   ├── SHOWCASE.md                  Live-demo/showcase runbook
│   ├── RUNNING_GUIDE.md             Command-by-command guide to the benchmarks and where results go
│   ├── PROJECT_DOCUMENTATION.md     This file
│   └── demo_runs/
│       └── demo_run_2026-09-10.log  Saved transcript of a clean demo_app.py run (earlier rule)
│
├── adaptive_layer/                  THE MIDDLEWARE (reusable package)
│   ├── __init__.py                  (empty — marks the package)
│   ├── client.py                    AdaptiveClient (proxy/interceptor)
│   ├── monitor.py                   AccessPatternMonitor + Stats (incl. write_share)
│   ├── decision.py                  Decision Engine (decide()) + Settings + Thresholds
│   └── feedback.py                  FeedbackLoop (background adaptation thread) + SwitchEvent
│
├── demo_app.py                      Scripted console demo (Phase 7)
│
├── benchmark/                       Evaluation / load-testing harness
│   ├── __init__.py                  (empty — marks the package)
│   ├── locustfile.py                Locust workload generator: 9 presets, staleness probes
│   ├── run_benchmark.py             Orchestrates latency levels x repeats x configs x presets via Locust
│   ├── netem.py                     Inter-node latency injection with tc netem
│   ├── switch_log.py                Writes/reads the per-run record of settings the adaptive layer used
│   ├── probe_log.py                 Tally of in-load staleness probes
│   ├── staleness_test.py            Read-after-write staleness experiment (4 scenarios)
│   └── analyze_results.py           Aggregation over repeats, tables (CSV) and charts
│
├── tests/
│   ├── __init__.py                  (empty — marks the package)
│   ├── test_monitor.py              Unit: AccessPatternMonitor (no DB)                           9 tests
│   ├── test_decision.py             Unit: Decision Engine, write-share rule, Thresholds          22 tests
│   ├── test_feedback.py             Unit: FeedbackLoop, fake clock/client/monitor, switch log    10 tests
│   ├── test_switch_log.py           Unit: switch-log writer/reader                                3 tests
│   ├── test_probe_log.py            Unit: probe tally + real locustfile probe path (fake client)  3 tests
│   ├── _locustfile_probe_check.py   Helper run in a subprocess by test_probe_log.py
│   ├── test_run_benchmark.py        Unit: runner plumbing (layout, scaling, cleanup)              8 tests
│   ├── test_analyze_results.py      Unit: analysis on synthetic result trees                     11 tests
│   ├── test_feedback_integration.py Integration: FeedbackLoop vs real replica set                 1 test
│   └── test_client_smoke.py         Integration: AdaptiveClient vs real replica set               4 tests
│
└── results/                         Generated output (gitignored)
    ├── write_share_eval/            Current evaluation: latency_<N>ms/rep<k>/ + aggregates + charts
    └── (earlier single-run results, if present, sit directly under results/)
```

---

## 6. File-by-File Reference

### Root

**`README.md`**
Project entry point. States the one-line pitch (workload-aware middleware that dynamically tunes write concern / read concern / read preference), identifies it as the VIT NoSQL (BCSE406L) DA team project, links out to `plan/exec_plan.md` and `docs/SHOWCASE.md`, and gives the quickstart (`venv` + `pip install -r requirements.txt`), noting the Docker Compose replica set is a hard prerequisite.

**`requirements.txt`**
Dependency list (by name, not version-pinned): `pymongo` (MongoDB driver), `pytest` (test runner), `pandas` (results aggregation), `matplotlib` (charts), `locust` (load generator), `ipykernel` and `nbclient` (to run `experiments.ipynb`). Locust itself depends on Flask for its own web UI, which is why Flask is installed even though this project has no web interface of its own.

**`docker-compose.yml`**
Defines the 3-node local MongoDB replica set the entire project depends on. Three services (`mongo1`, `mongo2`, `mongo3`) using the image `mongolayer-mongo:7.0`, which is built from `docker/mongo/Dockerfile` (declared once, on `mongo1`, so the three services share one build). Each runs `mongod --replSet rs0 --bind_ip_all --port <27017|27018|27019>`, has its own named volume (so data survives container restarts) and host port mapping, and is granted the `NET_ADMIN` capability so `tc` can inject network latency inside it. All share one bridge network (`rs0net`). A standalone `mongod` cannot demonstrate any of this project's logic — write concern, read concern, and read preference only produce observable differences against a real multi-node replica set.

**`docker/mongo/Dockerfile`**
`FROM mongo:7.0` plus `iproute2` (provides `tc`) and `iputils-ping` (used to verify the injected round-trip time). Build with `docker compose up -d --build`.

**`.gitignore`**
Standard Python ignores (`__pycache__/`, `*.pyc`, `.venv/`, `venv/`), Node ignores (unused here, leftover boilerplate), benchmark output (`results/*.csv`, `results/*.png`, `results/latency_*/`, `results/write_share_eval/` — regenerated by every run, not checked in), OS cruft (`.DS_Store`, `Thumbs.db`), and local tooling state (`.claude/`).

**`experiments.ipynb`**
The experiment runner, saved unexecuted. A configuration cell (`QUICK`, `REPEATS`, `LATENCY_LEVELS_MS`, `PRESETS_TO_RUN`, `DURATION_SCALE`, `STALENESS_TRIALS`, `RUN_TESTS`) prints the number of runs and an estimated run time before anything starts. Sections: setup; environment check (replica set health); tests; latency-injection verification (measured ping round trip per level); benchmark sweep; staleness sweep; load and aggregate; tables (run overview, p95 mean ± std, read and write latency separately, settings the adaptive layer used, adaptive vs baselines, isolated staleness, in-load staleness); charts across and per latency level; summary by latency level. It reuses `run_benchmark`, `staleness_test` and `analyze_results` directly, so the notebook and the command-line scripts agree. Needs the replica set running and the project `.venv` kernel.

**`demo_app.py`**
The Phase 7 scripted console demo. Builds the real stack (`AccessPatternMonitor` with an 8s window, `AdaptiveClient` wired to that monitor, `FeedbackLoop` with a 2s interval / 3s cooldown) against the real replica set (`demo.live_demo` collection) and runs it end-to-end with **nothing mocked**. Three phases, each separated by a pause long enough for the monitor's window to fully clear so results aren't contaminated by the previous phase:
- **Phase A (read-heavy):** 20 reads, no writes (plus one seed write) → about 5% write share → expects `FAST_SETTINGS`.
- **Phase B (write burst):** 60 writes and no reads, as fast as possible → 100% write share → expects `SAFE_SETTINGS`.
- **Phase C (mixed):** 5 writes and 10 reads interleaved at a moderate pace → about 33% write share → expects `MODERATE_SETTINGS`.

After each phase it prints the monitor's stats (including the write share) and the active settings, flags whether they matched the expected branch, and cleans up (`loop.stop()`, drops the demo collection, closes the client) in a `finally` block regardless of outcome. Run with `python demo_app.py`.

---

### `plan/`

**`plan/exec_plan.md`**
The execution plan for the whole project, broken into phases (0–13, plus the later phases added in the re-plan) from repo bootstrap through the DA-III final report/demo/slides, each with a Goal, Why, numbered Steps, and a Definition of Done. Includes a milestone map tying phase ranges to DA-II/DA-III submission checkpoints, an "Open decisions to revisit" section (per-collection vs. per-document granularity, an optional ML-based decision engine, team split), and a dated "Status and re-plan (2026-10-16)" section with the verified state and the revised Phase 12. Phase numbers referenced in code comments (e.g. "Phase 3", "Phase 6") point back to sections of this document.

---

### `docs/`

**`docs/replica_set_setup.md`**
The one-time, detailed replica-set setup reference: why hosts-file entries for `mongo1`/`mongo2`/`mongo3` → `127.0.0.1` are required (so the same `host:port` pairs resolve correctly both from inside the Docker network and from the host machine running Python), the `docker compose up -d --build` + `rs.initiate()` sequence (the build step adds `tc` to the image), a verification command, the pymongo connection string, a working smoke-test code snippet, and teardown commands (`docker compose down` vs. `down -v`).

**`docs/SHOWCASE.md`**
The "get it running right now" operational runbook for live demos/presentations. Covers: one-time setup (clone, venv, hosts file, first `docker compose up` + `rs.initiate()`); the every-time pre-showcase checklist (bring the replica set up, verify health, activate venv, run the test suite as a sanity check); the live demo (`python demo_app.py`, with a table of its three phases and what to watch for); supporting evidence (the benchmark runner and staleness test); shutdown commands; and a troubleshooting table for the most common failure symptoms.

**`docs/RUNNING_GUIDE.md`**
Command-by-command guide to the Locust benchmark harness: bringing the replica set up, the scripted demo, a single ad hoc Locust run and its environment variables, the orchestrated run (latency levels, repeats, `--duration-scale`, runtime estimates), the staleness experiment, the analysis step, where every result file ends up and what is in it, shutdown, and a command cheat sheet.

**`docs/demo_runs/demo_run_2026-09-10.log`**
A saved console transcript of a successful `demo_app.py` run (dated 2026-09-10), captured under the earlier absolute-rate rule — usable as fallback evidence of the demo, though its stats lines predate the write-share rule. Re-capture it once the demo has been re-run on the current code.

**`docs/PROJECT_DOCUMENTATION.md`**
This document.

---

### `adaptive_layer/` — the middleware package

**`adaptive_layer/__init__.py`**
Empty. Marks `adaptive_layer` as a regular Python package so `from adaptive_layer.client import AdaptiveClient` etc. resolve.

**`adaptive_layer/client.py`**
Defines `AdaptiveClient`, the thin `pymongo` proxy/interceptor every other component talks to.

- `__init__(uri, db_name, monitor=None, verbose=True)` — opens a `pymongo.MongoClient`, selects the database, optionally accepts an `AccessPatternMonitor` (or any duck-typed equivalent) to feed, and initializes `self._active_settings: dict[str, Settings]` (empty — collections default to `MODERATE_SETTINGS` until configured). `verbose=False` silences the per-operation console line (the benchmarks and the staleness experiment turn it off; the demo keeps it on).
- `set_active_settings(collection, settings)` — the **only mutation point** in the class; the `FeedbackLoop` is the intended caller.
- `get_active_settings(collection)` — returns the collection's currently active `Settings`, defaulting to `MODERATE_SETTINGS` for any collection never explicitly configured (never raises).
- `write(collection, doc)` — looks up active settings, builds a `WriteConcern(w=settings.write_concern)`, times and performs `insert_one`, logs the op, returns the `InsertOneResult`.
- `read(collection, query)` — looks up active settings, builds `ReadConcern(level=settings.read_concern)` and resolves the read-preference string (`primary`/`primaryPreferred`/`secondary`/`secondaryPreferred`/`nearest`) to a pymongo `ReadPreference` object via the module-level `_READ_PREFERENCES` map (raising `ValueError` on an unrecognized name), times and performs `find_one`, logs the op, returns the document or `None`.
- `_log_op(collection, op_type, timestamp, latency_ms)` — prints a formatted line (when `verbose`) and, if a monitor was supplied, calls `monitor.record(...)` so real traffic actually populates the Monitor's stats.
- `close()` — closes the underlying `MongoClient`.

Notably, `write()`/`read()` take **no** consistency-related arguments — that seam was deliberately removed (per Phase 5 of the exec plan) so the `FeedbackLoop` can change a collection's effective behavior without any caller code changing.

**`adaptive_layer/monitor.py`**
Defines `AccessPatternMonitor` and its `Stats` output type — completely decoupled from MongoDB and from `AdaptiveClient` (it only ever sees `(collection, op_type, timestamp)` tuples), which is what makes it unit-testable with synthetic timestamps and no live database.

- `Stats` — a frozen dataclass: `read_count`, `write_count`, `read_write_ratio` (reads per write; `float('inf')` if there are reads but zero writes), `write_freq` (writes per second, averaged over the window), `seconds_since_last_write` (`None` if no write in-window). It also has a `write_share` property: writes / (reads + writes), or `0.0` when the window has no operations. The Decision Engine decides on `write_share`.
- `NEUTRAL_STATS` — a module-level all-zero/`None` `Stats` instance returned for any collection with no (or no longer any) in-window events — so callers, especially the Decision Engine, never have to special-case "never seen this collection."
- `AccessPatternMonitor(window_seconds=30)` — raises `ValueError` if `window_seconds <= 0`. Internally: `collections.defaultdict(collections.deque)` mapping collection name → an oldest-first deque of `(timestamp, op_type)` tuples.
  - `known_collections()` — every collection name ever `record()`-ed (used by the `FeedbackLoop` to know what to evaluate each tick; a collection stays "known" even after all its events age out, correctly yielding `NEUTRAL_STATS` from `get_stats`).
  - `record(collection, op_type, timestamp)` — validates `op_type` is `"read"` or `"write"` (else `ValueError`), appends the event, then immediately prunes (so memory doesn't grow unbounded even if `get_stats()` is never called).
  - `get_stats(collection, now=None)` — defaults `now` to `time.time()`; returns `NEUTRAL_STATS` for an unknown or now-empty collection; otherwise prunes again (in case of a stale window edge), then computes read/write counts, the ratio (0.0 if no traffic at all, `inf` if reads-only), `write_freq` as `write_count / window_seconds`, and `seconds_since_last_write` from the max write timestamp.
  - `_prune(collection, now)` — pops events older than `now - window_seconds` off the left (oldest) end of the deque. Called both from `record()` and from `get_stats()`.

**`adaptive_layer/decision.py`**
Defines the Decision Engine — a deliberately pure function with zero side effects (no DB access, no state), so it's the easiest and most exhaustively unit-testable piece, and the one most likely to be scrutinized ("why did it pick majority write concern here?" should be answerable by one threshold).

- Why a write **share** and not a rate: the original rule used absolute writes per second (SAFE above 5/s; FAST needed under 0.5/s). That does not scale — at 250 operations/s even 5% writes is about 12 writes/s, so every busy collection was SAFE and FAST was unreachable above roughly 0.2% writes. A share gives the same decision for the same traffic mix at any rate.
- `HIGH_WRITE_SHARE = 0.40` and `LOW_WRITE_SHARE = 0.15`. They were fixed before the evaluation they are used in (rationale in the module docstring): `0.15` restates the old "more than 5 reads per write" rule as a share (1/6 ≈ 0.17) rounded down; `0.40` means writes are at least 2 of every 5 operations, chosen so a 1-write-per-2-reads mix (33%) is MODERATE and a 50/50 mix is clearly SAFE.
- `Thresholds(high_write_share, low_write_share)` — a frozen dataclass holding the two cut-offs, with defaults from the constants above. Raises `ValueError` unless `0 <= low_write_share < high_write_share <= 1`. `DEFAULT_THRESHOLDS` is the default instance; `FeedbackLoop` accepts a `thresholds` argument and passes it to `decide`.
- `Settings` — a frozen dataclass: `write_concern` (`str | int` — `"majority"` or an integer `w`), `read_concern` (a `ReadConcern` level string), `read_preference` (one of the string keys `AdaptiveClient` understands). Field values are chosen to match exactly what `AdaptiveClient` already accepts.
- Three named, module-level `Settings` constants so tests/callers compare against them directly instead of hand-constructing new instances:
  - `SAFE_SETTINGS = Settings("majority", "majority", "primary")`
  - `FAST_SETTINGS = Settings(1, "local", "nearest")`
  - `MODERATE_SETTINGS = Settings(1, "local", "primaryPreferred")`
- `settings_name(settings)` — `"SAFE"`, `"FAST"`, `"MODERATE"` for the three named configurations, otherwise `"CUSTOM"`; used by the switch log, the probe tally and the analysis.
- `decide(stats, thresholds=DEFAULT_THRESHOLDS) -> Settings`:
  1. a window with no operations at all → `MODERATE_SETTINGS` (no evidence for either extreme);
  2. `stats.write_share > thresholds.high_write_share` → `SAFE_SETTINGS`;
  3. `stats.write_share < thresholds.low_write_share` → `FAST_SETTINGS`;
  4. otherwise → `MODERATE_SETTINGS`.

All comparisons are strict (`>`/`<`), so a share sitting exactly on a threshold falls into the more moderate side. `decide` no longer reads `write_freq` or `read_write_ratio`.

**`adaptive_layer/feedback.py`**
Defines `FeedbackLoop` — described in its own docstring as "the actual novel contribution of the project" (everything before it is plumbing that makes this phase possible). Wires Monitor → Decision Engine → `AdaptiveClient.set_active_settings` together automatically, on a timer, with a cooldown to prevent oscillation ("flapping") under bursty load.

- `SwitchEvent(timestamp, collection, old, new, stats)` — a frozen dataclass: one real settings change, with the `Stats` that triggered it.
- `__init__(client, monitor, interval_seconds=30, cooldown_seconds=60, clock=time.time, thresholds=DEFAULT_THRESHOLDS)` — `clock` is injectable specifically so tests can supply a fake, controllable clock instead of real wall-clock time / real sleeping; `thresholds` is passed to `decide`. Tracks `self._last_switch: dict[collection -> timestamp of last actual settings change]` (not last evaluation — see below) and `self.switch_log: list[SwitchEvent]` (oldest first; read it with `list(loop.switch_log)` from another thread).
- `tick()` — one evaluation pass: for every collection `monitor.known_collections()` reports, pull `stats`, compute `decision = decide(stats, thresholds)`, compare against `client.get_active_settings(collection)`. If they're already equal, skip entirely (a no-op decision never touches the cooldown timer — cooldown only gates *real* switches, not routine "no change needed" evaluations). Otherwise, only apply the switch if no prior switch happened within `cooldown_seconds`; if still cooling down, suppress it. A switch that is applied is appended to `switch_log`. `tick()` is deliberately public and separate from the threaded loop so tests (and manual scripts) can drive one evaluation pass directly and deterministically.
- `run()` — blocking loop: `tick()`, then wait `interval_seconds` (via `threading.Event.wait`, which is also how `stop()` interrupts the wait early), repeat until stopped.
- `start()` — raises `RuntimeError` if already running; otherwise clears the stop event and starts `run()` on a daemon background thread.
- `stop(join_timeout=None)` — signals the stop event and joins the background thread (default timeout: `interval_seconds + 1`).

---

### `benchmark/` — evaluation harness

**`benchmark/__init__.py`**
Empty. Marks `benchmark` as a package (needed for `python -m benchmark.staleness_test`-style invocation, which is required — invoking the staleness test as a bare script instead breaks its internal package imports).

**`benchmark/locustfile.py`**
The Locust-based workload generator — the shared engine both `run_benchmark.py` and manual/standalone Locust runs drive. Chosen deliberately over a hand-rolled `asyncio` generator so that rate control, ramping, and per-request-type percentile stats come for free instead of being reimplemented.

- Configuration is entirely via environment variables (so `run_benchmark.py` can drive it across configurations without editing code): `BENCHMARK_MODE` (`static_fast` | `static_safe` | `adaptive`, default `adaptive`), `WORKLOAD_PRESET` (one of the nine presets below, default `MIXED_BURSTY`), `BENCHMARK_COLLECTION` (default `bench`), `BENCHMARK_KEY_SPACE` (default `1000` distinct synthetic keys), `BENCHMARK_OPS_PER_SEC` (per-user target via Locust's `constant_throughput`, default `5`), `BENCHMARK_PAYLOAD_BYTES` (size of the random payload on every written document, default `2048`), `BENCHMARK_SWITCH_LOG` (path; an adaptive run writes its switch log there), `BENCHMARK_PROBE_LOG` (path; the probe tally is written there), `BENCHMARK_PROBE_FRACTION` (share of iterations that are staleness probes, default `0.02`, `0` disables).
- Production-scale `FeedbackLoop` tuning for adaptive mode (distinct from the short values used in unit/integration tests): `WINDOW_SECONDS=15`, `INTERVAL_SECONDS=5`, `COOLDOWN_SECONDS=10`.
- `current_write_probability(preset, now=None)` — the fraction of operations that are writes, per preset:

  | Preset | Write probability |
  |---|---|
  | `READ_HEAVY` | 0.05 |
  | `WRITE_HEAVY` | 0.95 |
  | `MIXED_BURSTY` | alternates 0.95 / 0.05 every 15 s (time-modulo, not a Locust `LoadTestShape`, because that controls user count, not the read/write mix) |
  | `BALANCED`, `HOTSPOT`, `RAMP`, `HIGH_CONCURRENCY` | 0.5 (each preset's signal lies elsewhere: key distribution or the load shape set by the runner) |
  | `MODEST_WRITES` | 0.25 (between the two write-share thresholds, so it exercises MODERATE) |
  | `SPIKE` | 0.05, but 0.9 for the first 3 s of every 20 s |

- `pick_key(preset, key_space)` — uniform over the key space, except `HOTSPOT`, where 80% of operations hit the hottest 5% of keys.
- `on_test_start` — builds one **shared** `AccessPatternMonitor`/`AdaptiveClient` for the entire test run (not one per simulated user — `pymongo.MongoClient` is meant to be shared/pooled, and "adaptive" mode specifically needs every simulated user observing and reacting to the *same* aggregate traffic pattern), with per-operation printing off. It **drops the benchmark collection** (every run starts empty) and **indexes `key`** (otherwise reads scan a growing collection). For `static_fast`/`static_safe` it sets the fixed settings once; for `adaptive` it starts a real `FeedbackLoop`. It also records the start time and creates the probe tally. Shared state is attached to Locust's `environment` object.
- `on_test_stop` — stops the feedback loop, writes the switch log (adaptive, if `BENCHMARK_SWITCH_LOG` is set) and the probe tally (if `BENCHMARK_PROBE_LOG` is set), and closes the shared client.
- `AdaptiveMongoUser(User)` — one simulated concurrent caller, `wait_time = constant_throughput(OPS_PER_SEC_PER_USER)`. Its single `@task`, `read_or_write`, picks a key, and then either runs a **probe** (with probability `PROBE_FRACTION`) or an ordinary operation chosen by `current_write_probability`. Written documents are `{key, ts, payload}` with a random payload. A probe is a write followed immediately by a read of that document by `_id`; the write is reported to Locust as `write`, the read as `probe_read`, and the probe is tallied under the settings in force when the read ran — a read that does not find the document is a stale read. Stale probes are a measurement, not an error, so they are never reported to Locust as failures. Every call is timed, exceptions are caught and reported (Locust wants failures *reported*, not raised), and results are fired manually via `environment.events.request.fire(request_type="mongo", name=..., ...)` so Locust's stats engine tracks MongoDB calls like HTTP requests.

**`benchmark/run_benchmark.py`**
The benchmark orchestrator. For each repeat, each injected latency level, each of the 3 configurations and each of the 9 presets, it shells out to `locust` headlessly with the right environment variables, letting Locust itself write the stats CSVs.

- Each combination is run as a **separate subprocess**, not imported in-process — because Locust monkey-patches `ssl` via `gevent` on import, which can conflict with `pymongo`'s own `ssl` usage if both share a process. Shelling out avoids that entirely and gives clean process isolation between runs.
- Load shapes: a steady default (`FULL_DEFAULT`: 50 users, spawn rate 20/s, 300 s; `QUICK_DEFAULT`: 10 users, 5/s, 20 s) with per-preset overrides where the preset's signal depends on the load shape — `RAMP` (slow spawn, 1 user/s), `SPIKE` (180 s full, 40 s quick, so several spike cycles fit), `HIGH_CONCURRENCY` (200 users, 50/s full; 40 users quick). `params_for(preset, quick, duration_scale=1.0)` returns a copy of a preset's parameters with the duration scaled (floor of 30 s).
- `results_dir_for(latency_ms, rep)` — `results/write_share_eval/latency_<N>ms/rep<k>/`, a folder separate from any earlier results.
- `run_one(config, preset, params, results_dir)` — builds a unique per-combination collection name and CSV prefix, sets the environment (including `BENCHMARK_SWITCH_LOG` for adaptive and `BENCHMARK_PROBE_LOG` for every run), runs the `locust` subprocess with `--headless`/`--csv`/`--only-summary`, and on success hands the CSV prefix to `_summarize_stats_csv`; on a non-zero exit code prints the tail of stdout/stderr and returns `None`.
- `_summarize_stats_csv(csv_prefix)` — reads Locust's `_stats.csv`, finds the `"Aggregated"` row, and extracts request count, failure count, requests/sec, and p50/p95/p99 latency.
- `run_suite(latency_ms, quick, presets, configs, rep, duration_scale)` — applies the latency level, runs every configuration × preset, and **always clears the injected latency** in a `finally` block so the cluster is never left slowed.
- `main()` — CLI: `--quick`, `--latency <ms ...>` (default 0 5 43), `--repeats` (default 3, or 1 with `--quick`), `--duration-scale`. Loops repeats outermost, then latency levels, prints a summary table per sweep, and exits with status `1` if anything failed.

**`benchmark/netem.py`**
Inter-node latency injection for the 3-node Docker replica set, using `tc netem` inside each container. A `prio` queueing discipline with a `u32` filter on the destination IPs of the two peers delays only node-to-node packets (replication, majority acknowledgement, secondary lag), never the load generator's own connection. The delay is **one-way** per node, so the node-to-node round trip is about twice the value.

- `LATENCY_LEVELS_MS = [0, 5, 43]` (the default sweep) and `ALL_LATENCY_LEVELS_MS = [0, 1, 5, 33, 43]`; `LATENCY_LABELS` names each (baseline; same-region cross-AZ; neighbouring regions; cross-country; transatlantic). The module docstring records where the values come from (AWS and Azure published latency data).
- `apply_latency(delay_ms)` (0 just clears), `clear()` (safe to call repeatedly), `measure_rtt_ms(src, dst, count)` (average ping round trip, used to verify the injection).

**`benchmark/switch_log.py`**
`write_switch_log(path, switch_log, started_at, stopped_at)` writes, for one adaptive run, a CSV with a `start` row (t=0, MODERATE, the client default), one `switch` row per real settings change (time since start, the settings that became active, and the write share and counts that triggered it), and a `stop` row. `time_in_settings(path)` turns such a file into seconds spent in each of FAST / MODERATE / SAFE plus the number of switches — the direct evidence of which settings the adaptive layer actually used.

**`benchmark/probe_log.py`**
`ProbeTally` counts, per settings name, how many staleness probes ran and how many read back stale (`record(settings, fresh)`, `totals()`, `write(path)`); `read_probe_csv(path)` reads a tally back. Probe misses go to this CSV, not to Locust's failure count.

**`benchmark/staleness_test.py`**
The staleness experiment — the metric that demonstrates the consistency *trade-off*, since latency/throughput alone say nothing about correctness risk. Method: write a document containing a unique marker, then immediately read it back using whatever read preference the active configuration specifies; a "miss" means the read landed on a replica that hadn't caught up yet. Each trial also records which settings were in force when its read ran.

- Four scenarios, each producing `hits`/`misses`/`stale_rate`/`in_expected_settings` (trials that ran under the settings the scenario is named for):
  - `static_fast` — `FAST_SETTINGS` fixed the whole time, back-to-back trials.
  - `static_safe` — `SAFE_SETTINGS` fixed the whole time.
  - `adaptive_hot` — a real `FeedbackLoop` (window 6 s, interval 2 s, cooldown 3 s) is warmed up with 60 writes until it reaches `SAFE_SETTINGS` (polled via `_wait_until_settings`, 15 s timeout), then measured; back-to-back trials are 50% writes, which keeps it SAFE.
  - `adaptive_cold` — warmed up with a seed write + 30 reads until it reaches `FAST_SETTINGS`, then measured with each trial followed by `COLD_FILLER_READS_PER_TRIAL=9` reads (about 9% writes, comfortably under the 0.15 threshold) and a `COLD_TRIAL_SPACING_SECONDS=2` pause, so it stays read-heavy and the secondaries get time to catch up between isolated writes.
- `FULL_TRIALS=200`, `QUICK_TRIALS=40`; `adaptive_cold`'s trial count is separately capped at `min(trials, 60)`, since its pacing would otherwise dominate total runtime.
- `_one_trial(client, collection, seq)` — returns `(fresh, settings_in_force)`.
- `measure_static` / `measure_adaptive_hot` / `measure_adaptive_cold` — each sets up the client/monitor/loop, warms up if needed, runs the trials, and always tears down (drops its throwaway collection, stops any loop, closes the client).
- `run_suite(latency_ms, trials, rep)` — applies a latency level, runs all four scenarios, always clears the latency, and writes `results/write_share_eval/latency_<N>ms/rep<k>/staleness.csv`. `main()` accepts `--quick`, `--latency`, `--repeats`.
- `adaptive_hot` vs `static_safe` and `static_fast` is a like-for-like comparison (all under sustained write pressure); `adaptive_cold` is paced, so it is **not** directly comparable to `static_fast`.

**`benchmark/analyze_results.py`**
Turns the raw output into tables and charts. Runnable as `python benchmark/analyze_results.py`, and imported by the notebook.

- Loading: `load_benchmark_summary(dir)` reads each `<config>_<preset>_stats.csv` (the `Aggregated` row for overall p50/p95/p99, throughput and failures, and the separate `read` and `write` rows for per-operation latency); `load_staleness(dir)`; `rep_dirs(root, levels, max_rep)` finds the `latency_<N>ms/rep<k>` folders (the filters let a run ignore leftovers from an earlier smoke run); `load_all`, `load_switch_timelines`, `load_probes`.
- Aggregation over repeats: `aggregate_benchmark` (mean and sample standard deviation of each latency metric; NaN spread for a single repeat), `pool_staleness` (trials pooled; the spread is the standard deviation of the per-repeat rates), `aggregate_timelines` (mean share of run time per setting), `pool_probes` and `probes_by_settings` (probe stale rates pooled over repeats, and split by the settings in force).
- Charts: per-latency grouped bars (latency percentiles, throughput, adaptive overhead, staleness) and cross-latency charts — p50 and p95 against injected latency (error bars = standard deviation over repeats, with a distinct marker and line style per configuration so coinciding lines stay visible), staleness against latency, the settings the adaptive layer used, and the in-load probe stale rates.
- `run_analysis(root, verbose, levels, max_rep)` loads everything, writes `summary_reps.csv`, `summary_agg.csv`, `staleness_reps.csv`, `staleness_pooled.csv`, `settings_timeline.csv`, `settings_timeline_agg.csv`, `probe_reps.csv`, `probe_pooled.csv`, `probe_by_settings.csv` and all figures into `results/write_share_eval/`, and returns an `Analysis` named tuple of the tables.

---

### `tests/`

**`tests/__init__.py`**
Empty. Marks `tests` as a package.

**`tests/test_monitor.py`** (9 tests)
Pure unit tests for `AccessPatternMonitor` — no database, no real `sleep()`, all timestamps are synthetic floats. Covers: an unknown collection returns `NEUTRAL_STATS`; basic read/write counting and ratio computation; a read-only collection has an infinite ratio; a collection with reads but no writes has `seconds_since_last_write is None`; events older than the window get pruned out of `get_stats()`; a collection can be pruned all the way back to `NEUTRAL_STATS`; `record()` itself prunes immediately; an invalid `op_type` raises `ValueError`; a zero-or-negative `window_seconds` is rejected at construction.

**`tests/test_decision.py`** (22 tests)
Pure unit tests for the Decision Engine and `Thresholds` — `Stats` constructed directly, no DB, no monitor. Covers: one test per outcome (write-heavy → SAFE; read-heavy → FAST; in-between → MODERATE; a brand-new/`NEUTRAL_STATS` collection → MODERATE); writes-only and reads-only windows; **scale invariance** (the same mix gives the same decision at 0.05, 5 and 500 writes/s); boundary cases proving a share sitting exactly on either threshold falls to the moderate side (strict comparisons) and just past it switches; custom thresholds change the decision; the defaults equal the module constants; invalid `Thresholds` are rejected; a `None` `seconds_since_last_write` doesn't crash; the `write_share` property; and a parametrized check that the three named settings are well-formed.

**`tests/test_feedback.py`** (10 tests)
Pure unit tests for `FeedbackLoop.tick()` — a `FakeClock`, a `FakeMonitor` (stats set directly) and a `FakeClient` (records every `set_active_settings` call) stand in for all real dependencies — no database, no real threading, no real sleeping. Covers: a collection's first-ever decision applies immediately with no cooldown check; a would-be switch within the cooldown is suppressed and applied once it has elapsed; a "no change needed" tick doesn't call `set_active_settings` or touch the cooldown timer; multiple collections are evaluated independently; a tick with no known collections does nothing; moderate stats never trigger a switch from the default; the loop uses the thresholds it is given; and the switch log records each real switch with its trigger stats and ignores suppressed or unchanged decisions.

**`tests/test_switch_log.py`** (3 tests)
`settings_name` for the named configurations and `"CUSTOM"`; `time_in_settings` adds up to the run length for a run with switches; a run with no switches is all MODERATE.

**`tests/test_probe_log.py`** (3 tests) and **`tests/_locustfile_probe_check.py`**
The probe tally and its CSV round trip (including an empty file), plus an end-to-end test of the real locustfile task: the helper script runs in a **separate process** (importing the locustfile applies gevent monkey-patching, which must not leak into pytest) against a fake client that never finds a just-written document, and checks that about the configured fraction of iterations are probes, every probe read is by `_id`, every probe is tallied stale under the right settings, the probe's write and read are reported to Locust under `write` and `probe_read`, and nothing is reported as a failure.

**`tests/test_run_benchmark.py`** (8 tests)
Runner plumbing with `run_one` and `netem` replaced by fakes: duration scaling (and that it does not mutate the parameter tables); the `results/write_share_eval/latency_<N>ms/rep<k>` layout; the default sweep and nine presets; `run_suite` runs every combination and always clears the latency, including when a run raises; and that only adaptive runs get a switch-log path while every run gets a probe-log path.

**`tests/test_analyze_results.py`** (11 tests)
The analysis pipeline on a synthetic results tree with known answers: finding latency/repeat folders and the level/repeat filters; mean and sample standard deviation over repeats (and NaN spread, not a crash, for one repeat); read and write latency parsed separately (NaN when absent); pooled staleness and the spread of per-repeat rates; time-in-settings shares; probes pooled over repeats and split by settings; an empty probe file; and that `run_analysis` writes every CSV and every figure.

**`tests/test_feedback_integration.py`** (1 test, needs the replica set)
An end-to-end integration test for `FeedbackLoop` against the **real** replica set, the **real** `AdaptiveClient`, the **real** `AccessPatternMonitor`, and a **real** background thread via `start()`/`stop()` — the exact stack `demo_app.py` runs. Uses short window/interval/cooldown values (`WINDOW_SECONDS=2`, `INTERVAL_SECONDS=1`, `COOLDOWN_SECONDS=0`). `test_feedback_loop_escalates_on_write_burst_then_relaxes_on_reads` drives 20 rapid writes and asserts the settings escalate to `SAFE_SETTINGS`, then waits for the window to age out, drives 10 reads, and asserts they relax to `FAST_SETTINGS`.

**`tests/test_client_smoke.py`** (4 tests, needs the replica set)
Integration tests for `AdaptiveClient` against the real replica set. `_read_with_retry` acknowledges that `FAST_SETTINGS` can legitimately route a read to a secondary that hasn't replicated a just-written document yet — the exact staleness trade-off this project studies, not a bug — so tests using `FAST_SETTINGS` retry briefly. Tests cover: a `SAFE_SETTINGS` write followed by an immediate read round-trips correctly; `_log_op` really feeds a live `AccessPatternMonitor` with correct counts and ratio; two collections given opposite settings through the *same* client behave independently; and a never-configured collection defaults to `MODERATE_SETTINGS`. Also runnable directly as `python tests/test_client_smoke.py`.

---

### `results/`

Generated output, gitignored. The current evaluation writes only to `results/write_share_eval/`:

```
results/write_share_eval/
├── latency_<N>ms/rep<k>/        raw output of one (latency, repeat) sweep
│   ├── <config>_<preset>_stats.csv, _stats_history.csv, _failures.csv, _exceptions.csv   (Locust)
│   ├── adaptive_<preset>_switches.csv   settings the adaptive layer chose, and when
│   ├── <config>_<preset>_probe.csv      stale probe reads, by settings in force
│   └── staleness.csv                    the four staleness scenarios
├── latency_<N>ms/fig_*.png      per-latency charts (means over repeats)
├── summary_reps.csv, summary_agg.csv, staleness_reps.csv, staleness_pooled.csv,
│   settings_timeline.csv, settings_timeline_agg.csv, probe_reps.csv, probe_pooled.csv, probe_by_settings.csv
└── fig_sweep_p50.png, fig_sweep_p95.png, fig_sweep_staleness.png, fig_settings_used.png, fig_probe_staleness.png
```

Any files directly under `results/` (and `results/latency_*ms/`) are from the first single-run evaluation and are never overwritten by the new one.

---

### Not covered above (tooling/build artifacts, not part of the project's source)

- **`.pytest_cache/`** and **`__pycache__/`** — auto-generated by `pytest`/Python between runs. Not source; safe to delete/regenerate at any time.
- **`.claude/scheduled_tasks.lock`** — local Claude Code tooling state, explicitly gitignored (`.claude/` in `.gitignore`) and unrelated to the project's own logic.

---

## Appendix: The three named consistency configurations, at a glance

| Name | Write concern | Read concern | Read preference | Chosen when | Trade-off |
|---|---|---|---|---|---|
| **SAFE_SETTINGS** | `"majority"` | `"majority"` | `primary` | write share over `HIGH_WRITE_SHARE` (0.40): write-heavy | Slowest once nodes are far apart, but correctness-guaranteed — every write is durable across a majority before it's acknowledged, every read goes to the primary. |
| **MODERATE_SETTINGS** | `1` | `"local"` | `primaryPreferred` | write share between the two thresholds, an empty window, and brand-new/never-seen collections | The balanced default — fast-ish writes, reads prefer the primary but can fail over to a secondary. |
| **FAST_SETTINGS** | `1` | `"local"` | `nearest` | write share under `LOW_WRITE_SHARE` (0.15): read-heavy | Fastest — reads are routed to whichever node answers quickest, with minimal write durability guarantees. The staleness risk is accepted here because few operations are writes, but it is a real cost that grows with the distance between nodes; the staleness measurements exist to quantify it. |
