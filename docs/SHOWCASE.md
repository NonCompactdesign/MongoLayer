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
   Windows PowerShell: `.venv\Scripts\Activate.ps1` instead of `source .venv/Scripts/activate` (if this errors with "running scripts is disabled", run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first, in that same session only).

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
   **Windows PowerShell:** the multi-line `--eval '...'` block above gets mangled by PowerShell's argument quoting (it doesn't preserve nested `"..."` inside `'...'` the way bash does when handing the string to a native executable). Use this single-line form instead, with the quote styles swapped:
   ```powershell
   docker exec mongo1 mongosh --port 27017 --quiet --eval "rs.initiate({ _id: 'rs0', members: [ { _id: 0, host: 'mongo1:27017' }, { _id: 1, host: 'mongo2:27018' }, { _id: 2, host: 'mongo3:27019' } ] })"
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
   **Windows PowerShell** (see the quoting note in Section 0, step 3):
   ```powershell
   docker exec mongo1 mongosh --port 27017 --quiet --eval "var s = rs.status(); s.members.forEach(function(m) { print(m.name + ' -> ' + m.stateStr); });"
   ```
   If this errors with "not primary," just retry against `mongo2`/`mongo3` — whichever one is actually primary right now will answer.

4. **Activate the virtual environment:**
   ```bash
   source .venv/Scripts/activate
   ```
   Windows PowerShell: `.venv\Scripts\Activate.ps1`

5. **(Optional but recommended) Run the test suite as a final sanity check** before showing anyone anything:
   ```bash
   python -m pytest tests/ -q
   ```
   Should print `34 passed`. If it doesn't, fix that before the showcase — don't discover a broken build live.

---

## 2. The live demo

```bash
python demo_app.py
```

A non-interactive, narrated demo — three scripted traffic phases against the real replica set, printed to the console. Useful if you just want a clean saved log (see below) rather than something to click through live.

| Phase | Traffic | Watch for |
|---|---|---|
| A | 20 reads, no writes | Settings relax to `FAST_SETTINGS` (`w=1`, local, nearest) |
| B | 60 rapid writes | Settings escalate to `SAFE_SETTINGS` (majority, majority, primary) |
| C | Mixed reads/writes | Settings settle at `MODERATE_SETTINGS` |

Takes about a minute end-to-end (there are deliberate pauses between phases so the monitor's sliding window fully clears — the script prints why it's pausing, so it doesn't look like it's hung).

A saved transcript of a clean run is at [`demo_runs/demo_run_2026-09-10.log`](demo_runs/demo_run_2026-09-10.log) if you want to show the expected output without waiting live, or as a fallback if something goes wrong during a live run.

---

## 3. Supporting evidence (if there's time / it's asked for)

**Benchmark comparison** (adaptive vs. the two static baselines, across nine workload shapes and several injected network latencies):
```bash
python benchmark/run_benchmark.py --quick
```
`--quick` uses short/low-load params for fast iteration; drop the flag for the full-length version used for the real DA-III numbers (see `docs/RUNNING_GUIDE.md` §2.2 for the exact per-preset numbers). Prints a summary table; raw per-request-type stats land in `results/write_share_eval/`. Run `python benchmark/analyze_results.py` afterward to get the aggregate CSVs and the PNG charts, or run everything (including staleness) from `experiments.ipynb` for inline tables/charts.

**Staleness measurement** (the read-after-write correctness tradeoff, not just speed):
```bash
python -m benchmark.staleness_test --quick
```
Note the `python -m benchmark.staleness_test` form (not `python benchmark/staleness_test.py`) — running it as a script directly breaks the internal package imports. Prints a stale-read rate for four scenarios (static fast/safe, adaptive under hot and cold conditions) and, for the adaptive scenarios, how many trials really ran under the settings each is named for.

---

## 4. Shutting down afterward

```bash
docker compose down       # stop containers, keep the replica set data
docker compose down -v    # stop AND wipe data (only if you want a truly clean slate — re-run rs.initiate() after this)
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `failed to connect to the docker API` | Docker Desktop isn't running — start it, wait ~30–60s |
| `MongoshUnimplementedError: ... unrecognized option: ->` (or similar) from a multi-line `mongosh --eval '...'` command | You're in PowerShell, not bash — PowerShell mangles nested quotes in multi-line single-quoted strings passed to a native executable. Use the single-line PowerShell form given alongside each `--eval` command in this doc (outer `"..."`, inner `'...'`) instead |
| `mongosh` command says `MongoServerError: not primary` | The replica set re-elected primary onto a different node (normal, happens after restarts) — retry the same command against `mongo2` or `mongo3` |
| `pymongo.errors.ServerSelectionTimeoutError` from Python | Usually means the hosts file entries (`mongo1`/`mongo2`/`mongo3` → `127.0.0.1`) are missing — see Section 0, step 2 |
| `ModuleNotFoundError: No module named 'adaptive_layer'` | You ran a `benchmark/` script directly instead of as a module — use `python -m benchmark.staleness_test`, not `python benchmark/staleness_test.py` |
| Demo/benchmark leaves test data behind | Harmless — everything writes to its own throwaway collection/database and cleans up after itself in normal completion; if a run was killed mid-way, `docker exec mongo1 mongosh --port 27017 --eval 'db.getSiblingDB("<db_name>").dropDatabase()'` clears it (PowerShell: `docker exec mongo1 mongosh --port 27017 --eval "db.getSiblingDB('<db_name>').dropDatabase()"` — note the swapped quotes) (try `mongo2`/`mongo3` if you get "not primary") |
