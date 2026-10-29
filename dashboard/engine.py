"""The live engine behind the dashboard: the adaptive layer running against the real replica set,
with traffic whose knobs (Knobs) can change while it runs.

Engine.setup() pins the primary and starts the traffic and the feedback loop; step(), called about
once a second, follows the knobs (applying topology changes), records switches and takes a sample;
shutdown() stops everything and clears the injected delays.

Staleness probes: a share of the traffic (probe_fraction) writes a document and immediately reads
it back by _id; a read that misses it is stale. With read_own_writes on, those reads go to the
primary under FAST, so they are never stale; turn it off to see FAST's real exposure.
"""

import collections
import math
import random
import threading
import time

from adaptive_layer.client import AdaptiveClient
from adaptive_layer.decision import settings_name
from adaptive_layer.feedback import FeedbackLoop
from adaptive_layer.monitor import AccessPatternMonitor
from adaptive_layer.rules import SIGNALS_THRESHOLDS, SignalConfig
from testbed import cluster, netem, topology

URI = "mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0"
DB = "live_demo"
COLLECTION = "orders"

DEFAULT_TRAFFIC = {
    "write_share": 0.05,      # fraction of operations that are writes
    "ops_per_sec": 200,       # target total rate across all worker threads
    "hot_keys": 0,            # 0 = uniform over key_space; N = 80% of operations on N keys
    "key_space": 1000,
    "probe_fraction": 0.03,   # share of iterations that are write-then-read staleness probes
    "read_own_writes": True,  # per-key read-your-own-writes (#6)
    "topology": "EQUAL-0",    # EQUAL-0, EQUAL-5, T1 or T2
    "paused": False,
}

SAMPLE_SECONDS = 5  # each chart point covers the last 5 s (the tiles use the full decision window)
NODES = ("mongo1", "mongo2", "mongo3")
CLIENT_BASE_RTT_MS = 1.0  # host-to-container link without injected delay (see testbed/topology.py)


# --- knobs --------------------------------------------------------------------------------------

class Knobs:
    """The traffic settings, shared by the dashboard (writes) and the engine's threads (read)."""

    def __init__(self):
        self._state = dict(DEFAULT_TRAFFIC)
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def update(self, **changes):
        """Change some settings: update(write_share=0.5, ops_per_sec=300). Unknown keys and
        out-of-range values are rejected. Returns the full new state."""
        unknown = set(changes) - set(DEFAULT_TRAFFIC)
        if unknown:
            raise ValueError(f"unknown setting(s) {sorted(unknown)}; valid: {sorted(DEFAULT_TRAFFIC)}")
        if "write_share" in changes and not 0.0 <= changes["write_share"] <= 1.0:
            raise ValueError("write_share must be between 0 and 1")
        if "probe_fraction" in changes and not 0.0 <= changes["probe_fraction"] < 1.0:
            raise ValueError("probe_fraction must be between 0 and 1")
        if "topology" in changes:
            topology.by_name(changes["topology"])  # raises on an unknown name
        with self._lock:
            self._state.update(changes)
            return dict(self._state)


# --- measurements -------------------------------------------------------------------------------

def percentile(values, q):
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100.0 * len(ordered)) - 1)]


