# Next-level plan: SLO-aware, session-capable, faster-sensing adaptive consistency

Branch: `feature/next-level` (from `main` at d9f7baf). Written 2026-10-17.

This plan extends the finished v1 system (write-share rule, three fixed settings, 15 / 5 / 10 s timing) in four steps, ordered by what must come first. It builds on `plan/exec_plan.md` and does not change the v1 evaluation: v1 stays frozen as the baseline everything here is compared against.

---

## 1. Why this, and what the evidence says

What the current system and its evaluation showed (all measured in this repo; files named where they exist):

| Finding | Evidence |
|---|---|
| Traffic that shifts faster than the 15 s window cannot be tracked | MIXED_BURSTY / SPIKE settings timelines; offline replay: tracking 18% / 30% at 15/5/10 vs 75% / 61% at 5/2/5 |
| FAST is stale for reading your own writes, about 60-70% at 43 ms | `results/write_share_eval/_checks/staleness_quick_43ms.csv`, `claims_43ms/` |
| The controller sees only the write share, not staleness, lag or an application requirement | `adaptive_layer/decision.py` |
| Only three fixed actions; no way to ask for read-your-writes cheaply | `Settings` constants |
| **In our test bed MODERATE matches FAST's latency with 0% stale** (43 ms: write 1.5 / read 1.4 ms vs 1.5 / 1.3 ms, stale 0 / 60 vs 42 / 60) | measured 2026-10-17 |
| FAST + a causal session also gives 0% stale and cheap writes, but its reads wait about one replication delay (44 ms) | `_checks/causal_session_probe_43ms.txt` |

**The last two rows are the key planning fact.** The load generator is equally far from every node, so reading from the nearest replica has no advantage over reading from the primary. FAST's whole reason to exist (reads served by a nearby replica) is invisible in the current test bed, and MODERATE dominates it. Steps 1 and 2 below only mean something once the test bed can express "the client is close to a secondary and far from the primary". That is why the evaluation work is not "alongside" but **first**.

Related work this builds on (verification status in section 9): PBS (predicting staleness from latency), Harmony and OptCon (adapting the consistency level to a staleness or SLA target), Consistency Rationing (per-data consistency, switched at runtime), MongoDB causal sessions.

## 2. Goal and the claim we are trying to earn

> An adaptive layer that is told an application's staleness requirement, measures what its settings actually deliver, and can serve read-your-writes cheaply picks the least costly setting that meets the requirement, and does so faster and more reliably than the v1 workload-mix rule.

Not goals: reinforcement-learning or bandit policies (stretch only, section 8); per-key granularity; the web interface (still last).

## 3. Pre-registration (written before any of the new runs)

Each phase has a hypothesis and a pass criterion, fixed here. If a hypothesis fails, that is reported as a result. Parameters of the new components are frozen in a committed config file and tagged before the corresponding final runs, so nothing is tuned to the final data. The numeric thresholds marked (*) were confirmed as proposed on 2026-10-17 and are frozen together with the new components' parameters (section 7).

| ID | Hypothesis | Pass criterion |
|---|---|---|
| H0 (test bed) | With a client near one secondary and far from the primary, `nearest` reads are fast and primary reads are slow | `nearest` read median under 5 ms and primary read median at least 80 ms (T1 below) |
| H1 (session) | In that topology FAST + session removes FAST's staleness at a lower read cost than primary reads | 0 stale in the probes (at least 200 per cell); write median within 2x of FAST; read median lower than MODERATE's (*) |
| H2 (SLO controller) | The controller meets a stated staleness SLO while costing less than Static Safe, and violates it no more often than v1 | stale-read rate at or under the SLO in at least 95% of 60 s windows (*), and p99 latency below Static Safe's wherever a cheaper compliant setting exists |
| H3 (sensing) | Change detection tracks fast-shifting traffic without flapping | at least 60% match with the ideal setting on MIXED_BURSTY and SPIKE replay (*; v1: 18% / 30%); no more than 2x the ideal number of switches; at 10 ops/s steady traffic, no more than 1 switch per minute (*); on steady presets not worse than v1 by more than 5 points |

## 4. Phases

Order: **N0 -> N1 -> N2 -> N3 -> N4**. N0 is the prerequisite; N1 to N3 map to directions 1 to 3 from the research review; N0 and N4 are direction 6.

### N0. Evaluation foundation (prerequisite)

**Why:** without it N1 and N2 cannot be evaluated honestly (section 1).

