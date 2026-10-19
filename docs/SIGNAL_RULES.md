# Additions: more than the traffic ratio

> Branch `feature/adaptive-signals-lite` (from `main`). Eight small additions chosen from the twelve
> that were proposed. Each is a few dozen lines, has its own unit tests, and was checked on the
> live replica set. **The full evaluation and its analysis are in [`FINAL_REPORT.md`](FINAL_REPORT.md).**
> The other four (#2 round-trip check, #3 replication-lag guard, #4 node-health
> fall-back, #7 hot keys) are not on this branch; they are kept in `git stash` ("adaptive-signals:
> all 12 changes ...").

---

## 1. Why

On `main`, the layer decides from **one number**: the write share of the last 15 s
(writes / all operations). Below 15% writes it picks FAST, above 40% it picks SAFE, otherwise MODERATE.

That rule leaves four questions open, and the additions answer them:

| Open question | Addition |
|---|---|
| Does FAST ever pay off? In the original test bed every node is the same distance from the client, so `nearest` can't be faster than reading from the primary. | **#1** topologies with unequal distances, **#12** a pinned primary |
| What about the user who writes and then immediately reads back? Under FAST that read can miss the write. | **#6** read-your-own-writes per key |
| What if SAFE is too expensive? With nodes 86 ms apart a majority write takes ~260 ms. | **#9** latency budget for SAFE |
| Does every collection deserve the same treatment? Orders are not logs. | **#5** per-collection floor / ceiling |
| Plus: switching should be calm and explainable, and the comparison fair. | **#8** hysteresis, **#10** reasons, **#11** static MODERATE baseline |

Everything new is opt-in. `FeedbackLoop(client, monitor)` with no extra arguments behaves exactly
like `main`, so the original `adaptive` configuration stays in the benchmark as the baseline.

---

## 2. The eight additions at a glance

| # | Addition | Where | What it changes | Complexity (1-10) |
|---|---|---|---|---|
| 1 | **Nodes at different distances** (topologies T1, T2) | `benchmark/topology.py` | the client can be next to one secondary and far from the primary | 6 |
| 5 | **Per-collection floor and ceiling** | `rules.py` (`CollectionPolicy`) | e.g. `orders` never below SAFE, `logs` never above FAST | 2 |
| 6 | **Read-your-own-writes per key** | `client.py` | a read of a key or `_id` this client wrote < 5 s ago goes to the primary; all other reads stay `nearest` | 4 |
| 8 | **Hysteresis** | `decision.py` (`Thresholds.hysteresis`) | leave SAFE only below 0.35 and FAST only above 0.20 (enter at 0.40 / 0.15) | 1 |
| 9 | **Latency budget for SAFE** | `monitor.py`, `rules.py`, `feedback.py` | SAFE → MODERATE when SAFE writes' p95 > 100 ms; remembered for 60 s | 4 |
| 10 | **Say why it switched** | everywhere | every switch carries a reason string, saved in the switch log CSV | 2 |
| 11 | **Static MODERATE baseline** | locustfile, runner, staleness test, analysis | a 4th fixed configuration: `w=1`, primary reads | 2 |
| 12 | **Pinned primary + no chained replication** | `benchmark/cluster.py` | mongo1 is always primary; secondaries copy straight from it | 2 |

---

## 3. How a decision is made now

```
   Stats (monitor, 15 s window): write share, p95 write latency per setting
                     │
                     ▼
  ┌──────────────────────────────────────────┐
  │ traffic rule  decide_explained()         │  FAST / MODERATE / SAFE + "write share 0.05 < 0.15"
  │ + hysteresis #8 (uses the current state) │
  └──────────────────┬───────────────────────┘
                     ▼
  ┌──────────────────────────────────────────┐
  │ latency budget #9 (only if SAFE)         │  "SAFE write p95 264 ms > budget 100 ms -> MODERATE"
  └──────────────────┬───────────────────────┘
                     ▼
  ┌──────────────────────────────────────────┐
  │ policy clamp #5 (floor / ceiling)        │  always last: the application has the final word
  └──────────────────┬───────────────────────┘
                     ▼
        Decision(settings, reasons #10)  ──►  FeedbackLoop.tick(): apply if different and the
                                              cooldown has passed; log SwitchEvent with the reason

  Separately, in the client, per read:  FAST read of a key/_id written < 5 s ago  ──►  primary  (#6)
```

`apply_rules()` (`adaptive_layer/rules.py`) is a pure function of plain values (`Stats`,
`SignalConfig`, `CollectionPolicy`), so every rule is unit tested with hand-built inputs
(`tests/test_rules.py`).

---

## 4. Each addition in detail

### 1. Nodes at different distances (`benchmark/topology.py`)

**Problem.** `benchmark/netem.py` delays every node-to-node link by the same amount and leaves the
client alone, so the client is the same distance from every node. In that test bed FAST's
`nearest` reads can never beat primary reads, and no experiment can show what FAST is *for*.

**What it does.** Every link gets its own delay:
- *node ↔ node*: each node delays packets to a peer by half that pair's round trip (`tc netem`
  in a `prio` band per destination).
