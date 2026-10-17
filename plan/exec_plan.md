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
3. Create `requirements.txt`: `pymongo`, `pytest`, `pandas`, `matplotlib`, `locust` (used in Phase 8 for the workload generator — chosen over a hand-rolled asyncio generator to get rate control, ramping, and percentile stats for free).
4. `pip install -r requirements.txt`.
5. Create the skeleton folders/files: `adaptive_layer/__init__.py`, empty placeholder modules `adaptive_layer/client.py`, `adaptive_layer/monitor.py`, `adaptive_layer/decision.py`, `adaptive_layer/feedback.py`; `benchmark/__init__.py`, empty placeholder modules `benchmark/locustfile.py`, `benchmark/run_benchmark.py`, `benchmark/analyze_results.py`; `demo_app.py`; `results/` (empty, gitignored contents).
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

## Phase 8 — Benchmark Workload Generator (Locust)

**Goal:** A configurable, reusable load generator: given a read/write ratio, ops/sec, and duration, it drives that exact workload against the `AdaptiveClient`.

**Why:** You need the *same* workload generator to drive all three configurations (adaptive, static-fast, static-safe) for the comparison to be fair. **Decision:** use `locust` instead of a hand-rolled `asyncio` generator — rate control, ramping, live percentile stats, and a web UI (handy for the DA-III demo) all come for free instead of being reimplemented and re-debugged from scratch.