| Step | What | Notes |
|---|---|---|
| N0.1 | **Static MODERATE baseline.** Add `static_moderate` next to `static_fast` / `static_safe` in `locustfile.py`, `run_benchmark.py`, `analyze_results.py`, the notebook and tests | Small. Also answers the open question that adaptive on MODEST_WRITES had no static comparator |
| N0.2 | **Per-link latency model.** Run the load generator inside a container on the replica-set network so client-to-node links can be delayed independently, and extend `netem.py` from one delay for all node pairs to a latency matrix (node-node and client-node, both directions) | The host cannot do this (Docker Desktop); a client container with `NET_ADMIN` can. Risks: container CPU overhead on Windows, extra image build on a slow link (the earlier 264 MB pull took hours) |
| N0.3 | **Pinned primary.** Give one node a higher election priority so the primary is deterministic (needed to define "far from the client") | MongoDB `rs.reconfig` priorities; to be verified here |
| N0.4 | **Topologies** (values derived the same way as the current levels, from the AWS / Azure figures cited in `netem.py`): **T0** = today (equidistant); **T1** far primary: client to primary 43 ms one-way, client to nearest secondary 1 ms, node-node 43 ms; **T2** asymmetric three-site: node-node latencies 5 / 43 / 43 ms | T1 is the one that matters for FAST |
| N0.5 | **Validate the test bed (H0)** before anything else is built on it: `nearest` vs primary read latency under T1 | Gate: if H0 fails, stop and fix |
| N0.6 | **Failure injection harness:** stop a secondary mid-run; stop the primary (forces an election); optionally a network partition | Also closes the unverified "secondary goes down" item from `exec_plan.md` |

**Done when:** H0 passes; `static_moderate` runs through the pipeline; tests cover the matrix and the new baseline; the old T0 results remain reproducible.

### N1. Session read-your-writes (direction 1)

**Design decisions (recommendation first):**
1. *Where session behaviour lives.* Add `read_your_writes: bool = False` to `Settings` and a named `FAST_RYW_SETTINGS` (FAST with the flag). Defaults keep every existing constant and test equal. Alternative (per-call hint only) is simpler but gives the controller nothing to choose.
2. *Session ownership.* A session is passed explicitly per logical caller (`client.session()` returning an `AdaptiveSession`; `write(..., session=)`, `read(..., session=)`). MongoDB sessions must not be used by two threads at once (stated in the official docs), and our client is shared across many threads, so the client cannot own one. If the flag is on but no session is passed, fall back to a plain FAST read and count the miss.
3. *Guarantee honesty.* MongoDB documents causal consistency only for majority read **and** write concern. FAST + session uses `w=1`, which worked here (0 of 60 stale) but is best-effort across a failover (an unreplicated write can be rolled back). The plan tests that case instead of assuming it and documents the limit.

**Work:** client API and flag; benchmark users each hold a session (a simulated user is a real client); probes use the user's session; analysis shows session vs no-session; tests: unit (arguments reach pymongo, thread-misuse guard, fallback) and an integration test with injected latency (no-session reads stale, session reads not; non-flaky at 20 trials because chance of zero stale without a session is about 1e-8).

**Experiment E1** (T0 and T1; 0 / 5 / 43 ms; repeats): FAST, FAST + session, MODERATE, SAFE, SAFE + causal majority. Report stale rate, write and read latency. Failover sub-experiment: step the primary down during `w=1` writes with a session and count read-your-writes violations vs `w=majority`.

**Done when:** H1 is decided (pass or fail, both reported); the failover behaviour is documented.

### N2. SLO-driven controller (direction 2)

**Idea (the PBS / Harmony / OptCon lineage, applied to MongoDB with live measurement):** the application states a requirement; the controller picks the cheapest setting whose measured staleness meets it.

| Piece | Design |
|---|---|
| SLO spec | `Slo(max_stale_prob, needs_read_your_writes, max_write_p99_ms=None)` per collection; no SLO = v1 behaviour (backward compatible) |
| In-library probes | Sampled shadow reads: after a small fraction of the application's own writes, read that document back with the active plain settings and record fresh / stale. No extra writes; budgeted overhead (rate cap) and a documented cost. The benchmark's probes are the prototype |
| Signals | probe stale estimate with a Wilson confidence interval; heartbeat round trip from `replSetGetStatus` (`pingMs`, which tracked the injected delay: 82 / 75 ms at 43 ms; may need monitoring privileges, so probes alone must also work); write share (kept) |
| Candidate order | by measured write cost: FAST, FAST_RYW, MODERATE, SAFE; per-setting latencies learned online from operations |
| Choice | the cheapest candidate whose **upper confidence bound** on staleness is within the SLO; hysteresis and cooldown as today; falls back to MODERATE or SAFE when signals are missing or stale; every switch logged with its reason |

**Tests:** fake-clock unit tests (SLO met or violated, signal loss, flapping guard, probe budget); integration under T0 / T1.
**Ablation (E2):** v1 write-share rule vs SLO controller vs all four statics, on all presets under T1 at several SLOs.
**Done when:** H2 is decided; probe overhead is measured and reported.

### N3. Faster and more robust workload sensing (direction 3)

**Idea:** keep a long window for stability and add a fast detector so the loop reacts to genuine shifts (CUSUM or EWMA change detection on the write share, or two windows), plus hysteresis. There is no database-specific prior work in my searches, so this is presented as an engineering contribution with its own evidence.