- *client ↔ node*: the client runs on the host, and on Docker Desktop the host can't get `tc`
  rules of its own. So each node delays **both** directions of its link to the host (the bridge
  gateway): replies on egress, requests on ingress through an `ifb` device, half the extra round
  trip each. With only one direction delayed, a write would reach the primary instantly and
  read-after-write would look fresher than in a real geography.

| Topology | Node round trips | Client round trips | What it shows |
|---|---|---|---|
| `EQUAL-<ms>` | all pairs equal | ~1 ms to every node | the original test bed |
| **T1** "far primary" | 86 ms between all nodes | mongo2 ~1 ms, mongo1 (primary) and mongo3 ~87 ms | FAST reads are local; SAFE is expensive |
| **T2** "regional pair" | mongo1–mongo2 10 ms, mongo3 86 ms | mongo2 ~1 ms, mongo1 ~11 ms, mongo3 ~87 ms | a majority needs only the near pair, so SAFE is cheap |

**Live check C1.** T1 client round trips: mongo2 1.3 ms, mongo1 87.9 ms, mongo3 87.0 ms; node round
trip 86.4 ms. EQUAL-5: node round trip 10.3 ms, client ~1 ms to all three. Primary mongo1.

`python -m benchmark.topology T1` applies a topology and prints the measured client round trips;
`python -m benchmark.topology clear` removes it.

### 5. Per-collection floor and ceiling (`rules.py`)

```python
FeedbackLoop(client, monitor, policies={
    "orders": CollectionPolicy(floor="SAFE"),          # never weaker than SAFE
    "logs":   CollectionPolicy(ceiling="FAST"),        # never stronger than FAST
    "profiles": CollectionPolicy(ceiling="MODERATE"),  # never SAFE (latency matters more)
})
```

Applied last, after every other rule, including the latency budget. A collection whose floor is
above MODERATE (or whose ceiling is below it) gets that setting **at construction**, so its first
operations never run on the MODERATE default. Invalid levels and floor > ceiling are rejected.

**Live check C3.** Read-heavy traffic (5% writes) on a `floor="SAFE"` collection stayed SAFE, with
the reason "write share 0.05 < 0.15; policy floor SAFE -> SAFE".

### 6. Read-your-own-writes per key (`client.py`)

**Problem.** Under FAST, a user who writes a document and immediately reads it back can get
nothing from a secondary that hasn't replicated it yet. On T1 that happened in **48 of 100** trials.

**What it does.** `AdaptiveClient(..., key_field="key", read_own_writes_seconds=5)` remembers the
`key_field` value and `_id` of every document this client wrote, for 5 s. A read whose query asks
for one of them **by equality** goes to the primary; every other read keeps using `nearest`. Reads
under MODERATE are not counted as redirects, because `primaryPreferred` already reads the primary.

