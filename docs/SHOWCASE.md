# How to Start This Project for a Showcase

A step-by-step runbook for actually demoing the Adaptive Consistency Layer live — to a teammate, a grader, or yourself before a presentation. For the deeper one-time replica set setup details, see [`replica_set_setup.md`](replica_set_setup.md); this doc is the "get it running right now" version.

---

## 0. One-time setup (only needed once per machine, ever)

Skip this section if you've already run this project on this machine before.

1. **Clone the repo and create the virtual environment:**
   ```bash
   git clone https://github.com/NonCompactdesign/MongoLayer.git
   cd MongoLayer
   python -m venv .venv
   source .venv/Scripts/activate   # Windows Git Bash; use .venv\Scripts\activate.bat on cmd
   pip install -r requirements.txt
   ```

2. **Add three hosts file entries** (Windows: `C:\Windows\System32\drivers\etc\hosts`, edit as Administrator — see [`replica_set_setup.md`](replica_set_setup.md) for why this is needed):
   ```
   127.0.0.1 mongo1
   127.0.0.1 mongo2
   127.0.0.1 mongo3
   ```

3. **Start Docker Desktop** (if it isn't already running), then bring up the replica set for the first time:
   ```bash
   docker compose up -d
   docker exec mongo1 mongosh --port 27017 --quiet --eval '
   rs.initiate({
     _id: "rs0",
     members: [
       { _id: 0, host: "mongo1:27017" },
       { _id: 1, host: "mongo2:27018" },
       { _id: 2, host: "mongo3:27019" }
     ]
   })
   '
   ```
   This only needs to run once — the replica set config persists in Docker's named volumes across restarts. Don't re-run `rs.initiate()` on a machine where the containers have already been initiated; it'll error (harmlessly) if you do.

---

## 1. Every time, before the showcase

1. **Make sure Docker Desktop is running.** If it isn't, start it and wait ~30–60s for the daemon to come up.

2. **Bring the replica set back up** (data survives restarts in the named volumes):
   ```bash
   docker compose up -d
   ```

3. **Verify it's healthy** — expect one `PRIMARY`, two `SECONDARY` (doesn't matter which node holds which role; that can shift between restarts, it's normal):
   ```bash
   docker exec mongo1 mongosh --port 27017 --quiet --eval '
   var s = rs.status();
   s.members.forEach(function(m) { print(m.name + " -> " + m.stateStr); });
   '
   ```
   If this errors with "not primary," just retry against `mongo2`/`mongo3` — whichever one is actually primary right now will answer.

4. **Activate the virtual environment:**
   ```bash
   source .venv/Scripts/activate
   ```

5. **(Optional but recommended) Run the test suite as a final sanity check** before showing anyone anything:
   ```bash
   python -m pytest tests/ -q
   ```
   Should print `112 passed` (some of them use the live replica set). If it doesn't, fix that before the showcase — don't discover a broken build live.

6. **Pin the primary** (mongo1 primary, secondaries replicating straight from it). Every command in Section 2 also does this itself, but running it once up front shows the set is healthy:
   ```bash
   python -m benchmark.cluster
   ```

---

## 2. The main event: the additions, live

The web dashboard was removed from `main` (it will be rebuilt last). The live centrepiece is the
check script: five short scenarios, each showing one addition at work on the real replica set,
with the reason the layer gave.

```bash
python -m benchmark.signals_check          # all five, about 2 minutes
python -m benchmark.signals_check C2 C5    # or pick the ones you want to show
```

| Scenario | What happens on screen | The point to make |
|---|---|---|
| **C1** distances | topology T1 measured: client ~1 ms from mongo2, ~87 ms from the primary mongo1 and from mongo3 | the test bed can put a replica next to the client (#1, #12) |
| **C2** own writes | 100 write-then-read trials on T1: plain FAST ~30-50 of 100 stale; with per-key routing **0** stale while other reads stay ~2 ms | read-your-own-writes without giving up FAST for everyone (#6) |
| **C3** policy | read-heavy traffic on a `floor="SAFE"` collection stays SAFE | the application has the final word (#5) |
| **C4** budget | write-heavy on T1: SAFE p95 ~264 ms > 100 ms budget → MODERATE | SAFE has a price, and the layer can see it (#9) |
| **C5** hysteresis | after SAFE, traffic drops to 37.5% writes: the traffic rule alone goes to MODERATE, with hysteresis it stays SAFE | no flip-flopping on a threshold (#8) |

Each scenario prints its JSON result, including the switch reasons (#10), and PASS/FAIL. All
results are also written to `results/signals_eval/checks.json`.

To show the distances themselves:
```bash
python -m benchmark.topology T1      # prints client round trips: mongo2 ~1 ms, mongo1/mongo3 ~87 ms
python -m benchmark.topology clear
```

---

## 3. Alternative: the scripted console demo

```bash
python demo_app.py
```

A non-interactive, narrated alternative to the dashboard — three scripted traffic phases against the real replica set, printed to the console instead of a browser. Useful as a fallback if a live/interactive demo feels risky right before a presentation, or if you just want a clean saved log (see below) rather than something to click through live.

| Phase | Traffic | Watch for |
|---|---|---|
| A | 20 reads, no writes | Settings relax to `FAST_SETTINGS` (`w=1`, local, nearest) |
| B | 60 rapid writes | Settings escalate to `SAFE_SETTINGS` (majority, majority, primary) |
| C | Mixed reads/writes | Settings settle at `MODERATE_SETTINGS` |

Takes about a minute end-to-end (there are deliberate pauses between phases so the monitor's sliding window fully clears — the script prints why it's pausing, so it doesn't look like it's hung).

A saved transcript of a clean run is at [`demo_runs/demo_run_2026-09-10.log`](demo_runs/demo_run_2026-09-10.log) if you want to show the expected output without waiting live, or as a fallback if something goes wrong during a live run.

---

## 4. Supporting evidence (if there's time / it's asked for)

**The full evaluation** (about 4 h 35 min; run it the night before, not live): open `signals_evaluation.ipynb` and Run All, or run it headless:
```powershell
.venv\Scripts\python -m benchmark.run_notebook signals_evaluation.ipynb
```
Its results section (integrity, expectations, tables, figures) is what to show afterwards. The write-up of the 19 Oct 2026 run, with the journey, every addition and the analysis, is [`FINAL_REPORT.md`](FINAL_REPORT.md).

**Benchmark comparison** (five configurations: static FAST / MODERATE / SAFE, adaptive, adaptive with the additions):
```bash
python -m benchmark.run_benchmark --topology T1 --presets READ_HEAVY BALANCED --duration 60s --repeats 1
python -m benchmark.signals_report
```
About 1 minute per (configuration × preset), so ~11 minutes for this example. `signals_report` prints the summary table (latency, stale probes, time in each setting, switch reasons). The equal-distance latency sweep is still `python -m benchmark.run_benchmark --quick`. The full evaluation notebook stores its tables and figures in `docs/results/signals/<tag>/`.

**Staleness measurement** (the read-after-write correctness tradeoff, not just speed):
```bash
python -m benchmark.staleness_test --quick
```
Note the `python -m benchmark.staleness_test` form (not `python benchmark/staleness_test.py`) — running it as a script directly breaks the internal package imports. Prints a stale-read rate for five scenarios (static fast/moderate/safe, adaptive under hot and cold conditions) and — the interesting part — explains *why* the adaptive layer's "fast" setting is actually safer in practice than the static fast baseline, not just faster. Worth reading that explanation out loud if asked "so what does this actually prove."

---

## 5. Shutting down afterward

```bash
docker compose down       # stop containers, keep the replica set data
docker compose down -v    # stop AND wipe data (only if you want a truly clean slate — re-run rs.initiate() after this)
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `failed to connect to the docker API` | Docker Desktop isn't running — start it, wait ~30–60s |
| `mongosh` command says `MongoServerError: not primary` | The replica set re-elected primary onto a different node (normal, happens after restarts) — retry the same command against `mongo2` or `mongo3` |
| `pymongo.errors.ServerSelectionTimeoutError` from Python | Usually means the hosts file entries (`mongo1`/`mongo2`/`mongo3` → `127.0.0.1`) are missing — see Section 0, step 2 |
| `ModuleNotFoundError: No module named 'adaptive_layer'` / `'benchmark'` | You ran a `benchmark/` script directly instead of as a module — use `python -m benchmark.run_benchmark` / `python -m benchmark.signals_check`, not `python benchmark/run_benchmark.py` |
| A node still has injected delay after an interrupted run | `python -m benchmark.topology clear` |
| Demo/benchmark leaves test data behind | Harmless — everything writes to its own throwaway collection/database and cleans up after itself in normal completion; if a run was killed mid-way, `docker exec mongo1 mongosh --port 27017 --eval 'db.getSiblingDB("<db_name>").dropDatabase()'` clears it (try `mongo2`/`mongo3` if you get "not primary") |
