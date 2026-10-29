"""The full evaluation, run from the dashboard's Evaluation tab.

The same phases as `python -m benchmark.signals_eval` (preflight, live checks, load tests,
staleness, analysis), in a background thread, with what the page needs on top: a plan built from
the form, a time estimate, progress and a remaining-time estimate that follows the real pace, live
numbers of the Locust run in progress (from its _stats_history.csv), and the results of every
finished run as they come in. Stop ends the run in progress without marking it done, so a resume
(same tag) runs it again; finished runs are always kept.
"""

import collections
import csv
import datetime as dt
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path

from benchmark import run_benchmark as rb
from benchmark import signals_eval as ev
from benchmark import signals_report
from testbed import topology

SETUPS = ["EQUAL-0", "EQUAL-5", "T1", "T2"]
LIVE_POINTS = 600  # current-run history kept for the charts (one per second)


def plan_from(params):
    """A Plan from the page's form (missing fields keep the Plan defaults)."""
    fields = {"setups", "configs", "presets", "duration_scale", "fixed_duration_s", "repeats", "run_checks",
              "staleness_setups", "staleness_trials"}
    kwargs = {k: v for k, v in (params or {}).items() if k in fields and v is not None}
    if "fixed_duration_s" in kwargs and not kwargs["fixed_duration_s"]:
        kwargs["fixed_duration_s"] = None
    for k in ("repeats", "staleness_trials", "fixed_duration_s"):
        if kwargs.get(k) is not None:
            kwargs[k] = int(kwargs[k])
    if "duration_scale" in kwargs:
        kwargs["duration_scale"] = float(kwargs["duration_scale"])
    plan = ev.Plan(**kwargs)
    if not plan.setups or not plan.configs or not plan.presets:
        raise ValueError("choose at least one setup, one configuration and one workload")
    if plan.staleness_trials < 1:
        raise ValueError("staleness trials must be at least 1")
    return plan


def options():
    """What the form offers: setups, configurations, workloads (with their load shape), profiles."""
    return {
        "setups": [{"name": s, "description": topology.by_name(s).description} for s in SETUPS],
        "configs": list(rb.CONFIGS),
        "presets": [{"name": p, **rb.params_for(p, quick=False)} for p in rb.PRESETS],
        "core_presets": list(ev.CORE_PRESETS),
        "profiles": {name: asdict(ev.Plan(**kw)) for name, kw in ev.PROFILES.items()},
    }


def estimate(plan):
    rows, total = ev.estimate(plan)
    return {"rows": [{"phase": n, "seconds": round(s)} for n, s in rows], "total": round(total),
            "finish": (dt.datetime.now() + dt.timedelta(seconds=total)).strftime("%a %H:%M"),
            "runs": len(plan.cells()),
            "lengths": {p: ev._seconds(plan.params(p)["duration"]) for p in plan.presets}}


