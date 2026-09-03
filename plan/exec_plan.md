# Execution Plan — Adaptive Consistency Layer for MongoDB

This breaks the whole project (from empty repo to DA-III submission) into small, sequential phases. Each phase is scoped to be doable in one sitting (roughly an hour to half a day), produces a concrete artifact, and has a checkable "Definition of Done" so you know when to move on. Phases build on each other in order — don't skip ahead to benchmarking before the middleware actually closes the loop.

Check off phases as you go (`- [x]`) so the team can see progress at a glance.

## Milestone map

| Phases | Maps to | What it proves |
|---|---|---|
| 0–1 | Infra prerequisite | You have something to point code at |
| 2–6 | **DA-II core** | The middleware itself exists and closes the loop |
| 7–10 | **DA-II submission** | ~60% implementation + a first benchmark run with real numbers |
| 11–12 | **DA-III core** | Full benchmark suite, tuned, results in hand |
| 13 | **DA-III submission** | Final report, demo, slides |

---

## Phase 0 — Repo & Environment Bootstrap

**Goal:** A clean, reproducible dev environment the whole team can `git clone` and run.

**Why:** Nothing else in this plan works without this, and getting it wrong (wrong Python version, missing deps, no git history) wastes time repeatedly later.

**Steps:**
1. `git init` in `MongoLayer/` if not already a repo; create `.gitignore` (Python: `__pycache__/`, `*.pyc`, `.venv/`, `results/*.csv`, `results/*.png`, `.env`).
2. Create a virtual environment: `python -m venv .venv`, activate it.
3. Create `requirements.txt`: `pymongo`, `pytest`, `pandas`, `matplotlib`. Add `locust` only if you decide to use it over the custom asyncio generator (Phase 8 default assumes custom).
4. `pip install -r requirements.txt`.
5. Create the skeleton folders/files exactly as in `PROJECT.md` §7: `adaptive_layer/__init__.py`, `benchmark/__init__.py`, empty placeholder modules (`client.py`, `monitor.py`, `decision.py`, `feedback.py`, `workloads.py`, `run_benchmark.py`, `analyze_results.py`), `demo_app.py`, `results/` (empty, gitignored contents).
6. First commit: "Project skeleton."

**Definition of Done:** `python -c "import pymongo, pandas, matplotlib, pytest"` runs with no error; `git log` shows an initial commit; a teammate can clone the repo and get the same environment from `requirements.txt`.

---

## Phase 1 — MongoDB Replica Set via Docker Compose

**Goal:** A running 3-node local MongoDB replica set, verified to actually replicate.

**Why:** Every downstream phase needs this. Write/read concern and read preference produce zero observable difference against a standalone `mongod` — this is the #1 way this project silently produces meaningless results if skipped or half-done.

**Steps:**
1. Write `docker-compose.yml` with three `mongo` services (`mongo1`, `mongo2`, `mongo3`), each with `--replSet rs0`, distinct host ports (e.g. 27017/27018/27019), and a shared Docker network.
2. `docker compose up -d`.
3. Connect to `mongo1` via `mongosh` and run `rs.initiate({...})` naming all three members.
4. Verify with `rs.status()` — expect one `PRIMARY`, two `SECONDARY`.
5. Manual smoke test from `mongosh`: insert a document with `writeConcern: {w: "majority"}`; read it back from a secondary with `readPreference: "secondary"`.
6. Write a short `docs/replica_set_setup.md` note (2–3 sentences + the exact commands) so teammates don't have to reverse-engineer it.

**Definition of Done:** `rs.status()` shows a healthy 3-member set; a majority write and a secondary read both succeed without error; the setup steps are written down somewhere a teammate can follow without you.

---

## Phase 2 — AdaptiveClient Skeleton (Proxy/Interceptor)

**Goal:** A minimal `pymongo` wrapper that can execute one read and one write with **explicitly passed** settings, and logs the operation.

**Why:** This is the layer everything else plugs into. Keep it dumb for now — no dynamic settings lookup yet, that's Phase 5.

**Steps:**
1. In `adaptive_layer/client.py`, define `class AdaptiveClient`: constructor takes a MongoDB connection URI and DB name.
2. `write(collection, doc, write_concern)` — inserts `doc` into `collection` using the given write concern; records the call.
3. `read(collection, query, read_concern, read_preference)` — finds a doc; records the call.
4. `_log_op(collection, op_type, timestamp, latency_ms)` — for now, just `print()` it (Phase 3 replaces this with a real call into the Monitor).
5. Write a throwaway script (`scripts/smoke_test_client.py` or a pytest test) that connects to the Phase 1 replica set, writes a doc with `w="majority"`, reads it back with `read_preference=SecondaryPreferred`, and asserts the round trip works.

