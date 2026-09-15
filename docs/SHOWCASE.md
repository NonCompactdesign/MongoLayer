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
   Should print `34 passed`. If it doesn't, fix that before the showcase — don't discover a broken build live.

---

## 2. The main event: the live dashboard (recommended for a showcase/presentation)

```bash
python -m dashboard.app
```

Then open **http://localhost:8080**. This is a purpose-built live dashboard for exactly this project — nothing off-the-shelf shows an app's own consistency-tuning logic alongside the replica set's real behavior, so it's custom. It has:

- **Traffic controls** — a read:write ratio slider, a rate slider, Start/Stop, and three one-click presets (Read-Heavy, Write-Heavy, Balanced). Adjust these live and watch the system react in real time — no scripted phases, no waiting for a fixed sequence.
- **Active Consistency Settings** — the three live settings, with a blue flash the instant any of them changes (so a viewer's eye is drawn to it automatically), plus a plain-English label ("SAFE — hot/write-heavy pattern detected" etc.).
- **Access Pattern Monitor** — live read/write counts and two threshold bars (write frequency vs. `HIGH_WRITE_THRESHOLD`, read:write ratio vs. `READ_HEAVY_RATIO_THRESHOLD`) so a viewer can see *how close* the system is to switching, not just the switch itself.
- **Replica Set — Live Container Status** — all three containers, live role badges (PRIMARY/SECONDARY, tracks re-elections automatically), and each node's own insert/query/getmore counters ticking up in real time. This is the part that visibly proves writes land on the primary and reads get routed per the active read preference.
- **Live Operation Feed** — a scrolling log of the most recent ops.

**Reset Stats button** — MongoDB has no native "reset counters" command (`serverStatus().opcounters` is cumulative since the mongod process started). This button re-baselines the displayed per-node counters and the traffic op count back to 0 without restarting anything — safe to click right before a demo starts, or between runs, with no risk of disrupting the replica set or forcing a new primary election. It does **not** touch active settings or the Monitor's real observed pattern — those reflect actual system state, not just a display convenience.

**What to say while it's running:** drag the ratio slider toward Write and watch `write_freq` climb the threshold bar and the settings flash to SAFE; drag it back toward Read and hold it there (the FAST branch needs a *sustained* low-write, read-heavy pattern, not just an instant flip) and watch it relax to FAST. Point at the container panel and note that reads only start reaching the secondaries once the read preference actually allows it — under SAFE settings (`read_preference=primary`), the secondary query counters visibly stop moving.

Updates every second via polling — nothing to configure, just refresh the page if it ever shows "connection lost."

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

**Benchmark comparison** (adaptive vs. the two static baselines, across three workload shapes):
```bash
python benchmark/run_benchmark.py --quick
```
Takes about 2 minutes (`--quick` = 5 users/20s per combination; drop the flag for the full 20-user/60s version used for the real DA-III numbers, which takes ~10+ minutes). Prints a summary table; raw per-request-type stats land in `results/*.csv`.

**Staleness measurement** (the read-after-write correctness tradeoff, not just speed):
```bash
python -m benchmark.staleness_test --quick
```
Note the `python -m benchmark.staleness_test` form (not `python benchmark/staleness_test.py`) — running it as a script directly breaks the internal package imports. Prints a stale-read rate for four scenarios (static fast/safe, adaptive under hot and cold conditions) and — the interesting part — explains *why* the adaptive layer's "fast" setting is actually safer in practice than the static fast baseline, not just faster. Worth reading that explanation out loud if asked "so what does this actually prove."

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
| `ModuleNotFoundError: No module named 'adaptive_layer'` | You ran a `benchmark/`/`dashboard/` script directly instead of as a module — use `python -m dashboard.app` / `python -m benchmark.staleness_test`, not `python dashboard/app.py` |
| Dashboard shows "connection lost" | The Flask server crashed or was stopped — check the terminal it's running in for a traceback; a common cause is the replica set going down mid-demo, so check `docker ps` too |
| Dashboard settings never leave MODERATE | Give it a few seconds — the Feedback Loop only re-evaluates every `interval_seconds` (3s by default in the dashboard), and won't switch again within `cooldown_seconds` (5s) of its last switch. The threshold bars tell you how close it is. |
| Demo/benchmark leaves test data behind | Harmless — everything writes to its own throwaway collection/database and cleans up after itself in normal completion; if a run was killed mid-way, `docker exec mongo1 mongosh --port 27017 --eval 'db.getSiblingDB("<db_name>").dropDatabase()'` clears it (try `mongo2`/`mongo3` if you get "not primary") |
