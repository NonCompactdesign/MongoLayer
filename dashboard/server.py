"""Live dashboard for the adaptive layer: knobs for the traffic, live charts of what the layer
decides and what it costs. The full evaluation runs from the terminal (benchmark/signals_eval.py).

    .venv\\Scripts\\python -m dashboard.server            # then open http://localhost:8000
    .venv\\Scripts\\python -m dashboard.server --port 8050

The engine (dashboard/engine.py) runs in this process, started and stopped from the page. The
knobs live here too, so they keep their values while the engine is stopped and a new engine
starts with them. The page gets a snapshot every second over the /ws WebSocket.
"""

import argparse
import asyncio
import contextlib
import csv
import io
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from dashboard import presets
from dashboard.engine import Engine, Knobs

STATIC = Path(__file__).resolve().parent / "static"
TOPOLOGIES = ["EQUAL-0", "EQUAL-5", "T1", "T2"]


class Runner:
    """Owns the one Engine and the thread that steps it once a second."""

    def __init__(self):
        self.engine = None
        self.thread = None
        self.status = "stopped"  # stopped | starting | running | stopping
        self.message = ""
        self.error = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self.status != "stopped":
                raise RuntimeError(f"engine is {self.status}")
            self.engine = Engine(knobs)
            self.status, self.error, self.message = "starting", None, "Starting ..."
            self.thread = threading.Thread(target=self._run, args=(self.engine,), daemon=True, name="engine")
            self.thread.start()

    def stop(self):
        if self.engine is not None and self.status in ("starting", "running"):
            self.engine.stop_requested = True  # a stop during setup takes effect once setup is done

    def _log(self, text):
        self.message = text

    def _run(self, engine):
        set_up = False
        try:
            engine.setup(log=self._log)
            set_up = True
            self.status, self.message = "running", "Running"
            next_step = time.monotonic()
            while not engine.stop_requested:
                next_step += 1.0
                time.sleep(max(0.0, next_step - time.monotonic()))
                engine.step()
        except Exception as e:  # noqa: BLE001 - shown on the page
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.status = "stopping"
            try:
                engine.shutdown(log=self._log)
            except Exception as e:  # noqa: BLE001
                self.error = self.error or f"{type(e).__name__} while stopping: {e}"
            self.status = "stopped"
            self.message = f"Stopped after {engine.switch_count} switches." if set_up else "Stopped."

    def state(self):
        return {"status": self.status, "message": self.message, "error": self.error}


knobs = Knobs()
runner = Runner()


@contextlib.asynccontextmanager
async def lifespan(app):
    yield
    runner.stop()
    if runner.thread is not None:
        await asyncio.to_thread(runner.thread.join, 30)


app = FastAPI(title="Adaptive consistency dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_stale_page(request, call_next):
    """The page and its scripts are revalidated on every load, so an updated dashboard is never
    mixed with a cached older app.js or styles.css."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def _payload(full_history=False, since=None):
    e = runner.engine
    history = list(e.history) if e else []
    if not full_history:
        history = [p for p in history if since is None or p["t"] > since]
    return {"engine": runner.state(), "control": knobs.snapshot(), "snapshot": e.latest if e else None,
            "history": history}


@app.get("/api/state")
def get_state():
    return {**_payload(full_history=True), "presets": presets.as_list(), "topologies": TOPOLOGIES}


@app.post("/api/control")
def post_control(changes: dict):
    try:
        return knobs.update(**changes)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/preset/{name}")
def post_preset(name: str):
    if name not in presets.PRESETS:
        raise HTTPException(404, f"unknown preset {name!r}")
    return knobs.update(**presets.PRESETS[name][2])


@app.post("/api/engine/start")
def start_engine():
    try:
        runner.start()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return runner.state()


@app.post("/api/engine/stop")
def stop_engine():
    runner.stop()
    return runner.state()


@app.get("/api/export.csv")
def export_csv():
    rows = list(runner.engine.history) if runner.engine else []
    out = io.StringIO()
    if rows:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    name = time.strftime("live_dashboard_%Y%m%d_%H%M%S.csv")
    return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


@app.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    try:
        payload = _payload(full_history=True)
        await socket.send_json({"type": "init", **payload})
        last = payload["history"][-1]["t"] if payload["history"] else None
        engine = runner.engine
        while True:
            await asyncio.sleep(1.0)
            if runner.engine is not engine:  # a new engine started: the page starts its charts over
                engine = runner.engine
                payload = _payload(full_history=True)
                await socket.send_json({"type": "init", **payload})
            else:
                payload = _payload(since=last)
                await socket.send_json({"type": "tick", **payload})
            if payload["history"]:
                last = payload["history"][-1]["t"]
    except (WebSocketDisconnect, RuntimeError):
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Live dashboard (see the module docstring).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    print(f"Dashboard on http://localhost:{args.port}  (Ctrl+C stops it and the engine)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
