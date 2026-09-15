"""Live showcase dashboard: Flask backend.

Runs the real stack (AdaptiveClient + AccessPatternMonitor + FeedbackLoop,
all against the actual replica set from Phase 1) plus a controllable
TrafficGenerator, and exposes it over a small JSON API the frontend polls.

Run with:  python -m dashboard.app
Then open: http://localhost:8080
"""

import threading
import time

from flask import Flask, jsonify, request, send_from_directory
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from adaptive_layer.client import AdaptiveClient
from adaptive_layer.decision import HIGH_WRITE_THRESHOLD, LOW_WRITE_THRESHOLD, READ_HEAVY_RATIO_THRESHOLD
from adaptive_layer.feedback import FeedbackLoop
from adaptive_layer.monitor import AccessPatternMonitor
from dashboard.recording_monitor import RecordingMonitor
from dashboard.traffic_generator import TrafficGenerator

URI = "mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0"
DB_NAME = "dashboard_demo"
COLLECTION = "live"

WINDOW_SECONDS = 10
INTERVAL_SECONDS = 3
COOLDOWN_SECONDS = 5

NODES = [
    {"name": "mongo1", "port": 27017},
    {"name": "mongo2", "port": 27018},
    {"name": "mongo3", "port": 27019},
]

app = Flask(__name__, static_folder="static", static_url_path="")

_real_monitor = AccessPatternMonitor(window_seconds=WINDOW_SECONDS)
monitor = RecordingMonitor(_real_monitor)
client = AdaptiveClient(URI, DB_NAME, monitor=monitor)
feedback_loop = FeedbackLoop(client, monitor, interval_seconds=INTERVAL_SECONDS, cooldown_seconds=COOLDOWN_SECONDS)
traffic = TrafficGenerator(client, COLLECTION)

# One direct connection per node (not through the replica-set URI) so each
# node answers about itself specifically - a replica-set-aware connection
# would just transparently route everything to the primary, hiding exactly
# the per-node differences the dashboard wants to show.
_node_clients = {
    n["name"]: MongoClient(
        f"mongodb://{n['name']}:{n['port']}/?directConnection=true&serverSelectionTimeoutMS=1500"
    )
    for n in NODES
}

# serverStatus().opcounters is cumulative since the mongod process started -
# MongoDB has no "reset counters" command. To let a demo start from a clean
# "0" without restarting any container (which would also force a fresh
# primary election), the dashboard keeps its own baseline snapshot per node
# and displays (raw - baseline) instead of the raw cumulative value.
# Captured once at startup, and again whenever /api/reset is called.
_opcounter_baseline = {}
_baseline_lock = threading.Lock()

_OPCOUNTER_FIELDS = ("insert", "query", "getmore")


def _raw_opcounters(name):
    """Raw (unadjusted) opcounters for one node, or None if unreachable."""
    c = _node_clients[name]
    try:
        status = c.admin.command("serverStatus")
    except PyMongoError:
        return None
    oc = status.get("opcounters", {})
    return {field: oc.get(field, 0) for field in _OPCOUNTER_FIELDS}


def _capture_baseline():
    with _baseline_lock:
        for n in NODES:
            raw = _raw_opcounters(n["name"])
            if raw is not None:
                _opcounter_baseline[n["name"]] = raw


def _poll_node(name):
    c = _node_clients[name]
    try:
        hello = c.admin.command("hello")
        raw = _raw_opcounters(name)
        if raw is None:
            raise PyMongoError("serverStatus failed")
        with _baseline_lock:
            baseline = _opcounter_baseline.get(name, {field: 0 for field in _OPCOUNTER_FIELDS})
        # clamp at 0: guards against a node restarting (its own counters
        # reset to 0) while our baseline still holds a higher pre-restart value
        adjusted = {field: max(0, raw[field] - baseline.get(field, 0)) for field in _OPCOUNTER_FIELDS}
        return {
            "name": name,
            "reachable": True,
            "role": "PRIMARY" if hello.get("isWritablePrimary") else "SECONDARY",
            "opcounters": adjusted,
        }
    except PyMongoError as exc:
        return {"name": name, "reachable": False, "role": "UNREACHABLE", "error": str(exc)}


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/config")
def api_config():
    return jsonify({
        "high_write_threshold": HIGH_WRITE_THRESHOLD,
        "low_write_threshold": LOW_WRITE_THRESHOLD,
        "read_heavy_ratio_threshold": READ_HEAVY_RATIO_THRESHOLD,
        "collection": COLLECTION,
        "window_seconds": WINDOW_SECONDS,
        "interval_seconds": INTERVAL_SECONDS,
        "cooldown_seconds": COOLDOWN_SECONDS,
    })


@app.route("/api/status")
def api_status():
    stats = monitor.get_stats(COLLECTION)
    settings = client.get_active_settings(COLLECTION)
    now = time.time()
    return jsonify({
        "now": now,
        "monitor_stats": {
            "read_count": stats.read_count,
            "write_count": stats.write_count,
            "read_write_ratio": None if stats.read_write_ratio == float("inf") else stats.read_write_ratio,
            "read_write_ratio_is_infinite": stats.read_write_ratio == float("inf"),
            "write_freq": stats.write_freq,
            "seconds_since_last_write": stats.seconds_since_last_write,
        },
        "active_settings": {
            "write_concern": settings.write_concern,
            "read_concern": settings.read_concern,
            "read_preference": settings.read_preference,
        },
        "traffic": traffic.snapshot(),
        "nodes": [_poll_node(n["name"]) for n in NODES],
        "recent_ops": monitor.recent_ops(),
    })


@app.route("/api/traffic/start", methods=["POST"])
def api_traffic_start():
    body = request.get_json(silent=True) or {}
    traffic.configure(write_ratio=body.get("write_ratio"), ops_per_sec=body.get("ops_per_sec"))
    traffic.start()
    return jsonify(traffic.snapshot())


@app.route("/api/traffic/stop", methods=["POST"])
def api_traffic_stop():
    traffic.stop()
    return jsonify(traffic.snapshot())


@app.route("/api/traffic/configure", methods=["POST"])
def api_traffic_configure():
    body = request.get_json(silent=True) or {}
    traffic.configure(write_ratio=body.get("write_ratio"), ops_per_sec=body.get("ops_per_sec"))
    return jsonify(traffic.snapshot())


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Re-baseline the per-node opcounter display and the traffic op
    counter back to 0. Does NOT touch the Monitor/Decision Engine/active
    settings - those reflect real, current system state, not just a
    display convenience, so resetting them here would misrepresent what
    the system actually observed. This only resets what's cosmetic."""
    _capture_baseline()
    traffic.reset_count()
    return jsonify({"reset": True, "nodes": [_poll_node(n["name"]) for n in NODES], "traffic": traffic.snapshot()})


def main():
    feedback_loop.start()
    _capture_baseline()
    try:
        app.run(host="0.0.0.0", port=8080, threaded=True)
    finally:
        traffic.stop()
        feedback_loop.stop()
        client.close()
        for c in _node_clients.values():
            c.close()


if __name__ == "__main__":
    main()
