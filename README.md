# Adaptive Consistency Layer for MongoDB

A workload-aware Python middleware that watches each collection's traffic and tunes MongoDB's
write concern, read concern and read preference while the application runs, instead of fixing
them once for the whole application. A live dashboard lets you change the traffic and watch the
layer react.

NoSQL Databases (BCSE406L), VIT — DA team project.

## How it works

The layer chooses between three settings per collection:

| Setting | Write concern | Read concern | Read preference | For |
|---|---|---|---|---|
| FAST | `w: 1` | `local` | `nearest` | read-heavy traffic: reads from the closest node |
| MODERATE | `w: 1` | `local` | `primaryPreferred` | mixed traffic |
| SAFE | `w: majority` | `majority` | `primary` | write-heavy traffic: durable writes |

Every few seconds a feedback loop looks at the last 15 s of traffic and decides:

1. **Traffic rule:** write share above 40% → SAFE, below 15% → FAST, otherwise MODERATE.
2. **Hysteresis:** once in SAFE it leaves only below 35% (FAST only above 20%), so a share near a
   threshold doesn't flip the setting back and forth. A cooldown also spaces switches out.
3. **Latency budget:** if SAFE's write p95 goes over 100 ms (for example with a far primary), it
   steps down to MODERATE and doesn't try SAFE again for 60 s.
4. **Per-collection policy:** an optional floor or ceiling (for example "never weaker than SAFE"),
   applied last.

The client also sends reads of keys it wrote in the last 5 s to the primary (read-your-own-writes),
so FAST's local reads don't return stale copies of the application's own writes. Every switch
records the reason it happened.

## Project structure

```
adaptive_layer/   the middleware
  client.py         AdaptiveClient: write()/read() with the collection's active settings
  monitor.py        sliding-window access statistics per collection
  decision.py       the three settings and the traffic rule (with hysteresis)
  rules.py          latency budget and per-collection policy on top of the traffic rule
  feedback.py       the background loop that decides and switches, with a cooldown
dashboard/        web dashboard (Live demo and Evaluation tabs)
  server.py         FastAPI server: engine and evaluation start/stop, knobs, presets, WebSocket updates
  engine.py         the live engine: traffic generator, feedback loop, one-second samples
  evaluation.py     the full evaluation run from the page: plan, estimate, progress, live run data
  presets.py        one-click scenarios
  static/           the page (HTML, CSS, JS with uPlot charts)
testbed/          control of the Docker replica set
  cluster.py        pin mongo1 as primary, node states
  netem.py          inject network delay between nodes (tc netem)
  topology.py       named topologies: EQUAL-<ms>, T1 (far primary), T2 (regional pair)
benchmark/        offline evaluation
  locustfile.py     Locust workload (10 traffic presets) with staleness probes and a switch log
  run_benchmark.py  run every configuration × preset on a latency level or topology
  staleness_test.py isolated read-after-write staleness experiment
  signals_check.py  live check of each decision rule (~2 min)
  signals_eval.py   the full evaluation in one command
  signals_report.py summary table of a run
  probe_log.py, switch_log.py   helpers for the probe tallies and switch logs
docker/, docker-compose.yml     3-node replica set (mongo:7.0 + iproute2 for tc)
```

## Setup (once)

1. Python environment:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Hosts file entries (Windows: `C:\Windows\System32\drivers\etc\hosts`, as Administrator), so
   the same `mongoN:port` addresses work from the host and inside Docker:

   ```
   127.0.0.1 mongo1
   127.0.0.1 mongo2
   127.0.0.1 mongo3
   ```

3. Build and start the containers, then initiate the replica set (only on fresh volumes):

   ```bash
   docker compose up -d --build
   ```

   ```powershell
   docker exec mongo1 mongosh --port 27017 --quiet --eval "rs.initiate({ _id: 'rs0', members: [ { _id: 0, host: 'mongo1:27017' }, { _id: 1, host: 'mongo2:27018' }, { _id: 2, host: 'mongo3:27019' } ] })"
   ```

After that, `docker compose up -d` is all that's needed after a restart (the data and replica set
config live in Docker volumes). Connection string:
`mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0`.

## Dashboard

```bash
.venv\Scripts\python -m dashboard.server
```

Open http://localhost:8000. It has two tabs; only one of them uses the cluster at a time.

### Live demo tab

Press **Start engine** (it pins mongo1 as primary, then starts the traffic). Then:

- **Knobs:** target rate, write share, hot keys, key space, probe fraction, read-your-own-writes,
  topology, pause.
- **Scenarios:** one click sets a whole scenario: read-heavy (FAST), modest writes (MODERATE),
  balanced (SAFE), hysteresis (press Balanced first), far primary, staleness, read-your-own-writes
  back on, latency budget, hot keys, regional pair, back to start. Hover a button for what to expect.
- **Charts:** active setting over time, write share against the decision bands, throughput, read
  and write latency (p50/p95/p99), stale-probe rate, latency by setting, the topology map with the
  read and write paths, the latency budget, and a log of every switch with its reason.

Press **Stop engine** before closing: it clears the injected network delays. `Ctrl+C` in the server
terminal does the same.

### Evaluation tab

Runs the full evaluation: every fixed setting (static FAST, MODERATE, SAFE) and the adaptive layer,
on each chosen network setup and workload, then the staleness experiment.

- **Plan:** start from a profile (SMOKE ~14 min, FULL ~3 h 45 min, FULL_ALL ~6 h 10 min), then tune the
  setups, configurations, workloads, run length (scale of the full length, or fixed seconds per
  run), repeats, the live rule checks, and the staleness setups and trials. The estimated time and
  finish time update as you change it.
- **While it runs:** phase, runs done, elapsed and remaining time (following the real pace), live
  throughput and read/write latency of the run in progress, preflight and rule-check results, and
  the results of every finished run: grouped bars per setup (read/write p50/p95, stale %), the
  adaptive layer's time in FAST / MODERATE / SAFE, and a table. At the end: integrity checks,
  expectations and the figures.
- **Stop** ends the run in progress without counting it; **Resume** an earlier run from the list to
  run only what is left, or **View** it to see its results. Everything is kept in
  `results/signals_eval/<tag>/`.

## Evaluation from the command line

```bash
.venv\Scripts\python -m benchmark.signals_check                      # live check of each rule (~2 min)
.venv\Scripts\python -m benchmark.signals_eval --profile SMOKE       # quick pass of the whole pipeline (~14 min)
.venv\Scripts\python -m benchmark.signals_eval                       # full evaluation (~3 h 45 min)
.venv\Scripts\python -m benchmark.signals_eval --tag <tag>           # resume an interrupted run
```

The full evaluation compares static FAST / MODERATE / SAFE with the adaptive layer (config name
`adaptive_signals`) on equal distances, T1 and T2, across six workloads, plus the staleness
experiment. Results, tables and figures go to `results/signals_eval/<tag>/`.

## Tearing down

```bash
docker compose down        # stop containers, keep the data
docker compose down -v     # also wipe the data (initiate the replica set again afterwards)
```