def past_runs():
    """Earlier runs on disk, newest first: tag, profile, start time, runs done / planned."""
    out = []
    for meta_path in sorted(ev.EVAL_ROOT.glob("*/run_meta.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            meta = json.loads(meta_path.read_text())
            plan = ev.Plan(**meta["plan"])
        except (ValueError, KeyError, TypeError):
            continue
        folder = meta_path.parent
        done = sum(ev.is_done(folder, *c) for c in plan.cells())
        out.append({"tag": folder.name, "profile": meta.get("profile"), "started": meta.get("started"),
                    "finished": meta.get("finished"), "done": done, "planned": len(plan.cells())})
    return out


def _live_history(prefix, started):
    """Points of the Locust run in progress: [{t, users, rps, p50, p95, read_*, write_*}]."""
    path = Path(f"{prefix}_stats_history.csv")
    if not path.exists():
        return []
    points = collections.OrderedDict()
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                t = int(r["Timestamp"])
                if t < started - 1:
                    continue  # an earlier attempt's rows
                p = points.setdefault(t, {"t": t})
                name = {"Aggregated": "", "read": "read_", "write": "write_"}.get(r["Name"])
                if name is None:
                    continue
                num = lambda col: float(r[col]) if r.get(col) not in (None, "", "N/A") else None  # noqa: E731
                p[f"{name}rps"] = num("Requests/s")
                p[f"{name}p50"] = num("50%")
                p[f"{name}p95"] = num("95%")
                if not name:
                    p["users"] = num("User Count")
    except (OSError, ValueError, KeyError):
        return []
    return list(points.values())[-LIVE_POINTS:]


class _PageLog(ev.Log):
    """run_log.txt as before, and the lines `say` prints kept for the page."""

    def __init__(self, path, lines):
        super().__init__(path)
        self.lines = lines

    def say(self, text):
        super().say(text)
        self.lines.append(f"[{time.strftime('%H:%M:%S')}] {text}")


class EvalRunner:
    def __init__(self, is_busy=lambda: False):
        self._is_busy = is_busy  # the live engine using the cluster
        self.status = "idle"  # idle | running | stopping | finished | stopped | failed
        self.error = None
        self.tag = self.out = self.plan = None
        self.phase = None
        self.preflight = []
        self.current = None  # {cell, params, started, prefix}
        self.cell_seconds = {}  # cell -> (planned s, actual s) for the pace
        self.started = self.ended = None
        self.analysis = None
        self.lines = collections.deque(maxlen=300)
        self.thread = None
        self._stop = threading.Event()
        self._results_cache = (None, [])
        self._lock = threading.Lock()

    @property
    def running(self):
        return self.status in ("running", "stopping")

    # control

    def start(self, params, tag=None):
        with self._lock:
            if self.running:
                raise RuntimeError("an evaluation is already running")
            if self._is_busy():
                raise RuntimeError("the live engine is running: stop it first (both use the cluster)")
            if tag:
                meta = json.loads((ev.out_dir_for(tag) / "run_meta.json").read_text())
                plan, profile = ev.Plan(**meta["plan"]), meta.get("profile", "CUSTOM")
            else:
                plan, profile = plan_from(params), (params or {}).get("profile") or "CUSTOM"
                tag = f"{profile}_{dt.datetime.now():%Y-%m-%d_%H%M%S}"
            self._open(tag, plan)
            self.status, self.error, self.analysis = "running", None, None
            self.started, self.ended = time.time(), None
            self.preflight, self.cell_seconds, self.current = [], {}, None
            self.lines.clear()
            self._stop.clear()
            self.thread = threading.Thread(target=self._run, args=(profile,), daemon=True, name="evaluation")
            self.thread.start()

    def stop(self):
        if self.running:
            self.status = "stopping"
            self._stop.set()
            rb.stop_current()

    def view(self, tag):
        """Show an earlier run's results (no running)."""
        with self._lock:
            if self.running:
                raise RuntimeError("an evaluation is running")
            meta = json.loads((ev.out_dir_for(tag) / "run_meta.json").read_text())
            self._open(tag, ev.Plan(**meta["plan"]))
            self.status, self.error, self.phase, self.current = "idle", None, None, None
            self.started = self.ended = None
            self.preflight = json.loads((self.out / "preflight.json").read_text()) if (self.out / "preflight.json").exists() else []
            self.analysis = self._analysis_view() if (self.out / "summary.csv").exists() else None

    def _open(self, tag, plan):
        self.tag, self.plan, self.out = tag, plan, ev.out_dir_for(tag)
        self.out.mkdir(parents=True, exist_ok=True)
        self._results_cache = (None, [])

    # the run

    def _run(self, profile):
        plan, out, stop = self.plan, self.out, self._stop.is_set
        log = _PageLog(out / "run_log.txt", self.lines)
        try:
            ev.write_meta(out, plan, tag=self.tag, profile=profile,
                          started=dt.datetime.now().isoformat(timespec="seconds"))
            log.say(f"run {self.tag}: {len(plan.cells())} load-test runs planned")
            self.phase = "preflight"
            t0 = time.monotonic()
            self.preflight = [{"check": n, "ok": ok, "detail": d} for n, ok, d in ev.preflight(out)]
            ev.write_meta(out, plan, phase_seconds={"preflight": round(time.monotonic() - t0)})
            if not all(p["ok"] for p in self.preflight):
                raise RuntimeError("preflight failed: " + "; ".join(p["check"] for p in self.preflight if not p["ok"]))
            if plan.run_checks and not stop():
                self.phase = "live checks"
                t0 = time.monotonic()
                checks = ev.run_live_checks(out, log, should_stop=stop)
                ev.write_meta(out, plan, phase_seconds={"live_checks": round(time.monotonic() - t0)})
                if checks and not all(v["pass"] for v in checks.values()) and not stop():
                    log.say("a live check failed: the load tests run anyway; see the checks list")
            if not stop():
                self.phase = "load tests"
                t0 = time.monotonic()
                ev.run_load_tests(out, plan, log, should_stop=stop, progress=self._progress)
                ev.write_meta(out, plan, phase_seconds={"load_tests": round(time.monotonic() - t0)})
            if not stop():
                self.phase = "staleness"
                t0 = time.monotonic()
                ev.run_staleness(out, plan, log, should_stop=stop)
                ev.write_meta(out, plan, phase_seconds={"staleness": round(time.monotonic() - t0)},
                              finished=dt.datetime.now().isoformat(timespec="seconds"))
            self.phase = "analysis"
            res = ev.analyse(out, plan)
            ev.write_figures(out, res)
            ev.write_meta(out, plan, analysed=dt.datetime.now().isoformat(timespec="seconds"))
            self.analysis = self._analysis_view(res)
            self.status = "stopped" if stop() else "finished"
            log.say("stopped: finished runs are kept, resume to run the rest" if stop() else "evaluation finished")
        except Exception as e:  # noqa: BLE001 - shown on the page
            self.error = f"{type(e).__name__}: {e}"
            self.status = "failed"
            log.say(f"failed: {self.error}")
        finally:
            self.current = None
            self.phase = None
            self.ended = time.time()

    def _progress(self, event, cell, info):
        if event == "start":
            self.current = {"cell": cell, "params": info, "started": time.time(),
                            "prefix": str(ev.cell_prefix(self.out, *cell))}
        elif self.current and self.current["cell"] == cell:
            planned = ev._seconds(self.current["params"]["duration"]) + ev.RUN_OVERHEAD_S
            if info is not None:
                self.cell_seconds[cell] = (planned, time.time() - self.current["started"])
            self.current = None

    # what the page shows

    def _analysis_view(self, res=None):
        res = res or ev.analyse(self.out, self.plan)
        st = res["staleness"]
        return {
            "integrity": [{"item": n, "ok": ok, "detail": d} for n, ok, d in res["integrity"]],
            "expectations": [{"item": n, "ok": v, "detail": d} for n, v, d in res["expectations"]],
            "staleness": [] if st.empty else json.loads(st.to_json(orient="records")),
            "figures": sorted(p.name for p in (self.out / "figs").glob("*.png")) if (self.out / "figs").exists() else [],
        }

    def _results(self, done):
        """signals_report rows for the finished runs (recomputed only when a run finishes)."""
        key, rows = self._results_cache
        if key != done:
            rows = signals_report.summarise(self.out)
            self._results_cache = (done, rows)
        return rows

    def _remaining(self, cells, done_cells):
        """Seconds left: the plan's estimate for what is left, scaled by the pace so far."""
        plan = self.plan
        planned_left = 0.0
        for c in cells:
            if c in done_cells:
                continue
            length = ev._seconds(plan.params(c[3])["duration"]) + ev.RUN_OVERHEAD_S
            if self.current and self.current["cell"] == c:
                length = max(0.0, length - (time.time() - self.current["started"]))
            planned_left += length
        pairs = list(self.cell_seconds.values())
        pace = sum(a for _, a in pairs) / sum(p for p, _ in pairs) if pairs else 1.0
        pace = min(2.0, max(0.8, pace))
        setups_left = {c[0] for c in cells if c not in done_cells}
        left = planned_left * pace + ev.SETUP_OVERHEAD_S * len(setups_left)
        checks_done = (self.out / "checks.json").exists() and len(json.loads((self.out / "checks.json").read_text())) >= 5
        if plan.run_checks and not checks_done:
            left += ev.CHECKS_S
        for setup in plan.staleness_setups:
            if not (self.out / f"staleness_{topology.by_name(setup).name}.csv").exists():
                left += ev.STALENESS_S.get(setup, 300.0) * max(min(1.0, plan.staleness_trials / 200), 0.5) + ev.SETUP_OVERHEAD_S
        return left

    def state(self):
        base = {"status": self.status, "error": self.error, "tag": self.tag, "phase": self.phase,
                "lines": list(self.lines)[-120:]}
        if self.plan is None:
            return base
        cells = [tuple(c) for c in self.plan.cells()]
        done_cells = {c for c in cells if ev.is_done(self.out, *c)}
        checks_path = self.out / "checks.json"
        checks = json.loads(checks_path.read_text()) if checks_path.exists() else {}
        cur = self.current
        current = None
        if cur:
            current = {"setup": topology.by_name(cur["cell"][0]).name, "rep": cur["cell"][1], "config": cur["cell"][2],
                       "preset": cur["cell"][3], "users": cur["params"]["users"],
                       "length": ev._seconds(cur["params"]["duration"]), "elapsed": time.time() - cur["started"],
                       "points": _live_history(cur["prefix"], cur["started"])}
        remaining = self._remaining(cells, done_cells) if self.running else 0
        now = time.time()
        return {
            **base,
            "plan": asdict(self.plan),
            "runs_total": len(cells), "runs_done": len(done_cells),
            "elapsed": (self.ended or now) - self.started if self.started else None,
            "remaining": remaining,
            "finish": (dt.datetime.now() + dt.timedelta(seconds=remaining)).strftime("%H:%M") if self.running else None,
            "preflight": self.preflight,
            "checks": [{"name": k, "pass": v.get("pass"), "seconds": v.get("seconds")} for k, v in checks.items()],
            "current": current,
            "results": self._results(len(done_cells)),
            "staleness_done": [s for s in self.plan.staleness_setups
                               if (self.out / f"staleness_{topology.by_name(s).name}.csv").exists()],
            "analysis": self.analysis,
        }