**Work:** promote the offline replay tool (`timing_sim.py`, currently in the scratchpad) into `benchmark/` with tests; add traces (MIXED_BURSTY, SPIKE, ramp, a diurnal-style trace, low-rate steady traffic); implement the detector behind the existing `decide` / `FeedbackLoop` seams; **explicitly test the case I have not tested: low traffic**, where a short window holds few operations and the share is noisy (min-operations guard).
**Experiment E3:** replay first (cheap, deterministic), then live runs of the best configuration only.
**Done when:** H3 is decided; the v1 headline numbers are unchanged (v1 stays the frozen baseline).

### N4. Final evaluation and write-up (direction 6, second half)

- Full matrix under T0 and T1 (T2 for the headline presets), 3 repeats, with the static baselines including MODERATE; failure-injection runs from N0.6.
- Compute: sized after N0-N3; quick checks first, long runs last, as already agreed.
- Correctness check of the session claims (stale-read counting is the minimum; a Jepsen-style checker is optional).
- Docs: update `PROJECT_DOCUMENTATION.md`, `RUNNING_GUIDE.md`, the report; the web interface last, now able to show write share, SLO status, probe staleness and the reason for each switch.

## 5. Dependencies and effort (relative: S small, M medium, L large)

```
N0.1 (S) ---------------------------------------------\
N0.2 (L) -> N0.3 (S) -> N0.4 (S) -> N0.5 (gate) -> N0.6 (M) -> N1 (M) -> N2 (L) -> N3 (M) -> N4 (L)
```
N0.1 can be done immediately. N3 can start in parallel once N0 is done, because its replay stage needs no cluster. N2 depends on N1 (it chooses FAST_RYW) and on N0.4.

## 6. Risks

| Risk | Mitigation |
|---|---|
| Client-in-container adds overhead or noise on Windows | measure it in N0.5; compare T0-in-container against T0-on-host before trusting T1 |
| Slow network makes image builds painful | build once, keep the image; do not prune Docker |
| `pingMs` needs privileges, or lags | probes alone must work; treat `pingMs` as an optional extra |
| `w=1` + session breaks across failover | test it (E1 failover), document it, never claim more than MongoDB documents |
| In-library probes cost more than they are worth | rate cap, measured overhead, a no-probe mode |
| Scope creep | RL, per-key and the web UI stay out until N4; each phase has its own pass or fail |
| Results tuned to the data | section 3: hypotheses and parameters fixed first |

## 7. Branch and commit workflow

- Work happens on `feature/next-level`; `main` keeps v1 untouched. The refreshed v1 docs (`PROJECT_DOCUMENTATION.md`, `SHOWCASE.md`, `RUNNING_GUIDE.md`, the `exec_plan.md` status) go in as their own commit on this branch before N0 starts. Outdated docs (`DA2_Report.md`, the old `results_for_professor/` files) were moved out of the repo to `MongoLayer_archive/` on 2026-10-17.
- One commit per coherent step; every commit must pass the tests (checked by exporting the staged snapshot, as before).
- New results go under `results/v2_eval/` (gitignored like the rest); v1 results are never overwritten.
- Tag `v1-final` on `main` before the first N-phase final run, and `v2-config-frozen` when the new parameters are frozen.

## 8. Stretch (only if time remains)

A learned policy (decision tree as in OptCon, or a contextual bandit; I found no bandit-for-consistency paper in two searches), with the SLO controller as its safe fallback. The logs it would train on already exist (switch logs, probes, per-operation latency). Per-key or per-operation consistency hints (Consistency Rationing, OptCon).

## 9. References and how well each was verified

| Source | Verified how |
|---|---|
| MongoDB, Read Isolation, Consistency, and Recency (official docs) | page read; conditions (majority read and write concern, one thread per session) quoted from it. The "secondary waits for afterClusterTime" behaviour is not on that page |
| OptCon, Sidhanta et al., CCGrid 2016, arXiv 1603.07938 | abstract read |
| PBS, Bailis et al., VLDB 2012, arXiv 1204.6082 | search summary only |
| Harmony, 2012 (IEEE Xplore 6337791) | search summary only; check the quoted gains in the paper |
| Consistency Rationing, Kraska et al., PVLDB 2009 | search summary only; categories B and C not confirmed |
| Continuous Partial Quorums (arXiv 1507.03162); Characterizing and Adapting the Consistency-Latency Tradeoff (arXiv 1509.02464) | search summaries only |
| Adaptive Consistency via Machine-Learned Policy Control, IJEAT 2026 (Zenodo 20961163) | record page and abstract only; smaller venue, treat as a lead |

## 10. Decisions (made 2026-10-17: the recommendations were accepted, keeping it simple)

1. **Test bed (N0.2):** a client container with per-link latency; the load generator moves into the container.
2. **Thresholds marked (*) in section 3:** accepted as proposed.
3. **SLO form for N2:** a single staleness bound per collection (plus the needs-read-your-writes flag); no latency target in the first version.
4. **v2 beside v1:** v1 stays frozen as the baseline in every comparison.