class Window:
    """Thread-safe record of recent operations: (time, kind, latency ms, settings name, stale).
    Also keeps the last few thousand latencies per (settings, read/write) for the whole session,
    so the dashboard can compare what each setting costs (reset_by_setting() on a topology change)."""

    PER_SETTING_SAMPLES = 5000

    def __init__(self, seconds):
        self.seconds = seconds
        self._events = collections.deque()
        self._by_setting = collections.defaultdict(lambda: collections.deque(maxlen=self.PER_SETTING_SAMPLES))
        self._lock = threading.Lock()

    def add(self, kind, latency_ms, settings, stale=None, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._events.append((now, kind, latency_ms, settings, stale))
            cutoff = now - self.seconds
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()
            if kind in ("read", "probe_read", "write"):
                self._by_setting[(settings, "write" if kind == "write" else "read")].append(latency_ms)

    def reset_by_setting(self):
        with self._lock:
            self._by_setting.clear()

    def by_setting(self):
        """{settings name: {"read": {p50, p95, n}, "write": {...}}} over the session so far."""
        with self._lock:
            samples = {k: list(v) for k, v in self._by_setting.items()}
        out = {}
        for (settings, kind), values in samples.items():
            out.setdefault(settings, {})[kind] = {"p50": percentile(values, 50), "p95": percentile(values, 95), "n": len(values)}
        return out

    def summary(self, now=None, seconds=None):
        """Counts and percentiles over the last `seconds` (default: the whole window)."""
        now = time.time() if now is None else now
        seconds = self.seconds if seconds is None else min(seconds, self.seconds)
        with self._lock:
            events = [e for e in self._events if e[0] >= now - seconds]
        reads = [e[2] for e in events if e[1] == "read"]
        writes = [e[2] for e in events if e[1] == "write"]
        probes = [e for e in events if e[1] == "probe_read"]
        stale = sum(1 for e in probes if e[4])
        errors = sum(1 for e in events if e[1] == "error")
        return {
            "ops": len(reads) + len(writes),
            "reads": len(reads), "writes": len(writes), "errors": errors,
            "read_p50": percentile(reads, 50), "read_p95": percentile(reads, 95), "read_p99": percentile(reads, 99),
            "write_p50": percentile(writes, 50), "write_p95": percentile(writes, 95), "write_p99": percentile(writes, 99),
            "probes": len(probes), "stale": stale,
            "stale_pct": round(100.0 * stale / len(probes), 1) if probes else None,
        }


# --- traffic ------------------------------------------------------------------------------------

def plain_write_probability(write_share, probe_fraction):
    """The write probability for non-probe iterations that makes the OVERALL write share (probe
    writes included) equal write_share. A probe is one write and one read, so with probe fraction f
    and plain write probability p: share = (f + (1 - f) p) / (1 + f). The lowest reachable share is
    f / (1 + f) (all plain operations are reads)."""
    f = probe_fraction
    if f >= 1:
        return 0.0
    return min(1.0, max(0.0, (write_share * (1 + f) - f) / (1 - f)))


class Traffic:
    """Worker threads that follow the current knobs (rate, mix, keys, probes)."""

    def __init__(self, client, window, knobs, threads=8):
        self.client, self.window, self.knobs, self.threads = client, window, knobs, threads
        self._stop = threading.Event()
        self._workers = [threading.Thread(target=self._run, daemon=True, name=f"traffic-{i}") for i in range(threads)]

    def start(self):
        for w in self._workers:
            w.start()

    def stop(self):
        self._stop.set()
        for w in self._workers:
            w.join(timeout=10)

    def _key(self, s):
        if s["hot_keys"] and random.random() < 0.8:
            return random.randrange(int(s["hot_keys"]))
        return random.randrange(int(s["key_space"]))

    def _timed(self, kind, fn):
        settings = settings_name(self.client.get_active_settings(COLLECTION))
        start = time.monotonic()
        try:
            result = fn()
        except Exception:  # noqa: BLE001 - counted, the demo keeps running
            self.window.add("error", (time.monotonic() - start) * 1000, settings)
            time.sleep(0.05)
            return None, False
        self.window.add(kind, (time.monotonic() - start) * 1000, settings)
        return result, True

    def _run(self):
        while not self._stop.is_set():
            s = self.knobs.snapshot()
            if s["paused"] or s["ops_per_sec"] <= 0:
                time.sleep(0.2)
                continue
            started = time.monotonic()
            key = self._key(s)
            if random.random() < s["probe_fraction"]:
                res, ok = self._timed("write", lambda: self.client.write(COLLECTION, {"key": key, "ts": time.time()}))
                if ok:
                    settings = settings_name(self.client.get_active_settings(COLLECTION))
                    t0 = time.monotonic()
                    try:
                        doc = self.client.read(COLLECTION, {"_id": res.inserted_id})
                        self.window.add("probe_read", (time.monotonic() - t0) * 1000, settings, stale=doc is None)
                    except Exception:  # noqa: BLE001
                        self.window.add("error", (time.monotonic() - t0) * 1000, settings)
            elif random.random() < plain_write_probability(s["write_share"], s["probe_fraction"]):
                self._timed("write", lambda: self.client.write(COLLECTION, {"key": key, "ts": time.time()}))
            else:
                self._timed("read", lambda: self.client.read(COLLECTION, {"key": key}))
            # pace: each thread does its share of the target rate
            budget = self.threads / max(float(s["ops_per_sec"]), 0.1)
            left = budget - (time.monotonic() - started)
            if left > 0:
                self._stop.wait(left)


# --- engine -------------------------------------------------------------------------------------

def topology_view(name):
    """Round trips between the nodes and from the client, for the dashboard's map."""
    topo = topology.by_name(name)
    links = [{"a": a, "b": b, "rtt": topo.rtt(a, b)} for i, a in enumerate(NODES) for b in NODES[i + 1:]]
    client = {n: CLIENT_BASE_RTT_MS + topo.client_extra(n) for n in NODES}
    return {"name": name, "description": topo.description, "links": links, "client": client, "primary": "mongo1"}


def _settings_view(settings):
    return {"name": settings_name(settings), "write_concern": settings.write_concern,
            "read_concern": settings.read_concern, "read_preference": settings.read_preference}


class Engine:
    """The demo in-process: cluster setup, traffic, the feedback loop, and a sample history.

    What the dashboard reads (latest, history, events) is replaced or appended whole, so another
    thread can read it without a lock. Set stop_requested to end the loop that calls step()."""

    def __init__(self, knobs, window_s=15, interval=5, cooldown=10, threads=40, history_seconds=900):
        self.knobs = knobs
        # 40 threads: enough for 200 ops/s even with ~260 ms SAFE writes on T1
        self.window_s, self.interval, self.cooldown, self.threads = window_s, interval, cooldown, threads
        self.thresholds = SIGNALS_THRESHOLDS
        self.signals = SignalConfig()
        self.history = collections.deque(maxlen=history_seconds)  # one sample per step()
        self.events = collections.deque(maxlen=300)  # traffic changes and switches, oldest first
        self.latest = None  # the latest snapshot
        self.stop_requested = False
        self.client = self.loop = self.traffic = self.window = self.monitor = None
        self.t0 = None
        self._applied = None  # the knobs as of the last step()
        self._seen_switches = 0

    # lifecycle

    def setup(self, log=print):
        log("Pinning the primary (mongo1) and clearing old delays ...")
        netem.clear()
        cluster.pin_primary()
        s = self._applied = self.knobs.snapshot()
        if s["topology"] != "EQUAL-0":
            log(f"Applying topology {s['topology']} ...")
            topology.apply(topology.by_name(s["topology"]))
        self.monitor = AccessPatternMonitor(window_seconds=self.window_s)
        self.client = AdaptiveClient(URI, DB, monitor=self.monitor, verbose=False, key_field="key",
                                     read_own_writes_seconds=self._ryw_seconds(s))
        self.client._db.drop_collection(COLLECTION)
        self.client._db[COLLECTION].create_index("key")
        self.loop = FeedbackLoop(self.client, self.monitor, interval_seconds=self.interval,
                                 cooldown_seconds=self.cooldown, thresholds=self.thresholds, signals=self.signals)
        self.window = Window(self.window_s)
        self.traffic = Traffic(self.client, self.window, self.knobs, threads=self.threads)
        self.t0 = time.time()
        self.loop.start()
        self.traffic.start()
        self.step()  # a first sample, so a viewer has something at once

    def shutdown(self, log=print):
        log("Stopping traffic and the feedback loop, clearing delays ...")
        if self.traffic:
            self.traffic.stop()
        if self.loop:
            self.loop.stop()
        topology.clear()
        if self.client:
            self.client._db.drop_collection(COLLECTION)
            self.client.close()

    @property
    def switch_count(self):
        return len(self.loop.switch_log) if self.loop else 0

    def _ryw_seconds(self, s):
        return 5.0 if s["read_own_writes"] else None

    # once a second

    def step(self):
        """Follow the knobs and take a sample. Returns (changed settings, new switch events)."""
        new = self.knobs.snapshot()
        changed = {k: v for k, v in new.items() if v != self._applied.get(k)}
        self._applied = new
        if changed:
            self.events.append({"t": time.time(), "kind": "traffic",
                                "text": ", ".join(f"{k} = {v}" for k, v in changed.items())})
            if "topology" in changed:
                if new["topology"] == "EQUAL-0":
                    topology.clear()
                else:
                    topology.apply(topology.by_name(new["topology"]))
                self.window.reset_by_setting()  # what each setting costs depends on the distances
            if "read_own_writes" in changed:
                self.client.read_own_writes_seconds = self._ryw_seconds(new)
        log = list(self.loop.switch_log)
        switches = log[self._seen_switches:]
        self._seen_switches = len(log)
        for ev in switches:
            self.events.append({"t": ev.timestamp, "kind": "switch", "old": settings_name(ev.old),
                                "new": settings_name(ev.new), "write_share": round(ev.stats.write_share, 3),
                                "text": ev.reason})
        self._sample(new)
        return changed, switches

    def _sample(self, s):
        now = time.time()
        uptime = now - self.t0
        recent = self.window.summary(now=now, seconds=SAMPLE_SECONDS)
        full = self.window.summary(now=now)
        stats = self.monitor.get_stats(COLLECTION, now=now)
        active = self.client.get_active_settings(COLLECTION)
        span = max(1.0, min(SAMPLE_SECONDS, uptime))
        self.history.append({
            "t": round(now, 3), "setting": settings_name(active), "write_share": round(stats.write_share, 4),
            "target_ops": 0 if s["paused"] else s["ops_per_sec"],
            "read_ops": round((recent["reads"] + recent["probes"]) / span, 1),
            "write_ops": round(recent["writes"] / span, 1),
            **{k: recent[k] for k in ("read_p50", "read_p95", "read_p99", "write_p50", "write_p95", "write_p99")},
            "stale_pct": full["stale_pct"], "errors": recent["errors"],
        })

        decision = self.loop.last_decisions.get(COLLECTION)
        cfg = self.signals
        safe = stats.write_p95("SAFE")
        breach = self.loop._budget_breach.get(COLLECTION)  # (time, p95) the budget remembers (FeedbackLoop)
        backoff_left = max(0.0, cfg.budget_backoff_seconds - (now - breach[0])) if breach else 0.0
        last_switch = self.loop.switch_log[-1].timestamp if self.loop.switch_log else None
        self.latest = {
            "time": time.strftime("%H:%M:%S"), "t": now, "uptime": uptime, "window_s": self.window_s,
            "setting": _settings_view(active), "write_share": round(stats.write_share, 4), "summary": full,
            "achieved_ops": round((full["reads"] + full["writes"] + full["probes"]) / max(1.0, min(self.window_s, uptime)), 1),
            "redirected_reads": self.client.redirected_reads, "switches": len(self.loop.switch_log),
            "since_switch": now - last_switch if last_switch else None,
            "cooldown_left": max(0.0, self.cooldown - (now - last_switch)) if last_switch else 0.0,
            "reason": decision.reason if decision else None,
            "rules": {
                "low": self.thresholds.low_write_share, "high": self.thresholds.high_write_share,
                "hysteresis": self.thresholds.hysteresis,
                "budget_ms": cfg.write_p95_budget_ms,
                "budget_min_samples": cfg.budget_min_samples,
                "safe_write_p95": safe[0] if safe else None, "safe_write_n": safe[1] if safe else 0,
                "breach_p95": breach[1] if backoff_left > 0 else None, "backoff_left": backoff_left,
            },
            "by_setting": self.window.by_setting(),
            "topology": topology_view(s["topology"]),
            "traffic": s,
            "events": list(self.events)[-100:],
        }
