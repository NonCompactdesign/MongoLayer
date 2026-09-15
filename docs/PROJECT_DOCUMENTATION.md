# Adaptive Consistency Layer for MongoDB — Project Documentation

> NoSQL Databases (BCSE406L), VIT — DA team project.
> This document explains the problem being solved, the solution built, the system architecture, how every file in the repository maps into that architecture, the runtime data flow, and a description of every individual file in the project.

---

## 1. The Problem

MongoDB (like most distributed/replicated databases) exposes **tunable consistency knobs** on every read and write:

- **Write concern** (`w`) — how many replica-set members must acknowledge a write before it's considered successful (`w=1` = just the primary, `w="majority"` = a majority of the set).
- **Read concern** — what guarantee a read gives about the data it returns (`"local"` = whatever the node has, possibly stale/uncommitted from the cluster's perspective; `"majority"` = only data acknowledged by a majority).
- **Read preference** — which node(s) a read is allowed to be routed to (`primary`, `primaryPreferred`, `secondary`, `secondaryPreferred`, `nearest`).

There is a hard, well-known trade-off behind these knobs: **safety costs latency, and speed costs staleness risk.** `w="majority"` + `read_preference="primary"` is safe but slow (every write waits on multiple nodes; every read is funneled through one node). `w=1` + `read_preference="nearest"` is fast but can return stale or even not-yet-durable data if a secondary hasn't replicated the latest write yet.

In almost every real application, these settings are chosen **once, by hand, and left fixed** for the whole application (or at best, fixed per collection at the code level). But real workloads are not static — the same collection can be write-heavy during a bulk import, read-heavy during normal browsing, and bursty around specific events. A single fixed setting is necessarily a compromise: too safe (and slow) for the quiet periods, or too fast (and risky) for the hot periods.

**The problem this project addresses:** nothing in the standard MongoDB/pymongo stack *observes* how a collection is actually being used right now and *automatically* adjusts the consistency knobs in response. That adaptation has to be hand-tuned or left as a static, one-size-fits-none compromise.

---

## 2. The Solution

**Adaptive Consistency Layer** is a Python middleware that sits between the application and MongoDB (via `pymongo`) and closes the loop automatically:

1. **Observe** — every read/write that flows through the middleware is logged with its collection name, operation type, and timestamp.
2. **Analyze** — a sliding-window monitor turns that stream of logged operations into per-collection access-pattern statistics (read count, write count, read:write ratio, write frequency, time since last write).
3. **Decide** — a pure, rule-based decision function maps those statistics to one of three named consistency configurations:
   - **SAFE** (`w="majority"`, read concern `"majority"`, read preference `primary`) — for hot, write-heavy collections, where correctness matters more than speed.
   - **FAST** (`w=1`, read concern `"local"`, read preference `nearest`) — for cold, read-heavy collections, where speed matters more and staleness risk is low anyway (few writes to be stale about).
   - **MODERATE** (`w=1`, read concern `"local"`, read preference `primaryPreferred`) — the safe, balanced default for everything in between (and for any collection with no observed traffic yet).
4. **Act** — a background feedback loop re-evaluates every known collection on a timer and, subject to a cooldown (to prevent rapid oscillation/"flapping" under bursty load), pushes the new settings into the client. The **very next** operation on that collection automatically uses the new settings — no application code changes required.

This is validated in three ways:
- A **live demo app** (`demo_app.py`) and a **live dashboard** (`dashboard/`) that show the settings visibly changing in response to real traffic against a real 3-node MongoDB replica set.
- A **benchmark harness** (`benchmark/`, built on Locust) that drives the exact same read/write workloads against three configurations — `static_fast`, `static_safe`, and `adaptive` — and compares latency/throughput.
- A **staleness measurement** (`benchmark/staleness_test.py`) that directly measures read-after-write staleness per configuration, proving the adaptive layer's speed doesn't come at a uniformly higher correctness cost — it grants "fast" settings specifically when the staleness cost of doing so is low.

---

## 3. System Architecture

### 3.1 Component overview

```
                              ┌───────────────────────────────────────────┐
                              │              Application code             │
                              │   demo_app.py · dashboard/app.py ·        │
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
                      │  (SAFE / FAST / MODERATE)    │
                      └──────────────┬──────────────┘
                                     │ decide(stats)
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
                      └──────────────────────────────┘
```

`AdaptiveClient`, `AccessPatternMonitor`, `Decision Engine`, and `FeedbackLoop` together make up the `adaptive_layer` package — the reusable middleware. Everything else in the repo (`demo_app.py`, `dashboard/`, `benchmark/`, `tests/`) either *drives* this middleware to prove it works, or *measures* how well it works.

### 3.2 Component responsibilities

| Component | File | Responsibility |
|---|---|---|
| **AdaptiveClient** | `adaptive_layer/client.py` | The only thing application code talks to. Wraps `pymongo.MongoClient`. Exposes `write()`/`read()` with *no* consistency arguments — it looks up each collection's currently active `Settings` internally and applies them to the underlying pymongo call. Also the single mutation point (`set_active_settings`) that the feedback loop uses to change behavior live. |
| **AccessPatternMonitor** | `adaptive_layer/monitor.py` | Turns a stream of `(collection, op_type, timestamp)` events into a live, sliding-window `Stats` snapshot per collection. Fully decoupled from MongoDB — unit-testable with synthetic timestamps. |
| **Decision Engine** | `adaptive_layer/decision.py` | A pure function `decide(stats) -> Settings`. No I/O, no state. Encodes the three-branch policy (hot/write-heavy → SAFE, cold/read-heavy → FAST, else → MODERATE) against named threshold constants. |
| **FeedbackLoop** | `adaptive_layer/feedback.py` | The orchestrator that closes the loop: on a timer, pulls stats from the Monitor, runs the Decision Engine, and pushes changed decisions into the Client — subject to a cooldown that prevents rapid flapping. Runs as a background thread via `start()`/`stop()`; `tick()` is exposed separately for deterministic testing. |
| **Demo app** | `demo_app.py` | Scripts three traffic phases (read-heavy, write-burst, mixed) through the real stack against the real replica set and prints the settings adapting live — a console-based proof. |
| **Dashboard** | `dashboard/` | A Flask web app + vanilla JS/HTML/CSS frontend that runs the real stack continuously, with a UI to drive traffic live (sliders, presets) and watch the Monitor stats, active settings, and each replica-set node's real op counters update in real time. |
| **Benchmark harness** | `benchmark/` | Locust-based load generator plus a runner that drives `static_fast` / `static_safe` / `adaptive` configurations across three workload presets, capturing latency/throughput (and, separately, staleness) numbers for the evaluation section of the report. |
| **Tests** | `tests/` | Unit tests (Monitor, Decision Engine, FeedbackLoop with a fake clock/client/monitor — no DB) and integration tests (Client and FeedbackLoop against the real replica set). |

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
      decision  = decide(stats)                      # Decision Engine — pure function
      current   = client.get_active_settings(collection)

      if decision == current:
          continue                                    # nothing to do, cooldown untouched

      last_switch = last_switch_time[collection]
      if last_switch is not None and (now - last_switch) < cooldown_seconds:
          continue                                    # would switch, but suppressed by cooldown

      client.set_active_settings(collection, decision) # <-- the only mutation
      last_switch_time[collection] = now
```

`run()` simply calls `tick()`, sleeps `interval_seconds`, and repeats until `stop()` is called — driven by a background `threading.Thread` started from `start()`.

### 4.3 End-to-end flow (demo / dashboard)

```
 ┌────────────┐   write()/read()   ┌────────────────┐  record()  ┌──────────────────────┐
 │ Traffic     │ ─────────────────►│ AdaptiveClient  │──────────► │ AccessPatternMonitor  │
 │ source      │                   │                 │            │ (sliding window)      │
 │ (demo_app / │◄──────────────────┤                 │            └──────────┬────────────┘
 │  Traffic-    │  applies active   └───────┬─────────┘                       │ get_stats()
 │  Generator)  │  settings for the │       │ actual pymongo call             ▼
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

The **dashboard** additionally polls `serverStatus().opcounters` on each of the three MongoDB nodes directly (bypassing the replica-set-aware URI, so each node answers about itself) to show, live, which node writes/reads are actually landing on — visually confirming that the active read preference is really being honored.

---

## 5. Repository Map

```
MongoLayer/
├── README.md                       Entry point / quickstart
├── requirements.txt                 Python dependencies
├── docker-compose.yml               3-node MongoDB replica set definition
├── .gitignore
│
├── plan/
│   └── exec_plan.md                 Full phase-by-phase build plan (DA-I → DA-III)
│
├── docs/
│   ├── replica_set_setup.md         One-time replica set setup notes
│   ├── SHOWCASE.md                  Live-demo/showcase runbook
│   ├── PROJECT_DOCUMENTATION.md     This file
│   └── demo_runs/
│       └── demo_run_2026-09-10.log  Saved transcript of a clean demo_app.py run
│
├── adaptive_layer/                  THE MIDDLEWARE (reusable package)
│   ├── __init__.py                  (empty — marks the package)
│   ├── client.py                    AdaptiveClient (proxy/interceptor)
│   ├── monitor.py                   AccessPatternMonitor + Stats
│   ├── decision.py                  Decision Engine (decide()) + Settings + thresholds
│   └── feedback.py                  FeedbackLoop (background adaptation thread)
│
├── demo_app.py                      Scripted console demo (Phase 7)
│
├── dashboard/                       Live web dashboard (Flask + vanilla JS)
│   ├── __init__.py                  (empty — marks the package)
│   ├── app.py                       Flask backend + JSON API
│   ├── recording_monitor.py         RecordingMonitor (Monitor decorator w/ live feed)
│   ├── traffic_generator.py         TrafficGenerator (controllable read/write source)
│   └── static/
│       ├── index.html               Dashboard page markup
│       ├── app.js                   Dashboard frontend logic (polling + controls)
│       └── style.css                Dashboard styling
│
├── benchmark/                       Evaluation / load-testing harness
│   ├── __init__.py                  (empty — marks the package)
│   ├── locustfile.py                Locust workload generator (Phase 8)
│   ├── run_benchmark.py             Orchestrates configs × presets via Locust (Phase 9)
│   ├── analyze_results.py           (placeholder — Phase 11, not yet implemented)
│   └── staleness_test.py            Read-after-write staleness measurement (Phase 10)
│
├── tests/
│   ├── __init__.py                  (empty — marks the package)
│   ├── test_monitor.py              Unit tests: AccessPatternMonitor (no DB)
│   ├── test_decision.py             Unit tests: Decision Engine (no DB)
│   ├── test_feedback.py             Unit tests: FeedbackLoop, fake clock/client/monitor
│   ├── test_feedback_integration.py Integration test: FeedbackLoop vs real replica set
│   └── test_client_smoke.py         Integration tests: AdaptiveClient vs real replica set
│
└── results/
    └── .gitkeep                     Keeps the (gitignored) results/ directory in git
```

---

## 6. File-by-File Reference

### Root

**`README.md`**
Project entry point. States the one-line pitch (workload-aware middleware that dynamically tunes write concern / read concern / read preference), identifies it as the VIT NoSQL (BCSE406L) DA team project, links out to `plan/exec_plan.md` and `docs/SHOWCASE.md`, and gives the quickstart (`venv` + `pip install -r requirements.txt`), noting the Docker Compose replica set is a hard prerequisite.

**`requirements.txt`**
Pinned-by-name (not version-pinned) dependency list: `pymongo` (MongoDB driver), `pytest` (test runner), `pandas` (results aggregation, Phase 11), `matplotlib` (result plotting, Phase 11), `locust` (benchmark load generator), `flask` (dashboard web server).

**`docker-compose.yml`**
Defines the 3-node local MongoDB replica set the entire project depends on. Three `mongo:7.0` services (`mongo1`, `mongo2`, `mongo3`), each running `mongod --replSet rs0 --bind_ip_all --port <27017|27018|27019>`, each on its own named volume (so data survives container restarts) and its own host port mapping, all sharing one bridge network (`rs0net`). A standalone `mongod` cannot demonstrate any of this project's logic — write concern, read concern, and read preference only produce observable differences against a real multi-node replica set.

**`.gitignore`**
Standard Python ignores (`__pycache__/`, `*.pyc`, `.venv/`, `venv/`), Node ignores (unused here, leftover boilerplate), benchmark output (`results/*.csv`, `results/*.png` — regenerated by every run, not checked in), OS cruft (`.DS_Store`, `Thumbs.db`), and local tooling state (`.claude/`).

**`demo_app.py`**
The Phase 7 scripted console demo. Builds the real stack (`AccessPatternMonitor` with an 8s window, `AdaptiveClient` wired to that monitor, `FeedbackLoop` with a 2s interval / 3s cooldown) against the real replica set (`demo.live_demo` collection) and runs it end-to-end with **nothing mocked**. Three phases, each separated by a pause long enough for the monitor's window to fully clear so results aren't contaminated by the previous phase:
- **Phase A (read-heavy):** 20 reads, no writes → expects settings to relax to `FAST_SETTINGS`.
- **Phase B (write burst):** 60 writes as fast as possible → expects settings to escalate to `SAFE_SETTINGS`.
- **Phase C (mixed):** an interleaved mix of reads/writes at a moderate pace → expects settings to settle at `MODERATE_SETTINGS`.

After each phase it prints the monitor's stats and the active settings, flags whether they matched the expected branch, and cleans up (`loop.stop()`, drops the demo collection, closes the client) in a `finally` block regardless of outcome. Run with `python demo_app.py`.

---

### `plan/`

**`plan/exec_plan.md`**
The full execution plan for the whole project, broken into 14 phases (0–13) from repo bootstrap through the DA-III final report/demo/slides, each with a Goal, Why, numbered Steps, and a Definition of Done. Includes a milestone map tying phase ranges to DA-II/DA-III submission checkpoints, and an "Open decisions to revisit" section covering per-collection vs. per-document monitoring granularity, an optional ML-based decision engine as a stretch goal, and how the work could be split across a 2–3 person team. This is the authoritative build plan the rest of the repository was implemented against — phase numbers referenced elsewhere in code comments (e.g. "Phase 3", "Phase 6") point back to sections of this document.

---

### `docs/`

**`docs/replica_set_setup.md`**
The one-time, detailed replica-set setup reference: why hosts-file entries for `mongo1`/`mongo2`/`mongo3` → `127.0.0.1` are required (so the same `host:port` pairs resolve correctly both from inside the Docker network and from the host machine running Python), the `docker compose up -d` + `rs.initiate()` sequence, a verification command, the pymongo connection string, a working smoke-test code snippet, and teardown commands (`docker compose down` vs. `down -v`).

**`docs/SHOWCASE.md`**
The "get it running right now" operational runbook for live demos/presentations. Covers: one-time setup (clone, venv, hosts file, first `docker compose up` + `rs.initiate()`); the every-time pre-showcase checklist (bring the replica set up, verify health, activate venv, run the test suite as a sanity check); the three ways to demo the system — the **live dashboard** (`python -m dashboard.app`, recommended, with a full walkthrough of its UI panels and the Reset Stats button's semantics), the **scripted console demo** (`python demo_app.py`, with a table of its three phases and what to watch for), and **supporting evidence** (the benchmark runner and staleness test, run live if time allows); shutdown commands; and a troubleshooting table for the most common failure symptoms (Docker not running, stale primary after failover, missing hosts entries, running a package script directly instead of via `python -m`, etc.).

**`docs/demo_runs/demo_run_2026-09-10.log`**
A saved, real console transcript of a successful `demo_app.py` run (dated 2026-09-10), captured as fallback evidence — usable to show the expected output without running the demo live, or as a backup if a live run fails during a presentation. Shows all three phases' per-operation log lines plus the final stats/settings printout for each phase, all matching their expected branch.

**`docs/PROJECT_DOCUMENTATION.md`**
This document.

---

### `adaptive_layer/` — the middleware package

**`adaptive_layer/__init__.py`**
Empty. Marks `adaptive_layer` as a regular Python package so `from adaptive_layer.client import AdaptiveClient` etc. resolve.

**`adaptive_layer/client.py`**
Defines `AdaptiveClient`, the thin `pymongo` proxy/interceptor every other component talks to.

- `__init__(uri, db_name, monitor=None)` — opens a `pymongo.MongoClient`, selects the database, optionally accepts an `AccessPatternMonitor` (or duck-typed equivalent, e.g. the dashboard's `RecordingMonitor`) to feed, and initializes `self._active_settings: dict[str, Settings]` (empty — collections default to `MODERATE_SETTINGS` until configured).
- `set_active_settings(collection, settings)` — the **only mutation point** in the class; the `FeedbackLoop` is the intended caller.
- `get_active_settings(collection)` — returns the collection's currently active `Settings`, defaulting to `MODERATE_SETTINGS` for any collection never explicitly configured (never raises).
- `write(collection, doc)` — looks up active settings, builds a `WriteConcern(w=settings.write_concern)`, times and performs `insert_one`, logs the op, returns the `InsertOneResult`.
- `read(collection, query)` — looks up active settings, builds `ReadConcern(level=settings.read_concern)` and resolves the read-preference string (`primary`/`primaryPreferred`/`secondary`/`secondaryPreferred`/`nearest`) to a pymongo `ReadPreference` object via the module-level `_READ_PREFERENCES` map (raising `ValueError` on an unrecognized name), times and performs `find_one`, logs the op, returns the document or `None`.
- `_log_op(collection, op_type, timestamp, latency_ms)` — prints a formatted line for visibility and, if a monitor was supplied, calls `monitor.record(...)` so real traffic actually populates the Monitor's stats.
- `close()` — closes the underlying `MongoClient`.

Notably, `write()`/`read()` take **no** consistency-related arguments — that seam was deliberately removed (per Phase 5 of the exec plan) so the `FeedbackLoop` can change a collection's effective behavior without any caller code changing.

**`adaptive_layer/monitor.py`**
Defines `AccessPatternMonitor` and its `Stats` output type — completely decoupled from MongoDB and from `AdaptiveClient` (it only ever sees `(collection, op_type, timestamp)` tuples), which is what makes it unit-testable with synthetic timestamps and no live database.

- `Stats` — a frozen dataclass: `read_count`, `write_count`, `read_write_ratio` (reads per write; `float('inf')` if there are reads but zero writes), `write_freq` (writes per second, averaged over the window), `seconds_since_last_write` (`None` if no write in-window).
- `NEUTRAL_STATS` — a module-level all-zero/`None` `Stats` instance returned for any collection with no (or no longer any) in-window events — so callers, especially the Decision Engine, never have to special-case "never seen this collection."
- `AccessPatternMonitor(window_seconds=30)` — raises `ValueError` if `window_seconds <= 0`. Internally: `collections.defaultdict(collections.deque)` mapping collection name → an oldest-first deque of `(timestamp, op_type)` tuples.
  - `known_collections()` — every collection name ever `record()`-ed (used by the `FeedbackLoop` to know what to evaluate each tick; a collection stays "known" even after all its events age out, correctly yielding `NEUTRAL_STATS` from `get_stats`).
  - `record(collection, op_type, timestamp)` — validates `op_type` is `"read"` or `"write"` (else `ValueError`), appends the event, then immediately prunes (so memory doesn't grow unbounded even if `get_stats()` is never called).
  - `get_stats(collection, now=None)` — defaults `now` to `time.time()`; returns `NEUTRAL_STATS` for an unknown or now-empty collection; otherwise prunes again (in case of a stale window edge), then computes read/write counts, the ratio (0.0 if no traffic at all, `inf` if reads-only), `write_freq` as `write_count / window_seconds`, and `seconds_since_last_write` from the max write timestamp.
  - `_prune(collection, now)` — pops events older than `now - window_seconds` off the left (oldest) end of the deque. Called both from `record()` and from `get_stats()`.

**`adaptive_layer/decision.py`**
Defines the Decision Engine — a deliberately pure function with zero side effects (no DB access, no state), so it's the easiest and most exhaustively unit-testable piece, and the one most likely to be scrutinized ("why did it pick majority write concern here?" should be answerable by one threshold constant).

- Named threshold constants (placeholders at time of writing, pending Phase 12 tuning against real benchmark evidence):
  - `HIGH_WRITE_THRESHOLD = 5.0` — writes/sec at or above this is "hot."
  - `READ_HEAVY_RATIO_THRESHOLD = 5.0` — read:write ratio above this is "read-heavy."
  - `LOW_WRITE_THRESHOLD = 0.5` — writes/sec below this is "cold."
- `Settings` — a frozen dataclass: `write_concern` (`str | int` — `"majority"` or an integer `w`), `read_concern` (a `ReadConcern` level string), `read_preference` (one of the string keys `AdaptiveClient` understands). Field values are chosen to match exactly what `AdaptiveClient` already accepts.
- Three named, module-level `Settings` constants so tests/callers compare against them directly instead of hand-constructing new instances:
  - `SAFE_SETTINGS = Settings("majority", "majority", "primary")`
  - `FAST_SETTINGS = Settings(1, "local", "nearest")`
  - `MODERATE_SETTINGS = Settings(1, "local", "primaryPreferred")`
- `decide(stats: Stats) -> Settings` — three ordered branches (order matters — hot/write-heavy is checked **first** and wins even if the collection also looks read-heavy, e.g. right after a burst with reads mixed in, because safety takes priority when both signals fire at once):
  1. `stats.write_freq > HIGH_WRITE_THRESHOLD` → `SAFE_SETTINGS`.
  2. `stats.read_write_ratio > READ_HEAVY_RATIO_THRESHOLD and stats.write_freq < LOW_WRITE_THRESHOLD` → `FAST_SETTINGS`.
  3. otherwise → `MODERATE_SETTINGS`.

All comparisons are strict (`>`/`<`), so a value sitting exactly on a threshold does **not** yet count as hot/cold — it falls through toward the safer/moderate branch.

**`adaptive_layer/feedback.py`**
Defines `FeedbackLoop` — described in its own docstring as "the actual novel contribution of the project" (everything before it is plumbing that makes this phase possible). Wires Monitor → Decision Engine → `AdaptiveClient.set_active_settings` together automatically, on a timer, with a cooldown to prevent oscillation ("flapping") under bursty load.

- `__init__(client, monitor, interval_seconds=30, cooldown_seconds=60, clock=time.time)` — `clock` is injectable specifically so tests can supply a fake, controllable clock instead of real wall-clock time / real sleeping. Tracks `self._last_switch: dict[collection -> timestamp of last actual settings change]` (not last evaluation — see below).
- `tick()` — one evaluation pass: for every collection `monitor.known_collections()` reports, pull `stats`, compute `decision = decide(stats)`, compare against `client.get_active_settings(collection)`. If they're already equal, skip entirely (a no-op decision never touches the cooldown timer — cooldown only gates *real* switches, not routine "no change needed" evaluations). Otherwise, only apply the switch if no prior switch happened within `cooldown_seconds`; if still cooling down, suppress it. `tick()` is deliberately public and separate from the threaded loop so tests (and manual scripts) can drive one evaluation pass directly and deterministically.
- `run()` — blocking loop: `tick()`, then wait `interval_seconds` (via `threading.Event.wait`, which is also how `stop()` interrupts the wait early), repeat until stopped.
- `start()` — raises `RuntimeError` if already running; otherwise clears the stop event and starts `run()` on a daemon background thread.
- `stop(join_timeout=None)` — signals the stop event and joins the background thread (default timeout: `interval_seconds + 1`).

---

### `dashboard/` — live web dashboard

**`dashboard/__init__.py`**
Empty. Marks `dashboard` as a package so it can be run as `python -m dashboard.app` (required — running `python dashboard/app.py` directly breaks the internal `adaptive_layer`/`dashboard` package imports).

**`dashboard/app.py`**
The Flask backend. Wires up the real middleware stack — `AccessPatternMonitor` (wrapped in `RecordingMonitor`), `AdaptiveClient`, `FeedbackLoop` (10s window / 3s interval / 5s cooldown) — against the real replica set (`dashboard_demo.live` collection), plus a `TrafficGenerator` the frontend can control, and exposes it all over a small JSON API polled once per second by the browser.

- Opens one **direct** (non-replica-set-aware) `MongoClient` per node (`mongo1`/`mongo2`/`mongo3`, `directConnection=true`) specifically so each node can be asked about *itself* — a replica-set-aware connection would transparently route everything to the primary, hiding exactly the per-node differences the dashboard exists to show.
- Opcounter baselining: `serverStatus().opcounters` is cumulative since the `mongod` process started, and MongoDB has no native "reset counters" command. The dashboard snapshots a per-node baseline (`_capture_baseline()`, taken at startup and again on `/api/reset`) and always displays `raw - baseline` (clamped at 0 to tolerate a node restarting mid-demo, which resets its real counters below the stored baseline).
- `_raw_opcounters(name)` / `_poll_node(name)` — query one node's `hello` (for PRIMARY/SECONDARY role) and `serverStatus` (for insert/query/getmore counts), returning a reachability-aware summary (`{"reachable": False, "role": "UNREACHABLE", ...}` on any `PyMongoError`, e.g. mid-election).
- Routes:
  - `GET /` — serves `static/index.html`.
  - `GET /api/config` — static config for the frontend: the three decision thresholds, the demo collection name, and the window/interval/cooldown values (used to render threshold-bar labels).
  - `GET /api/status` — the main polled endpoint: current monitor stats, current active settings, `TrafficGenerator` snapshot, all three nodes' live status, and the recent-ops feed.
  - `POST /api/traffic/start` / `/api/traffic/stop` / `/api/traffic/configure` — control the `TrafficGenerator`'s write ratio, rate, and running state.
  - `POST /api/reset` — re-baselines the per-node opcounter display and zeroes the traffic generator's op counter. Explicitly does **not** touch the Monitor, Decision Engine, or active settings, since those reflect real observed system state rather than a display convenience.
- `main()` — starts the `FeedbackLoop`, captures the initial opcounter baseline, runs the Flask app (`host=0.0.0.0`, `port=8080`, `threaded=True` — required since the dashboard has multiple concurrent background activities: the feedback loop thread, the traffic generator thread, and per-request polling), and cleans everything up (stop traffic, stop feedback loop, close all Mongo clients) in a `finally` block. Run with `python -m dashboard.app`.

**`dashboard/recording_monitor.py`**
Defines `RecordingMonitor` — a thin decorator around a real `AccessPatternMonitor` that additionally keeps a small thread-safe ring buffer (`collections.deque(maxlen=30)`) of the most recent operations, for the dashboard's live "Operation Feed" panel. Duck-types the exact interface `AdaptiveClient` expects from a monitor (`record`/`get_stats`/`known_collections`) and forwards everything through to the real monitor unmodified — meaning `adaptive_layer` code itself is completely untouched by this dashboard-only addition. `recent_ops()` returns the current feed contents (newest-first, via `appendleft`).

**`dashboard/traffic_generator.py`**
Defines `TrafficGenerator` — a simple, controllable, single-threaded read/write traffic source used by the dashboard's UI sliders. Deliberately much simpler than the Locust-based benchmark harness: it isn't trying to simulate concurrent users or measure latency percentiles, it's a live knob a presenter turns to change the read:write ratio and rate on the fly and watch the adaptive layer react.

- State: `write_ratio` (0.0–1.0, clamped), `ops_per_sec` (clamped to a minimum of 0.1), a running flag (`threading.Event`), and a cumulative `op_count`.
- `configure(write_ratio=None, ops_per_sec=None)` — updates either/both, thread-safely.
- `snapshot()` — returns the current running/ratio/rate/count state, for the `/api/status` JSON response.
- `reset_count()` — zeroes the displayed op counter only (purely cosmetic — doesn't touch the traffic itself or any real adaptive-layer state).
- `start()`/`stop()` — start/stop the background traffic thread (`start()` is a no-op if already running).
- `_run()` — the traffic loop: repeatedly decides read vs. write by comparing `random.random()` against `write_ratio`, issues the corresponding `client.write(...)`/`client.read(...)` call, increments the op count, and sleeps `1.0 / ops_per_sec` between ops.

**`dashboard/static/index.html`**
The dashboard's page markup. Sections, top to bottom: a header with a live connection-status indicator; a **Traffic Controls** card (read:write ratio slider, ops/sec rate slider, Start/Stop/Reset buttons, three one-click presets — Read-Heavy 5%/10ops, Write-Heavy 95%/15ops, Balanced 50%/8ops — and a traffic status line); a two-column grid with the **Active Consistency Settings** card (three setting boxes plus a plain-English mode label) and the **Access Pattern Monitor** card (read/write counts, write-frequency and read:write-ratio stats each with a threshold progress bar); a **Replica Set — Live Container Status** card (populated by JS, one box per node); and a **Live Operation Feed** card (scrolling recent-ops list). Loads `style.css` and `app.js`.

**`dashboard/static/app.js`**
The dashboard's frontend logic — vanilla JS, no framework/build step.
- `fetchJSON(url, options)` — thin `fetch` wrapper that throws on a non-OK response.
- `setConnIndicator(state)` — updates the header's connecting/live/connection-lost badge.
- `renderSettings(settings)` — updates the three setting boxes, flashes any box whose value just changed (`flashIfChanged`, a 1.2s CSS-class-toggle animation so a viewer's eye is drawn to changes automatically), and derives a plain-English mode label (`"SAFE — hot/write-heavy pattern detected"`, `"FAST — cold/read-heavy pattern detected"`, or the moderate default) purely from the returned settings' shape.
- `renderStats(stats)` — updates the read/write counts and ratio, and sets the two threshold bars' widths as a percentage of their configured threshold (treating an infinite ratio as 1.5× the threshold so the bar still renders meaningfully full rather than breaking).
- `renderTraffic(traffic)` / `renderNodes(nodes)` / `renderFeed(ops)` — render the traffic status line, the per-node status boxes (name, PRIMARY/SECONDARY/unreachable role badge, insert/query/getmore counters), and the recent-ops feed list, respectively.
- `poll()` — the main polling function: fetches `/api/status`, updates the connection indicator, and calls all the `render*` functions above. Called immediately on load and then every `POLL_INTERVAL_MS` (1000ms) via `setInterval`.
- `wireControls()` — attaches event listeners to both sliders (live-`POST`s to `/api/traffic/configure` on every drag, and updates the on-screen labels), the Start/Stop/Reset buttons (`POST` to the corresponding endpoint; Reset shows a temporary "Reset ✓"/"Reset failed" label), and each preset button (sets both sliders and immediately starts traffic with those values).
- `init()` — loads `/api/config` (used to label the threshold bars with their actual numeric thresholds), wires controls, and kicks off polling.

**`dashboard/static/style.css`**
Dark-themed styling for the dashboard: CSS custom properties for the palette (background, card background, accent blue, green/amber/red/purple status colors); card/grid layout (two-column grid collapsing to one column under 900px); traffic control styling including colored Start/Stop/Reset buttons; the settings boxes' flash-on-change transition; the two threshold progress bars (gradient fills); the per-node status boxes with color-coded PRIMARY/SECONDARY/unreachable role badges; and the scrollable, monospace live operation feed with amber-for-write/blue-for-read coloring.

---

### `benchmark/` — evaluation harness

**`benchmark/__init__.py`**
Empty. Marks `benchmark` as a package (needed for `python -m benchmark.staleness_test`-style invocation, which is required — invoking the staleness test as a bare script instead breaks its internal package imports).

**`benchmark/locustfile.py`**
The Phase 8 Locust-based workload generator — the shared engine both `run_benchmark.py` and manual/standalone Locust runs drive. Chosen deliberately over a hand-rolled `asyncio` generator so that rate control, ramping, and per-request-type percentile stats come for free instead of being reimplemented.

- Configuration is entirely via environment variables (so `run_benchmark.py` can drive it across configurations without editing code): `BENCHMARK_MODE` (`static_fast` | `static_safe` | `adaptive`, default `adaptive`), `WORKLOAD_PRESET` (`READ_HEAVY` | `WRITE_HEAVY` | `MIXED_BURSTY`, default `MIXED_BURSTY`), `BENCHMARK_COLLECTION` (default `bench`), `BENCHMARK_KEY_SPACE` (default `1000` distinct synthetic keys), `BENCHMARK_OPS_PER_SEC` (per-user target via Locust's `constant_throughput`, default `5`).
- Production-scale `FeedbackLoop` tuning for adaptive mode (distinct from the short values used in unit/integration tests): `WINDOW_SECONDS=15`, `INTERVAL_SECONDS=5`, `COOLDOWN_SECONDS=10`.
- `current_write_probability(preset, now=None)` — returns the fraction of ops that should be writes for the given preset: `0.05` for `READ_HEAVY`, `0.95` for `WRITE_HEAVY`, and for `MIXED_BURSTY`, a time-modulo oscillation between `0.95` and `0.05` on a 15-second period (`_MIXED_BURSTY_PERIOD_SECONDS`). Explicitly *not* implemented as a Locust `LoadTestShape`, because `LoadTestShape` controls simulated **user count** over time, not the read/write **mix** each user generates — the wrong tool for demonstrating a pattern shift within a steady user count.
- `on_test_start` (Locust `test_start` event listener) — builds one **shared** `AccessPatternMonitor`/`AdaptiveClient` for the entire test run (not one per simulated user — `pymongo.MongoClient` is meant to be shared/pooled, and "adaptive" mode specifically needs every simulated user observing and reacting to the *same* aggregate traffic pattern). For `static_fast`/`static_safe`, calls `set_active_settings` once with the fixed settings and never starts a feedback loop; for `adaptive`, starts a real `FeedbackLoop`. Shared state is attached to Locust's `environment` object, per Locust's own idiomatic pattern.
- `on_test_stop` — stops the feedback loop (if one was started) and closes the shared client.
- `AdaptiveMongoUser(User)` — one simulated concurrent caller, reusing the shared client/monitor. `wait_time = constant_throughput(OPS_PER_SEC_PER_USER)`. Its single `@task`, `read_or_write`, picks a random key from the configured key space, decides write vs. read via `current_write_probability`, times the call, catches any exception (Locust wants failures *reported*, not raised), and manually fires `environment.events.request.fire(request_type="mongo", name=op_name, response_time=..., ...)` so Locust's stats engine tracks MongoDB calls exactly like it would HTTP requests.
- Standalone sanity-run status noted in the file/exec-plan: a `READ_HEAVY` run measured 29/705 writes (4.11%) against a 5% target, and a `MIXED_BURSTY` run measured ~48 req/s against a ~50 req/s target with 0 failures — confirming the generator hits its configured rate/mix within tolerance.

**`benchmark/run_benchmark.py`**
The Phase 9 benchmark orchestrator. For each of the 3 configurations (`static_fast`, `static_safe`, `adaptive`) × 3 workload presets (`READ_HEAVY`, `WRITE_HEAVY`, `MIXED_BURSTY`) — 9 combinations total — it shells out to `locust` headlessly with the right environment variables, letting Locust itself write `results/<config>_<preset>_stats.csv` (and related files).

- Each combination is run as a **separate subprocess**, not imported in-process — because Locust monkey-patches `ssl` via `gevent` on import, which can conflict with `pymongo`'s own `ssl` usage if both share a process. Shelling out avoids that entirely and gives clean process isolation between runs.
- `QUICK = {"users": 5, "spawn_rate": 5, "duration": "20s"}` (fast iteration while developing) vs. `FULL = {"users": 20, "spawn_rate": 10, "duration": "60s"}` (real numbers) — selected via a `--quick` CLI flag. Both durations are chosen to comfortably exceed `MIXED_BURSTY`'s 15-second oscillation period so the pattern shift actually shows up.
- `run_one(config, preset, params)` — builds a unique per-combination collection name and CSV prefix, runs the `locust` subprocess with `--headless`/`--csv`/`--only-summary`, and on success hands the CSV prefix to `_summarize_stats_csv`; on a non-zero exit code, prints the tail of stdout/stderr and returns `None`.
- `_summarize_stats_csv(csv_prefix)` — reads Locust's own `_stats.csv`, finds the `"Aggregated"` row, and extracts request count, failure count, requests/sec, and p50/p95/p99 latency — printing a one-line summary and flagging any failures.
- `main()` — runs all 9 combinations, prints a final aligned summary table, and exits with status `1` if any combination failed or produced request failures (useful for CI/scripted use).

**`benchmark/analyze_results.py`**
Currently an **empty placeholder file**. Per `plan/exec_plan.md` Phase 11 ("Full Benchmark Suite + Results Analysis"), this is where `pandas` will be used to load every `results/*.csv`, aggregate into `results/summary.csv`, and `matplotlib` will be used to produce the latency-percentile comparison chart, throughput-vs-workload chart, staleness-rate bar chart, and monitoring/decision-engine overhead chart for the final report. Not yet implemented as of this document.

**`benchmark/staleness_test.py`**
The Phase 10 staleness measurement — the metric that demonstrates the actual consistency *trade-off*, since latency/throughput alone say nothing about correctness risk. Method: write a document containing a unique marker, then immediately read it back using whatever read preference the active configuration specifies; a "miss" means the read landed on a replica that hadn't caught up yet.

- Four scenarios measured, each producing a `hits`/`misses`/`stale_rate`:
  - `static_fast` — `FAST_SETTINGS` fixed the whole time, no adaptation, no warmup needed.
  - `static_safe` — `SAFE_SETTINGS` fixed the whole time.
  - `adaptive_hot` — a real `FeedbackLoop` is warmed up with `warmup_writes=60` writes until it reaches `SAFE_SETTINGS` (polled via `_wait_until_settings`, 15s timeout), then measured — the trials themselves keep it hot.
  - `adaptive_cold` — warmed up with a seed write + `warmup_reads=30` reads until it reaches `FAST_SETTINGS`, then measured with the write+read trial deliberately spaced `COLD_TRIAL_SPACING_SECONDS=2` apart and padded with `COLD_FILLER_READS_PER_TRIAL=6` extra reads per trial — necessary because a bare write+read loop is a 1:1 read:write ratio, which alone would never satisfy the Decision Engine's read-heavy branch and would let the collection drift back toward `MODERATE_SETTINGS` mid-measurement.
- `FULL_TRIALS=200`, `QUICK_TRIALS=40` (via `--quick`); `adaptive_cold`'s trial count is separately capped at `min(trials, 60)` regardless of mode, since its per-trial pacing would otherwise make it dominate total runtime for no added statistical value.
- `_one_trial(client, collection, seq)` — writes a document with a unique marker, immediately reads it back by that marker, and returns whether it was found (fresh) — the core staleness probe.
- `measure_static` / `measure_adaptive_hot` / `measure_adaptive_cold` — each sets up the appropriate client/monitor/loop, warms up if needed, runs the trials, and always tears down (drops its throwaway collection, stops any loop, closes the client) in a `finally` block.
- `_report(config_name, trials, hits)` — computes and prints the stale rate, returns a result dict.
- `main()` — runs all four scenarios in order, prints a summary table, writes `results/staleness.csv`, and prints a detailed interpretation note explaining *why* a direct numeric comparison between `adaptive_cold` and `static_fast` isn't apples-to-apples (their write pressure differs by design — `adaptive_cold`'s spacing is exactly *why* the Decision Engine grants it `FAST_SETTINGS` in the first place, and that same spacing gives secondaries time to catch up between writes) — while `adaptive_hot` vs. `static_safe` *is* directly comparable, since both are measured under the same sustained back-to-back write pressure.

---

### `tests/`

**`tests/__init__.py`**
Empty. Marks `tests` as a package.

**`tests/test_monitor.py`**
Pure unit tests for `AccessPatternMonitor` — no database, no real `sleep()`, all timestamps are synthetic floats (the entire point of keeping the Monitor decoupled from MongoDB). Covers: an unknown collection returns `NEUTRAL_STATS`; basic read/write counting and ratio computation; a read-only collection has an infinite ratio; a collection with reads but no writes has `seconds_since_last_write is None`; events older than the window get pruned out of `get_stats()`; a collection can be pruned all the way back to `NEUTRAL_STATS`; `record()` itself prunes immediately (not only on `get_stats()` calls), verified by inspecting the internal deque length directly; an invalid `op_type` raises `ValueError`; a zero-or-negative `window_seconds` is rejected at construction.

**`tests/test_decision.py`**
Pure unit tests for the Decision Engine — `Stats` objects constructed directly via a local `make_stats()` helper, no DB, no monitor. Table-driven, covering: one test per branch (hot/write-heavy → `SAFE_SETTINGS`; read-heavy+cold → `FAST_SETTINGS`; mixed/moderate → `MODERATE_SETTINGS`; brand-new/`NEUTRAL_STATS` collection → `MODERATE_SETTINGS`); a read-heavy-but-also-hot case proving the hot branch wins when both signals fire simultaneously; boundary cases proving a value sitting **exactly** on `HIGH_WRITE_THRESHOLD`, `READ_HEAVY_RATIO_THRESHOLD`, or `LOW_WRITE_THRESHOLD` does *not* yet trigger the corresponding branch (strict inequality); an infinite read:write ratio doesn't crash `decide()`; a `None` `seconds_since_last_write` doesn't crash `decide()` (that field isn't even read today, but must stay safe if that changes); and a parametrized sanity check that the three module-level constants really are the only three possible `decide()` outputs.

**`tests/test_feedback.py`**
Pure unit tests for `FeedbackLoop.tick()` — a `FakeClock` (starts at a fixed time, advances only when told to), a `FakeMonitor` (stats set directly per collection rather than derived from real events), and a `FakeClient` (records every `set_active_settings` call in order) stand in for all real dependencies — no database, no real threading, no real sleeping. This tests the *orchestration logic* (when a switch happens vs. gets suppressed), not threading or real MongoDB behavior. Covers: a collection's first-ever decision applies immediately with no cooldown check; a would-be switch within the cooldown window is suppressed; the same switch is applied once the cooldown has elapsed; a "no change needed" tick doesn't call `set_active_settings` at all and — critically — doesn't touch the cooldown timer either; multiple collections are evaluated independently in one `tick()`; a `tick()` with no known collections does nothing; and moderate stats never trigger a switch away from the client's own default.

**`tests/test_feedback_integration.py`**
An end-to-end integration test for `FeedbackLoop` against the **real** replica set, the **real** `AdaptiveClient`, the **real** `AccessPatternMonitor`, and a **real** background thread via `start()`/`stop()` — i.e., the exact stack `demo_app.py` runs, as opposed to `test_feedback.py`'s fake-everything unit tests. Requires the Phase 1 replica set to be running. Uses short window/interval/cooldown values (`WINDOW_SECONDS=2`, `INTERVAL_SECONDS=1`, `COOLDOWN_SECONDS=0`) so the test finishes in seconds. `_wait_until_settings` polls (rather than a single fixed sleep) to be robust against real thread-scheduling/tick-timing variance. The one test, `test_feedback_loop_escalates_on_write_burst_then_relaxes_on_reads`, drives 20 rapid writes and asserts the settings escalate to `SAFE_SETTINGS`, then waits for the window to fully age out, drives 10 reads, and asserts the settings relax to `FAST_SETTINGS` — proving the whole loop closes for real, not just in isolation.

**`tests/test_client_smoke.py`**
Integration tests for `AdaptiveClient` against the real replica set (Phase 2/5 territory) — as opposed to the Monitor/Decision Engine's pure unit tests, these intentionally hit real MongoDB to prove the proxy layer works end-to-end. A `client` pytest fixture creates an `AdaptiveClient` and cleans up known test collections (`smoke`, `smoke_monitor`, `smoke_a`, `smoke_b`) afterward. `_read_with_retry` is a helper acknowledging that `FAST_SETTINGS` (`read_preference="nearest"`, `read_concern="local"`) can legitimately route a read to a secondary that hasn't replicated a just-written document yet — the exact staleness trade-off this whole project studies, not a bug — so tests using `FAST_SETTINGS` retry briefly instead of asserting instant visibility. Tests cover: a `SAFE_SETTINGS` write followed by an immediate read round-trips correctly; `_log_op` really feeds a live `AccessPatternMonitor` (not just prints) with correct read/write counts and ratio; two collections given opposite settings (`SAFE_SETTINGS` vs. `FAST_SETTINGS`) through the *same* `AdaptiveClient` instance behave completely independently — one collection's settings never leak into or get clobbered by the other's; and a never-configured collection correctly defaults to `MODERATE_SETTINGS`. Also runnable directly as `python tests/test_client_smoke.py` for a quick manual check.

---

### `results/`

**`results/.gitkeep`**
An empty placeholder file whose only purpose is to make git track the (otherwise-empty) `results/` directory, since its actual contents (`*.csv`, `*.png` benchmark output) are gitignored and regenerated by every benchmark/staleness run.

---

### Not covered above (tooling/build artifacts, not part of the project's source)

- **`.pytest_cache/`** — auto-generated by `pytest` between runs (cached test-run metadata: last-failed tests, node IDs). Not source; safe to delete/regenerate at any time.
- **`.claude/scheduled_tasks.lock`** — local Claude Code tooling state, explicitly gitignored (`.claude/` in `.gitignore`) and unrelated to the project's own logic.

---

## Appendix: The three named consistency configurations, at a glance

| Name | Write concern | Read concern | Read preference | Chosen when | Trade-off |
|---|---|---|---|---|---|
| **SAFE_SETTINGS** | `"majority"` | `"majority"` | `primary` | `write_freq > HIGH_WRITE_THRESHOLD` (hot / write-heavy) | Slowest, but correctness-guaranteed — every write is durable across a majority before it's acknowledged, every read goes to the primary. |
| **MODERATE_SETTINGS** | `1` | `"local"` | `primaryPreferred` | Everything else, including brand-new/never-seen collections | The safe, balanced default — fast-ish writes, reads prefer the primary but can fail over to a secondary. |
| **FAST_SETTINGS** | `1` | `"local"` | `nearest` | `read_write_ratio > READ_HEAVY_RATIO_THRESHOLD and write_freq < LOW_WRITE_THRESHOLD` (cold / read-heavy) | Fastest — reads are routed to whichever node answers quickest, with minimal write durability guarantees. Staleness risk is acceptable here specifically *because* writes are rare in this regime, so there's little to be stale about. |