**Scope.** Per client process: it covers this process's own writes, not writes made by other
application servers. Only equality queries match (`{"key": 7}`, not `{"key": {"$in": ...}}`).
5 s comfortably outlasts the replication delay on this test bed (well under a second even at
86 ms between nodes).

**Live check C2 (T1, FAST, 100 write-then-read trials):**

| | stale | own-write read p50 | other reads p50 |
|---|---|---|---|
| plain FAST | 48 / 100 | 2.1 ms | 1.7 ms |
| FAST + per-key routing | **0 / 100** | 87.4 ms (primary) | **1.9 ms** (still local) |

Only the reads that need it pay the primary's distance.

### 8. Hysteresis (`decision.py`)

`Thresholds(hysteresis=0.05)` (`rules.SIGNALS_THRESHOLDS`): entering SAFE still needs a share above
0.40, but *leaving* it needs a share below 0.35. FAST is entered below 0.15 and left above 0.20. A
share hovering on a threshold therefore doesn't flip the setting back and forth every cooldown.
0.05 is a third of the 15-point FAST zone. The two "stay" bands must not overlap
(`hysteresis < (high − low) / 2`). The default is 0, which is exactly the original rule.

**Live check C5 (equal distances).** 60% writes → SAFE, then 37.5% writes:

| | after the drop to 37.5% writes | switches |
|---|---|---|
| traffic rule alone | **MODERATE** ("write share 0.38 between 0.15 and 0.40") | 2 |
| with hysteresis | **SAFE** ("write share 0.37 > 0.35 (hysteresis: staying SAFE)") | 1 |

### 9. Latency budget for SAFE (`monitor.py`, `rules.py`, `feedback.py`)

**Signal.** The client gives the monitor each operation's latency and the name of the settings it
ran under. `Stats.write_p95("SAFE")` is the 95th-percentile latency (nearest rank) of SAFE writes
in the window.

**Rule.** In SAFE, with at least 20 SAFE writes measured, if their p95 exceeds **100 ms**, drop to
MODERATE. 100 ms is the commonly cited limit for an action to feel instant (general usability
guidance, not measured here). Once the layer has left SAFE,
no new SAFE samples arrive, so the feedback loop **remembers the breach for 60 s** before letting
SAFE be tried again; otherwise it would bounce straight back. The switch still respects the cooldown.

**Live check C4 (T1, 60% writes).** SAFE → MODERATE after 4.2 s: "SAFE write p95 264 ms > budget 100 ms".

The budget is a performance policy, not a safety rule: it trades durability (`w=1` instead of
majority) for latency, on purpose. A collection that must stay durable whatever the cost uses
`CollectionPolicy(floor="SAFE")`, which overrides it. On equal distances and on T2, SAFE writes
are well inside 100 ms, so the budget never fires there.

### 10. Say why it switched

`decide_explained()` returns the traffic rule's reason ("write share 0.52 > 0.40"); the budget and
the policy append theirs ("... -> MODERATE"). `SwitchEvent.reason` holds the joined string,
`FeedbackLoop.last_decisions[collection]` holds the latest `Decision` (switched to or not), and the
switch-log CSV has a `reason` column. Every live check verifies that every switch has a reason.

### 11. Static MODERATE baseline

`static_moderate` (`w=1`, local read concern, `primaryPreferred`) is a fixed configuration in the
locustfile, the runner, the staleness test and the analysis. Without it, adaptive was compared
only against the two extremes. On equal distances MODERATE has the same latency as FAST and no
stale reads, so it is the baseline FAST has to beat.

### 12. Pinned primary, no chained replication (`benchmark/cluster.py`)