**Definition of Done:** The smoke test passes against the real replica set; `_log_op` visibly fires for both the write and the read.

---

## Phase 3 — Access Pattern Monitor

**Goal:** A standalone, DB-independent component that tracks per-collection access patterns from a stream of logged events.

**Why:** Decoupling this from `AdaptiveClient` means you can unit-test it with fake timestamps instead of hitting the database — much faster iteration.

**Steps:**
1. In `adaptive_layer/monitor.py`, define `class AccessPatternMonitor(window_seconds=30)`.
2. `record(collection, op_type, timestamp)` — appends to a `collections.deque` keyed by collection name.
3. `get_stats(collection)` — prunes entries older than `window_seconds`, then returns a small dataclass/dict: `read_count`, `write_count`, `read_write_ratio`, `write_freq` (writes/sec), `seconds_since_last_write`.
4. Handle the empty case cleanly (new collection, no events yet) — return a sentinel/neutral stats object, not a crash.
5. Write `tests/test_monitor.py`: feed synthetic timestamped events (no DB needed) and assert the computed stats are correct, including the pruning behavior (events older than the window shouldn't count).
6. Wire `AdaptiveClient._log_op` (Phase 2) to actually call `monitor.record(...)` instead of just printing.

**Definition of Done:** `pytest tests/test_monitor.py` passes, including at least one test that proves old events get pruned; running the Phase 2 smoke test now also populates real monitor stats you can print and inspect.

---

## Phase 4 — Decision Engine

**Goal:** A pure function: `stats -> (write_concern, read_concern, read_preference)`.

**Why:** Keeping this a pure function (no DB access, no side effects) makes it trivial to unit-test exhaustively — this is the piece a grader will scrutinize most, so it should be the best-tested code in the project.

**Steps:**
1. In `adaptive_layer/decision.py`, define named threshold constants at module level: `HIGH_WRITE_THRESHOLD`, `READ_HEAVY_RATIO_THRESHOLD`, `LOW_WRITE_THRESHOLD` (start with placeholder values — you'll tune these for real in Phase 12).
2. `decide(stats) -> Settings` implementing the three branches from `PROJECT.md` §3: hot/write-heavy → majority/majority/primary; read-heavy+cold → w=1/local/nearest; else → moderate default.
3. Write `tests/test_decision.py`: table-driven tests, one case per branch, plus boundary cases (stats exactly at a threshold, brand-new collection with neutral/empty stats).

**Definition of Done:** `pytest tests/test_decision.py` passes for all branches and boundary cases; you can hand someone `decide()` and a `stats` dict with no database running and get a sensible answer.

---

## Phase 5 — Per-Collection Active Settings in AdaptiveClient

**Goal:** `AdaptiveClient` stops taking settings as call arguments and instead looks up the *currently active* settings per collection internally.

**Why:** This is the seam the Feedback Loop (Phase 6) plugs into — it needs somewhere to push new settings that the client will actually use on the next operation.

**Steps:**
1. Add `self._active_settings: dict[str, Settings]` to `AdaptiveClient`, defaulting every collection to a moderate config until told otherwise.
2. Add `set_active_settings(collection, settings)`.
3. Change `read()`/`write()` to no longer require explicit concern/preference args — they look up `self._active_settings[collection]` instead.
4. Manual test: call `set_active_settings` with two different configs for two different collections, perform a write/read on each, and confirm (via logging or by asserting on the pymongo call arguments) that each collection actually used its own settings.

**Definition of Done:** Two collections with different active settings behave independently through the same `AdaptiveClient` instance; no caller has to pass concern/preference by hand anymore.

---

## Phase 6 — Feedback Loop (close the loop)

**Goal:** Wire Monitor → Decision Engine → `AdaptiveClient.set_active_settings` together automatically, on a timer, with cooldown/hysteresis.

**Why:** This is the actual novel contribution of the project. Everything before this phase is plumbing; this is where "adaptive" becomes real.

**Steps:**
1. In `adaptive_layer/feedback.py`, define `class FeedbackLoop(client, monitor, interval_seconds, cooldown_seconds)`.
2. `run()` (as a background thread or `asyncio` task): every `interval_seconds`, for each collection the monitor has seen: pull `stats`, run `decide(stats)`, and only call `client.set_active_settings(...)` if at least `cooldown_seconds` have passed since that collection's last settings change (track per-collection last-switch timestamps).
3. `start()` / `stop()` for clean lifecycle control (important for the demo app and benchmark harness to be able to shut it down).
4. Write `tests/test_feedback.py` using a fake/mock client and monitor (no real DB or real sleep — inject a fake clock or use very short intervals) to verify: decisions get applied when cooldown has elapsed, and get suppressed when it hasn't.
5. Manual end-to-end test against the real replica set: script a burst of writes to one collection, wait past `interval_seconds`, confirm settings escalated toward majority/majority; then switch to reads-only, wait, confirm it relaxes back down.

**Definition of Done:** `pytest tests/test_feedback.py` passes; the manual end-to-end test shows settings visibly changing in response to a real workload shift, with no rapid flapping between ticks.

**⟶ At this point the core middleware is functionally complete.**

---

## Phase 7 — Demo App (visible proof it works)

**Goal:** A runnable `demo_app.py` a grader can watch and *see* the system adapt in real time.

**Why:** DA-II wants "experiments carried till date with protocols, procedures and results obtained" — a live, narratable demo is the cheapest way to produce convincing evidence before the full benchmark harness exists.

**Steps:**
1. `demo_app.py`: run three scripted phases against the Phase 1 replica set through the full stack (client + monitor + decision engine + feedback loop, actually running): (A) read-heavy phase, (B) write-burst phase, (C) mixed phase.
2. After each phase, print the monitor's current stats and the active settings for the demo collection, so the change is visible in the console.
3. Capture a log/screen recording of one full run — this becomes your evidence artifact for DA-II.

**Definition of Done:** `python demo_app.py` runs start-to-finish against the Docker replica set and prints visibly different active settings per phase; you have a saved log or recording of a successful run.

---

## Phase 8 — Benchmark Workload Generator

**Goal:** A configurable, reusable load generator: given a read/write ratio, ops/sec, and duration, it drives that exact workload.

**Why:** You need the *same* workload generator to drive all three configurations (adaptive, static-fast, static-safe) for the comparison to be fair.

**Steps:**
1. In `benchmark/workloads.py`, implement an `asyncio`-based generator that yields timed read/write events against a configurable key space, at a configurable rate.
2. Define named presets matching `PROJECT.md` §5: `READ_HEAVY` (e.g. 95/5), `WRITE_HEAVY` (5/95), `MIXED_BURSTY` (oscillates between read-heavy and write-heavy within one run — this is the one that should make the adaptive layer visibly outperform either static baseline).
3. Standalone sanity script: run the generator alone for 30s at a known ratio, count actual reads/writes produced, and assert they're within tolerance of the target ratio and rate.

**Definition of Done:** The sanity script confirms the generator hits its configured rate and read/write mix within a reasonable tolerance (e.g. ±10%).

---

## Phase 9 — Benchmark Runner + Latency/Throughput Metrics

**Goal:** Orchestrate all three configurations × the workload presets, capturing per-op latency and throughput, and write raw results to CSV.

**Why:** This is the actual "benchmark run" DA-II wants evidence of.

**Steps:**
1. In `benchmark/run_benchmark.py`, for each config in `[static_fast (w=1/local), static_safe (w=majority/majority), adaptive]`, for each workload preset: freshly configure the client for that run, drive the Phase 8 generator against it, record per-operation latency with a timestamp, write raw rows to `results/<config>_<workload>.csv`.
3. Add a `--quick` flag (short duration, e.g. 15s) for fast iteration while developing, versus a full-length run for real numbers later (Phase 11).
4. Compute and print p50/p95/p99 latency and ops/sec at the end of each run as a sanity check.

**Definition of Done:** `python benchmark/run_benchmark.py --quick` produces correctly-formatted CSVs in `results/` for every (config × workload) combination, with sane-looking percentile numbers printed to console.

---

## Phase 10 — Staleness Measurement

**Goal:** Directly measure read-after-write staleness per configuration, not just latency/throughput.

**Why:** Staleness is the metric that actually demonstrates the consistency *tradeoff* — latency/throughput alone don't prove anything about correctness risk.

**Steps:**
1. `benchmark/staleness_test.py`: write a monotonically increasing counter value to a document, then immediately issue a `secondaryPreferred` read for it; compare the read value against what was just written.
2. Repeat N times (e.g. 200+) per configuration; record hit (fresh) vs. miss (stale) and, where measurable, how stale (value lag or wall-clock delay until the correct value appears).
3. Aggregate into a stale-read-rate percentage per configuration.

**Definition of Done:** You have a staleness-rate number for each of the three configurations, and the adaptive layer's rate sits between the two static baselines in the direction you'd expect (closer to static-safe under its "hot" rule, closer to static-fast under its "cold" rule) — if it doesn't, that's a Phase 12 tuning problem, not a Phase 10 bug, but confirm which before moving on.

**⟶ DA-II submission checkpoint: Phases 0–10 give you ~60% implementation with a working proxy, monitor, decision engine, feedback loop, a live demo, and a first benchmark run with real latency/throughput/staleness numbers.**

---

## Phase 11 — Full Benchmark Suite + Results Analysis

**Goal:** Run the complete matrix at full duration and turn raw CSVs into the tables/figures that go in the final report.

**Steps:**
1. Run `run_benchmark.py` (full-length, no `--quick`) and `staleness_test.py` across every configuration × every workload preset. Run each combination more than once if time allows, for statistical stability.
2. In `benchmark/analyze_results.py`, use `pandas` to load all `results/*.csv`, aggregate into a summary table (`results/summary.csv`), and use `matplotlib` to produce: a latency-percentile comparison chart, a throughput-vs-workload chart, a staleness-rate bar chart, and a monitoring/decision-engine overhead comparison chart.
3. For each figure, write one sentence of what it shows — you'll need this for the report's Discussion section anyway.

**Definition of Done:** `results/summary.csv` and a set of labeled PNG figures exist, and each has a one-sentence takeaway drafted.

---

## Phase 12 — Tuning, Hardening, Edge Cases

**Goal:** Fix any flapping/instability the full run exposed, and handle the operational edge cases a grader might poke at live.

**Steps:**
1. Review feedback-loop behavior from Phase 11's full runs — look for rapid oscillation between settings under steady-state load; if present, widen the cooldown or adjust thresholds (Phase 4's constants) and re-run the affected benchmark.
2. Handle: a brand-new collection with no stats yet (should default sensibly, not crash); the monitor's window with all events aged out; a client operation when a secondary is temporarily unreachable (shouldn't crash the whole run).
3. Document the final threshold/cooldown values chosen and *why* (this becomes a paragraph in the report's Implementation section — "we tuned X because Y").

**Definition of Done:** Re-running Phase 11's benchmark shows no rapid oscillation under steady-state workloads; the edge cases above have been manually tried and don't crash the system; the tuning rationale is written down.

---

## Phase 13 — Final Report, Demo Rehearsal, Slides (DA-III)

**Goal:** Package everything into the DA-III deliverables.

**Steps:**
1. Extend `DA1_Literature_Review.tex` (or start fresh from it) into the full research-paper-format final report: Abstract, Introduction, Related Work (reuse the lit review), System Design (reuse `PROJECT.md` §3 + the system design figure), Implementation, Evaluation (Phase 11's figures/tables), Discussion (what the numbers mean, including the Phase 12 tuning story), Conclusion, References.
2. Rehearse the `demo_app.py` live-adaptation demo end-to-end, timed, at least twice.
3. Prepare presentation slides: problem → architecture diagram → live demo → key benchmark results → conclusion.

**Definition of Done:** Final report compiles to a clean PDF with all figures embedded and captioned; the live demo runs cleanly within your presentation's time limit; slides are ready.

---

## Open decisions to revisit along the way

- **Granularity:** per-collection (default plan above) vs. per-document (`_id`) monitoring — stay per-collection through DA-II; only attempt per-key as a stretch goal if Phase 11 leaves spare time before DA-III.
- **ML stretch goal:** the rule-based Decision Engine (Phase 4) is the examinable core. A logistic-regression variant is optional novelty points — only attempt it after Phase 13's core deliverables are safely done, as an additional Phase 14 if time allows.
- **Team split (if 2–3 people):** Phases 0–2 and 8–9 (infra/plumbing) parallelize well; Phases 3–6 (the core adaptive logic) benefit from one owner for consistency; Phase 11–13 (analysis/report) is natural work for whoever isn't heads-down on code late in the timeline.