**Steps:**
1. In `benchmark/locustfile.py`, define a `User` class wrapping `AdaptiveClient` as its "client" (Locust's HTTP assumption doesn't apply — MongoDB calls are reported manually via `environment.events.request.fire(request_type=..., name=..., response_time=..., response_length=0, exception=...)` so Locust's stats engine tracks them like any other request type).
2. Define `@task(weight)` methods for `read` and `write` against a configurable key space; control the read:write ratio via task weights and pacing via a `wait_time` class (e.g. `constant_throughput`).
3. Define named presets via a `WORKLOAD_PRESET` env var: `READ_HEAVY` (5% writes), `WRITE_HEAVY` (95% writes), `MIXED_BURSTY` (oscillates between the two on a fixed wall-clock period — implemented as a simple time-modulo probability function, not a Locust `LoadTestShape`: `LoadTestShape` controls simulated user *count* over time, not the read/write *mix*, so it's the wrong tool for demonstrating a pattern shift within a steady user count). This is the preset that should make the adaptive layer visibly outperform either static baseline.
4. Standalone sanity run: `locust -f benchmark/locustfile.py --headless -u 10 -r 5 -t 30s` against a known ratio; confirm Locust's own request-count breakdown by type is within tolerance of the target ratio and rate.

**Definition of Done:** A headless Locust run hits its configured rate and read/write mix within a reasonable tolerance (e.g. ±10%), confirmed from Locust's own printed stats. **Status: done (2026-09-10)** — `READ_HEAVY` sanity run: 29/705 writes (4.11%) vs. a 5% target; default `MIXED_BURSTY` run: 1417 ops, 0 failures, ~48 req/s vs. a ~50 req/s target. See `benchmark/locustfile.py` and the project vault's Phase 8 note for the shared-client architecture and env-var configuration this actually uses.

---

## Phase 9 — Benchmark Runner + Latency/Throughput Metrics (Locust)

**Goal:** Orchestrate all three configurations × the workload presets, capturing per-op latency and throughput, and write raw results to CSV.

**Why:** This is the actual "benchmark run" DA-II wants evidence of. Locust already computes p50/p95/p99-equivalent response-time percentiles per request type and can export them directly — no custom percentile math needed here.

**Steps:**
1. In `benchmark/run_benchmark.py`, for each config in `[static_fast (w=1/local), static_safe (w=majority/majority), adaptive]`, for each workload preset from Phase 8: reconfigure the client for that run, then invoke Locust headlessly (`locust -f benchmark/locustfile.py --headless --csv results/<config>_<workload> -u N -r N -t Xs`) so Locust itself writes the `_stats.csv` / `_stats_history.csv` files.
2. Add a `--quick` flag (short duration, e.g. 15s) for fast iteration while developing, versus a full-length run for real numbers later (Phase 11).
3. After each run, read Locust's own `_stats.csv` and print p50/p95/p99 latency and ops/sec as a sanity check (Locust's stats object exposes `get_response_time_percentile()` directly if you want this in-process instead of shelling out).

**Definition of Done:** `python benchmark/run_benchmark.py --quick` produces Locust's CSV output in `results/` for every (config × workload) combination, with sane-looking percentile numbers printed to console.

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

---

## Status and re-plan (2026-10-16)

This section supersedes the checklist above for what remains. Phases 0-11 are done; the remaining work is a revised Phase 12, Phase 13, and a new last phase for the web interface.

### Verified state

| Area | State | How it was checked |
|---|---|---|
| Middleware (`adaptive_layer/`) | Implemented | Reads and imports cleanly; `FeedbackLoop` now records every switch in `switch_log` |
| Unit tests (no database) | 31 pass | `pytest tests/test_monitor.py tests/test_decision.py tests/test_feedback.py` |
| Integration tests (5, need the replica set) | **Not re-verified** | Docker had no images, volumes or containers at the start of this session; the cluster must be rebuilt first |
| Last full evaluation | On disk, complete | 120 benchmark runs (5 latency levels x 8 traffic presets x 3 configs), 0 failed requests, 11,724,158 requests; 20 staleness measurements, 3,300 trials |
| Web interface | Removed from the working tree | No `dashboard`, Flask, or port-8080 references remain in code or live docs. The 7 files are still in commit `649e248` (`git checkout 649e248 -- dashboard` restores them). Flask stays installed only because Locust depends on it |
| Git | Nothing committed since `649e248` | 28 uncommitted entries |

**Core finding, reproduced with the project's own monitor and `decide()`:** at the load-test rates every one of the 8 traffic presets decides SAFE (write rates 10 to 499 writes/s against a SAFE threshold of 5 writes/s). FAST needs under 0.5 writes/s, which at 250 ops/s is under about 0.2% writes. So the adaptive configuration behaves like Static Safe, and this is a scale problem in the rule, not a matter of nudging the constants. This was reproduced on simulated traffic at the target rates; the real runs did not log the chosen settings.

### Phase 12 (revised): make the adaptive layer able to win, and prove it

Decisions taken (2026-10-16): the rule uses **write share**; the sweep is **3 latency levels (0, 5, 43 ms) x 3 repeats**; the two stale snapshot docs were deleted and will be remade.

| # | Step | State |
|---|---|---|
| 0 | Restore the environment | **Done 2026-10-17**: image pulled (slow link), `docker compose up -d --build`, replica set initiated; full suite **71 / 71 pass** including the 5 live tests; `netem` verified (round trip 10.3 ms at 5 ms and 86.3 ms at 43 ms, client path unaffected, cleared afterwards); `demo_app.py` matches all three phases |
| 1 | Switch log per adaptive run | **Done and verified** on real runs (`<config>_<preset>_switches.csv`, summarised as share of time per setting) |
| 2 | Write-share rule with `Thresholds` parameters (FAST < 0.15, SAFE > 0.40, fixed before the re-run, rationale in `decision.py`) | **Done and verified**: unit tests, live tests, demo, and what adaptive actually chose on all nine presets (see findings below) |
| 3 | Presets that reach every branch | **Done**: `MODEST_WRITES` (25% writes) added for MODERATE; READ_HEAVY / SPIKE / MIXED_BURSTY reach FAST. Low-rate presets were dropped: the unit test shows the rule is scale-independent |
| 4 | Staleness inside the load test | **Done and verified**: about 2% of iterations are probes (write, then immediate read of that document by `_id`), tallied by the settings in force into `<config>_<preset>_probe.csv`; stale probes are not Locust failures; tables and chart added to the notebook and analysis |
| 5 | `adaptive_cold` pacing | **Done and verified**: 9 filler reads per trial; each trial records the settings in force, and all 40 of 40 trials at 43 ms ran under FAST |
| 6 | Sweep with repeats | **Deferred to the end by decision (2026-10-16): until the project is finished only quick checks are run (see below); the 3x3 full-length runs for presented results come last.** Pipeline done and tested (`--repeats`, `--duration-scale`, mean and std, pooled staleness, new folder `results/write_share_eval/`); not yet run. Full length is about **19.7 hours** (9 presets x 3 configs x 3 levels x 3 repeats); with `--duration-scale 0.4` about 8 hours |
| 7 | Edge cases: stop one secondary mid-run | **Not done** (the cluster is available now) |
| 8 | Write down the tuning rationale | Thresholds and reasoning are in `decision.py`; the report text is still to write after the run |

**Findings from the verification runs (2026-10-17; quick runs, single run each, so qualitative):**

- **Steady traffic picks the expected setting.** Adaptive's time in each setting, after the first 5 s that the loop needs for its first decision (so MODERATE for the first 5 s of every run): READ_HEAVY went to FAST; WRITE_HEAVY, BALANCED, HOTSPOT, RAMP and HIGH_CONCURRENCY went to SAFE; MODEST_WRITES stayed in MODERATE (no switch at all). So on the 50%+ write presets adaptive will behave like Static Safe by design.
- **The 15 s window averages out MIXED_BURSTY.** Phases flip every 15 s, equal to the window, so the window never clears: write share swings only between about 0.34 and 0.65-0.92. In an 89 s run adaptive never returned to FAST after the first period and alternated MODERATE and SAFE (7 switches; FAST 17% / MODERATE 51% / SAFE 32% of the time). It does not track the bursts.
- **SPIKE bursts are diluted.** The 3 s bursts only lift the 15 s window to about 0.22-0.24 write share, so adaptive alternated FAST and MODERATE on the 20 s cycle and reached SAFE only once, at start-up, when the first window held only 5 s of data (share 0.59). It is never SAFE during a burst.
- **FAST really is stale at distance, even for a read-heavy collection.** `adaptive_cold` at 43 ms, all trials under FAST: 28 of 40 stale (70%), the same as `static_fast` (26 of 40, 65%). The earlier ~5% figure came from the scenario being mostly in MODERATE. A low write share does not make read-after-write safe on `nearest` / `local` reads; whether FAST is acceptable depends on whether the application reads its own writes.
- **Single quick-run latency differences are noise.** Adaptive's p95 in three identical READ_HEAVY runs was 81, 33 and 11 ms; the static configurations varied between 12 and 28 ms. Close other applications during the long runs and rely on the repeats.
- **Claim check at 43 ms (60 s runs, 10 users, one run per cell; files in `results/write_share_eval/_checks/claims_43ms/`):** READ_HEAVY: write median 4 / 180 / 4 ms and write p95 8 / 180 / 6 ms for Fast / Safe / Adaptive, in-load stale probes 39 of 60 (65%) / 0 of 44 / 33 of 52 (63%), adaptive in FAST 92% of the run. WRITE_HEAVY: write median 4 / 170 / 180 ms, stale probes 46 of 52 (88%) / 0 of 53 / 0 of 59, adaptive in SAFE 91% of the run. So adaptive matched Fast on read-heavy latency and Safe on write-heavy consistency, inheriting each one's cost (Fast's staleness; Safe's write latency). Fast was staler under write-heavy load (88%) than read-heavy (65%), which supports avoiding FAST on write-heavy traffic. Reads were 2-4 ms under every configuration at this light load.

**Open design questions raised by these findings (for the user, not decided):** (a) **decided 2026-10-17: keep window / interval / cooldown at 15 / 5 / 10 s** (set in Phase 8, before this work). Traffic that changes faster than the window cannot be tracked, which is an expected limit of any windowed adaptive system, and the report states it. Offline replay of the benchmark traffic through the real monitor, decision rule and loop (fake clock) showed the match with a perfect tracker is 18% (MIXED_BURSTY) and 30% (SPIKE) at 15/5/10 against 75% and 61% at 5/2/5, steady traffic is unaffected, and cooldown mostly guards against flapping; low-traffic behaviour of short windows was not tested; (b) there is no static MODERATE baseline, so on MODEST_WRITES adaptive is compared only with Fast and Safe; (c) the FAST staleness cost for applications that read their own writes (for example by using sessions or causal consistency, which this project does not do).

**Checking runs until then (quick mode, 1 repeat):** all 9 presets x 3 levels is about 37 min plus about 3 min staleness; 4 key presets (READ_HEAVY, WRITE_HEAVY, MIXED_BURSTY, MODEST_WRITES) x 2 levels (0, 43 ms) about 10 min; one preset at one level about 1 min. Caveat: quick runs are 20 s, shorter than the adaptive loop's timing (15 s window, 5 s interval, 10 s cooldown), so they can confirm which setting adaptive picks on steady traffic but cannot show it tracking MIXED_BURSTY or SPIKE; check those two with a longer run of just that preset (for example 90 s).

**Definition of Done:** adaptive's latency and staleness are measured against both baselines on traffic that exercises all three branches, with repeats; the switch log shows what it chose; the edge cases are tried; the rationale is written.

### Phase 13: final report, demo rehearsal, slides (unchanged)

Write the final report from the final evaluation (the Review II report is archived outside the repo, in `MongoLayer_archive/`); rerun `demo_app.py` timed, twice.

### Phase 14 (new, last): web interface

Deferred on purpose until everything above is done. It is rebuilt on the final, tuned middleware, so it should show the real behaviour (settings changes, monitor stats, per-node counters) rather than a stale design. The previous version (Flask + vanilla JS, traffic sliders, live settings, per-node opcounters) is in commit `649e248` as a starting point.

### Phase 15 (optional stretch): logistic-regression decision engine, per-document monitoring

Unchanged from "Open decisions" above; only after Phase 13 is safely done.

### Housekeeping (do early)

- Commit in logical groups (latency injection and Docker image; benchmark and notebook; docs; web interface removal) so the removal is a clear, revertable commit.
- Stale docs were removed on 2026-10-17: `DA2_REVIEW_SUMMARY.md`, `results_for_professor/REPORT.md` (deleted) and `DA2_Report.md` plus the rest of `results_for_professor/` (moved to `MongoLayer_archive/`, outside the repo). `DA2 Report.pdf` and `experiments_run1_old_rule.ipynb` stay untracked on purpose.
- `docs/PROJECT_DOCUMENTATION.md` was refreshed on 2026-10-16 to match the code; re-check it after any further change.