`python -m benchmark.cluster` (also run by `run_benchmark --topology`, `signals_check` and the
notebook) gives mongo1 election priority 10 and sets `chainingAllowed=false`, then waits until
mongo1 is primary with two healthy secondaries that both replicate **straight from it**.
- "far from the primary" only means something if the primary is always the same node; MongoDB
  re-elects whenever a node restarts.
- with chaining on, a secondary may copy from the *other* secondary; the extra hop adds a whole
  inter-node delay (in one earlier T1 run, mongo2 syncing from mongo3 made FAST 100% stale instead of
  27%; recorded in `plan/next_level_plan.md` on `feature/next-level`).

---

## 5. Parameters (all fixed before the runs that report on them)

| Parameter | Value | Where | Basis |
|---|---|---|---|
| traffic thresholds | 0.15 / 0.40 | `decision.py` | unchanged from `main` |
| hysteresis | 0.05 | `rules.SIGNALS_THRESHOLDS` | a third of the FAST zone |
| read-own-writes memory | 5 s | locustfile, `signals_check` | well above the replication delay |
| SAFE write budget / samples / back-off | 100 ms / 20 / 60 s | `rules.SignalConfig` | commonly cited "feels instant" limit |
| T1 / T2 distances | 86 ms / 10 ms round trips | `topology.py` | the AWS / Azure figures `netem.py` cites |

---

## 6. Network setups: why 0 ms, T1 and T2 (and not 5 or 43 ms)

- **43 ms equal delay: dropped.** T1 already has an 86 ms (= 2 × 43 ms) round trip between nodes,
  and on top of it the client distances that make the comparison meaningful.
- **5 ms equal delay: not worth a new 1.5 h.** The overnight run of 2026-10-19 measured 47 of its 50
  runs at 5 ms (tag `FULL_10H_2026-10-19_0231`, raw data in `results/signals_eval/`):
  - latency: practically unchanged; only SAFE write medians get 5–7 ms slower (read-heavy 17 → 24 ms),
    and SAFE's p95 stays at 33–42 ms (83 ms at 200 users);
  - staleness: static FAST goes from ~0% (RAMP 1.4%) to 0.6–10.9% stale (76.9% on RAMP, where the light load
    means reads come back faster than replication): FAST pays in
    staleness for no speed gain when every node is equally far.

  With these eight additions the layer behaves at 5 ms as at 0 ms (SAFE stays far inside the
  100 ms budget), so a new 5 ms run would repeat what that data already shows. Add `"EQUAL-5"` to
  the notebook's `CUSTOM` setups to include it anyway (+1 h 30 min).

---

## 7. How to run it

```bash
docker compose up -d                       # replica set
python -m pytest -q                        # 112 tests (unit tests + live-cluster tests)
python -m benchmark.cluster                # pin mongo1, chaining off (the commands below also do this)
python -m benchmark.signals_check          # 5 live checks, ~2 min, PASS/FAIL each
python -m benchmark.topology T1            # apply a topology by hand (and `clear`)
```

### The full evaluation in one notebook: `signals_evaluation.ipynb`

Profile **`FULL`** (the default): 3 network setups (EQUAL-0, T1, T2) × 5 configurations × 6
core workloads = **90 Locust runs** of 180 s (SPIKE 108 s), 1 repeat, plus the 5 live checks and
the staleness experiment on every setup. **Estimated 4 h 35 min** from the measured per-run
overhead. The six workloads each show one addition at work:

