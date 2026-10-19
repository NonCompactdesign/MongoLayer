# Adaptive Consistency Layer for MongoDB

A workload-aware Python middleware that monitors per-collection MongoDB access patterns and dynamically tunes write concern / read concern / read preference instead of leaving them fixed for the whole application.

NoSQL Databases (BCSE406L), VIT — DA team project.

**Start here:** [`plan/exec_plan.md`](plan/exec_plan.md) — the full phase-by-phase build plan, from environment setup through the DA-III final report.

**Running a live demo/showcase?** See [`docs/SHOWCASE.md`](docs/SHOWCASE.md) for the exact startup sequence, what to run, and troubleshooting.

**More than the traffic ratio.** The layer proposes a setting from the write share, with hysteresis so it doesn't flip on a threshold, then applies a latency budget for SAFE and a per-collection floor/ceiling. The client routes reads of its own recent writes to the primary, and every switch records why it happened. The test bed can put the client next to one replica and far from the primary. **Final report (journey, additions, results, analysis): [`docs/FINAL_REPORT.md`](docs/FINAL_REPORT.md).** See [`docs/SIGNAL_RULES.md`](docs/SIGNAL_RULES.md) for the eight additions in detail. Full reference: [`docs/PROJECT_DOCUMENTATION.md`](docs/PROJECT_DOCUMENTATION.md).

## Quickstart

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash; use .venv\Scripts\activate.bat on cmd
pip install -r requirements.txt
```

A 3-node MongoDB replica set (via Docker Compose) is required before any of the adaptive logic is meaningful — see [`docs/replica_set_setup.md`](docs/replica_set_setup.md). Then:

```bash
python -m pytest -q                     # 112 tests
python -m benchmark.signals_check       # live check of every addition (~2 min)
```

The whole evaluation (~4 h 35 min, resumable, results shown in the notebook): open `signals_evaluation.ipynb` and Run All, or

```powershell
.venv\Scripts\python -m benchmark.run_notebook signals_evaluation.ipynb
```
