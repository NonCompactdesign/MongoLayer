# Adaptive Consistency Layer for MongoDB

A workload-aware Python middleware that monitors per-collection MongoDB access patterns and dynamically tunes write concern / read concern / read preference instead of leaving them fixed for the whole application.

NoSQL Databases (BCSE406L), VIT — DA team project.

**Start here:** [`plan/exec_plan.md`](plan/exec_plan.md) — the full phase-by-phase build plan, from environment setup through the DA-III final report.

## Quickstart

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash; use .venv\Scripts\activate.bat on cmd
pip install -r requirements.txt
```

A 3-node MongoDB replica set (via Docker Compose) is required before any of the adaptive logic is meaningful — see Phase 1 in the execution plan.