| Workload | Shows |
|---|---|
| READ_HEAVY | FAST + per-key read-your-own-writes (#6) |
| READ_HEAVY_HOTSPOT | the cost of routing hot keys to the primary (#6) |
| MODEST_WRITES | MODERATE |
| BALANCED | the SAFE latency budget (#9) |
| MIXED_BURSTY | hysteresis and switching (#8, #10) |
| SPIKE | short write bursts |

The other four add little for these additions: WRITE_HEAVY and HOTSPOT behave like BALANCED (all
SAFE), RAMP only tests a slow ramp-up, and HIGH_CONCURRENCY is a load test of the layer itself.
Profile `FULL_ALL` includes them (150 runs, ~7 h 40 min). Profile `SMOKE` checks the whole
pipeline in ~15 min. Running setups in parallel is not an option: there is one cluster, one
network setup at a time, and two load tests at once would distort each other's latency.

| Step | Cell | Stops the run if... |
|---|---|---|
| Settings | `PROFILE`, `RUN_TAG` (resume), `STOP_ON_CHECK_FAIL`, `CUSTOM` plan | – |
| Plan | every phase with its estimate and the expected finish time | – |
| Preflight | Docker up, no leftover delays, primary pinned + direct replication, T1 applies and measures, all tests pass, disk space | any check fails |
| Live checks | the 5 scenarios, one line each | any scenario fails |
| Load tests | one line per run with the time left; full Locust output in `run_log.txt` | – |
| Staleness | per setup | – |
| Results | integrity (all runs finished, no hung run, no failures, probes present, checks passed); **expectations fixed before the run**; one table per setup; 5 figures; every switch reason | – |
| Store | committable copy in `docs/results/signals/<tag>/` | – |

Everything goes to `results/signals_eval/<tag>/`. Every finished run leaves a `.done` marker:
after an interruption, set `RUN_TAG` to the printed tag and run again, and finished runs, checks
and staleness setups are skipped. **A run that hangs is killed 2 minutes past its length (with its
child processes), retried once, and recorded as failed if the retry fails too**, so one stuck run
can't block the evening. Headless, saving the notebook after every cell:

```powershell
.venv\Scripts\python -m benchmark.run_notebook signals_evaluation.ipynb
```

Before the long run: laptop plugged in, Windows sleep set to *Never*, Docker Desktop running.

**The hang of 2026-10-19 and its fix.** The first overnight run stopped making progress after 97
runs: Locust runs on gevent, which swapped the locks in the monitor and client for its own; one
broke while Locust stopped its users ("Semaphore released too many times"), one user survived and
the process never exited. The locks are now plain OS locks (`adaptive_layer/_locks.py`; safe because
nothing inside them waits on anything), and the watchdog above guards against any other hang.

### Use it in your own code

```python
from adaptive_layer.client import AdaptiveClient
from adaptive_layer.feedback import FeedbackLoop
from adaptive_layer.monitor import AccessPatternMonitor
from adaptive_layer.rules import SIGNALS_THRESHOLDS, CollectionPolicy, SignalConfig

monitor = AccessPatternMonitor(window_seconds=15)
client = AdaptiveClient(URI, "shop", monitor=monitor, key_field="sku", read_own_writes_seconds=5)
loop = FeedbackLoop(
    client, monitor, interval_seconds=5, cooldown_seconds=10,
    thresholds=SIGNALS_THRESHOLDS, signals=SignalConfig(),
    policies={"orders": CollectionPolicy(floor="SAFE")},
)
loop.start()
...
print(loop.last_decisions["catalog"].reason)   # why catalog is on its current setting
```

---

## 8. Early results (before the full run)

**The full run's results are in [`FINAL_REPORT.md`](FINAL_REPORT.md)** (run `FULL_2026-10-19_1452`:
90/90 runs, 0 failed requests, 8/8 pre-stated expectations held; data in
[`results/signals/FULL_2026-10-19_1452/`](results/signals/FULL_2026-10-19_1452/)). This section keeps the
short runs from earlier on 2026-10-19 that first showed each addition working.

**Live checks: 5/5 PASS** (`python -m benchmark.signals_check`, run twice, both passes):

| Check | Result |
|---|---|
| C1 distances | T1 client: mongo2 0.9–1.3 ms, primary mongo1 ~87.5 ms, mongo3 ~87 ms; T1 nodes ~86.4 ms; EQUAL-5 nodes 10.3 ms; primary mongo1 |
| C2 own writes | plain FAST 48–49 / 100 stale; with per-key routing **0 / 100**, other reads still 1.8–1.9 ms |
| C3 policy | 5%-write traffic on a `floor="SAFE"` collection: stays SAFE |
| C4 budget | SAFE → MODERATE after 4.2 s: "SAFE write p95 263–264 ms > budget 100 ms" |
| C5 hysteresis | after SAFE, at 37.5% writes: traffic rule alone → MODERATE (2 switches), with hysteresis stays SAFE (1 switch) |

**Smoke run of the notebook** (profile `SMOKE`: READ_HEAVY and BALANCED, 30 s per run, 50 users,
EQUAL-0 and T1; 14.6 min end to end). Integrity: 20/20 runs, 0 failed requests, no hung or
retried run, 5/5 checks. Every expectation that applies held. On T1:

| Workload | Configuration | read p50 / p95 ms | write p50 / p95 ms | stale probes | time FAST / MOD / SAFE % |
|---|---|---|---|---|---|
| READ_HEAVY | static_fast | 9 / 18 | 88 / 96 | **26.5%** | – |
| | static_moderate | 88 / 92 | 88 / 93 | 0% | – |
| | static_safe | 88 / 92 | 230 / 260 | 0% | – |
| | adaptive (traffic rule) | 3 / 91 | 88 / 100 | **11.4%** | 82 / 18 / 0 |
| | **adaptive_signals** | **3 / 91** | 88 / 94 | **0%** | 82 / 18 / 0 |
| BALANCED | static_fast | 3 / 9 | 88 / 92 | 27.8% | – |
| | static_safe | 89 / 98 | 250 / 270 | 0% | – |
| | adaptive (traffic rule) | 89 / 99 | **230 / 260** | 0% | 0 / 18 / 82 |
| | **adaptive_signals** | 88 / 110 | **91 / 260** | 0% | 0 / 65 / 35 (budget) |

- **Read-heavy on T1** is the case FAST exists for. The traffic rule alone uses FAST and pays with
  11.4% stale reads. With the additions it makes the same choice (3 ms median reads) with 0%
  stale: only reads of the client's own recent writes go to the 87 ms primary.
- **Balanced on T1:** the budget moves SAFE (~250 ms writes) to MODERATE, so median writes drop
  from 230 ms to 91 ms. This trades majority durability for latency, on purpose. `floor="SAFE"`
  prevents it for a collection that must stay durable.
- **Equal 0 ms:** every configuration has the same latency (15–25 ms medians, set by the load
  generator) and 0% stale. SAFE stays (write p95 42 ms, inside the budget).

**The overnight hang, re-tested.** The run that hung on 2026-10-19 (`adaptive_signals`,
HIGH_CONCURRENCY, 200 users, equal 5 ms) was repeated 4 times with the lock fix and without the
watchdog: all 4 exited normally, 0 failed requests. The hang was intermittent (1 run in ~10), so
this is not proof. The watchdog covers any remaining case.

**Equal 5 ms** (overnight data, 47 of 50 runs): see §6.

---

## 9. Limitations (stated plainly)

- **No guard against a lagging or failed secondary.** Without #3/#4, FAST keeps reading from a
  secondary that has fallen behind, and SAFE writes wait for as long as a majority is unavailable.
  Rule #6 still protects a client's own writes.
- **FAST is still chosen on equal distances** (no #2), where it gains nothing and adds staleness;
  the evaluation shows this honestly (EQUAL-0 rows).
- **Per-process view.** The monitor and the read-your-own-writes memory see only this process's
  operations; several application servers each adapt on their own traffic.
- **The budget weakens durability on purpose.** A collection that must never accept `w=1` needs
  `floor="SAFE"`.
- **Single runs.** One repeat per cell: no error bars. Plain FAST's stale rate varied 28–48% between
  passes of the same check.
