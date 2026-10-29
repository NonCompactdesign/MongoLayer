"""Live checks of the additions on the real replica set: one short scenario each, printing what
it expected, what happened, and PASS / FAIL. About 3 minutes in total.

  C1 distances   (#1, #12) topology T1 really puts the client ~1 ms from mongo2 and ~87 ms from
                 mongo1 (the pinned primary) and mongo3; EQUAL-5 really gives a 10 ms node round trip
  C2 own writes  (#6) T1, FAST, 100 x (write, then read it back by _id): plain FAST vs FAST +
                 per-key read-your-own-writes; other reads must stay local
  C3 policy      (#5) read-heavy traffic on a floor="SAFE" collection stays SAFE
  C4 budget      (#9) T1, 60% writes: SAFE p95 ~260 ms > 100 ms budget -> MODERATE, with that reason
  C5 hysteresis  (#8) 60% writes -> SAFE, then 37.5% writes: it stays SAFE (> 0.35, the exit
                 threshold), although 37.5% is below the 0.40 that enters SAFE

Every scenario also checks that the switch carries a reason (#10). Short 6 s window, 1 s tick,
3 s cooldown (the benchmark uses 15 / 5 / 10 s). Results go to results/signals_eval/checks.json.

Usage:
    python -m benchmark.signals_check              # all scenarios
    python -m benchmark.signals_check C2 C5        # some
"""

import json
import random
import sys
import threading
import time
import uuid
from pathlib import Path

from adaptive_layer.client import AdaptiveClient
from adaptive_layer.decision import FAST_SETTINGS, SAFE_SETTINGS, settings_name
from adaptive_layer.feedback import FeedbackLoop
from adaptive_layer.monitor import AccessPatternMonitor
from adaptive_layer.rules import CollectionPolicy
from testbed import cluster, netem, topology

URI = "mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0"
DB = "signals_check"
OUT = Path(__file__).resolve().parent.parent / "results" / "signals_eval" / "checks.json"
WINDOW, INTERVAL, COOLDOWN = 6, 1, 3


class Traffic:
    """Background threads doing reads/writes with a given write share until stop().
    `share` can be changed while it runs."""

    def __init__(self, client, coll, write_share, threads=6, keys=1000):
        self.client, self.coll, self.share, self.keys = client, coll, write_share, keys
        self.ok = {"read": 0, "write": 0}
        self.errors = {}
        self.write_ms = []
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._run, daemon=True) for _ in range(threads)]

    def _run(self):
        while not self._stop.is_set():
            key = random.randrange(self.keys)
            op = "write" if random.random() < self.share else "read"
            start = time.monotonic()
            try:
                if op == "write":
                    self.client.write(self.coll, {"key": key, "ts": time.time()})
                    self.write_ms.append((time.monotonic() - start) * 1000)
                else:
                    self.client.read(self.coll, {"key": key})
                self.ok[op] += 1
            except Exception as exc:  # noqa: BLE001 - counted, the scenario judges them
                name = type(exc).__name__
                self.errors[name] = self.errors.get(name, 0) + 1
                time.sleep(0.05)

    def start(self):
        for t in self._threads:
            t.start()
        return self

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10)


def make(coll, read_own_writes=None, policies=None):
    """A client + feedback loop with all the rules (hysteresis, latency budget, policies)."""
    monitor = AccessPatternMonitor(window_seconds=WINDOW)
    client = AdaptiveClient(URI, DB, monitor=monitor, verbose=False, key_field="key",
                            read_own_writes_seconds=read_own_writes)
    client._db.drop_collection(coll)
    client._db[coll].create_index("key")
    loop = FeedbackLoop(client, monitor, interval_seconds=INTERVAL, cooldown_seconds=COOLDOWN, policies=policies)
    return client, loop


def wait_for(check, timeout):
    """Seconds until check() is true, or None."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if check():
            return round(time.monotonic() - start, 1)
        time.sleep(0.2)
    return None


def reason(loop, coll):
    d = loop.last_decisions.get(coll)
    return d.reason if d else ""


def every_switch_has_a_reason(loop):
    return all(ev.reason for ev in list(loop.switch_log))


def session(coll, share, **make_kwargs):
    """Context manager: client + running loop + traffic, all cleaned up afterwards."""
    class _Ctx:
        def __enter__(self):
            self.client, self.loop = make(coll, **make_kwargs)
            self.loop.start()
            self.traffic = Traffic(self.client, coll, share).start()
            return self

        def name(self):
            return settings_name(self.client.get_active_settings(coll))

        def __exit__(self, *exc):
            self.traffic.stop()
            self.loop.stop()
            self.client._db.drop_collection(coll)
            self.client.close()
    return _Ctx()


# --- scenarios ----------------------------------------------------------------------------------

def c1_distances():
    with topology.applied(topology.T1):
        t1 = topology.measure_client_rtts(samples=5)
        t1_nodes = netem.measure_rtt_ms("mongo1", "mongo2")
    with topology.applied(topology.equal(5)):
        eq5_nodes = netem.measure_rtt_ms("mongo1", "mongo3")
        eq5_client = topology.measure_client_rtts(samples=5)
    states = cluster.node_states()
    ok = (t1["mongo2"] < 10 and t1["mongo1"] > 70 and t1["mongo3"] > 70 and 75 < t1_nodes < 100
          and 8 < eq5_nodes < 15 and max(eq5_client.values()) < 10 and states.get("mongo1") == "PRIMARY")
    r = lambda d: {k: round(v, 1) for k, v in d.items()}  # noqa: E731
    return ok, {"T1_client_rtt_ms": r(t1), "T1_node_rtt_ms": round(t1_nodes, 1),
                "EQUAL5_node_rtt_ms": round(eq5_nodes, 1), "EQUAL5_client_rtt_ms": r(eq5_client),
                "primary": next((n for n, s in states.items() if s == "PRIMARY"), None)}


def _own_write_trials(client, coll, n=100):
    stale, own_ms, other_ms = 0, [], []
    for i in range(n):
        _id = client.write(coll, {"key": f"m{i}", "marker": str(uuid.uuid4())}).inserted_id
        start = time.monotonic()
        doc = client.read(coll, {"_id": _id})
        own_ms.append((time.monotonic() - start) * 1000)
        stale += doc is None
        start = time.monotonic()
        client.read(coll, {"key": "never-written"})
        other_ms.append((time.monotonic() - start) * 1000)
    own_ms.sort(), other_ms.sort()
    return {"trials": n, "stale": stale, "own_read_p50_ms": round(own_ms[n // 2], 1),
            "other_read_p50_ms": round(other_ms[n // 2], 1)}


def c2_own_writes():
    out = {}
    with topology.applied(topology.T1):
        for label, ryw in (("plain_fast", None), ("fast_read_own_writes", 5.0)):
            client = AdaptiveClient(URI, DB, verbose=False, key_field="key", read_own_writes_seconds=ryw)
            client._db.drop_collection("c2")
            client.set_active_settings("c2", FAST_SETTINGS)
            client.read("c2", {"key": "warm"})
            try:
                out[label] = _own_write_trials(client, "c2")
                out[label]["redirected_reads"] = client.redirected_reads
            finally:
                client._db.drop_collection("c2")
                client.close()
    ryw = out["fast_read_own_writes"]
    ok = ryw["stale"] == 0 and out["plain_fast"]["stale"] > 0 and ryw["other_read_p50_ms"] < 10 and ryw["redirected_reads"] == 100
    return ok, out


def c3_policy():
    with session("c3_orders", 0.05, policies={"c3_orders": CollectionPolicy(floor="SAFE")}) as s:
        time.sleep(8)
        name, why = s.name(), reason(s.loop, "c3_orders")
    return name == "SAFE" and "policy floor SAFE" in why, {"settings": name, "reason": why}


def c4_budget():
    with topology.applied(topology.T1), session("c4", 0.6) as s:
        reached = wait_for(lambda: any("budget" in ev.reason for ev in list(s.loop.switch_log)), 30)
        time.sleep(1.5)
        name, why = s.name(), (s.loop.switch_log[-1].reason if s.loop.switch_log else "")
        ms = sorted(s.traffic.write_ms)
        p95 = round(ms[int(0.95 * len(ms))], 1) if ms else None
        reasons_ok = every_switch_has_a_reason(s.loop)
    return reached is not None and name == "MODERATE" and reasons_ok, {
        "settings": name, "reason": why, "after_s": reached, "all_writes_p95_ms": p95}


def c5_hysteresis():
    netem.clear()  # equal distances, so SAFE is cheap and the budget stays out of it
    with session("c5", 0.6) as s:
        safe_after = wait_for(lambda: s.client.get_active_settings("c5") == SAFE_SETTINGS, 15)
        s.traffic.share = 0.375  # between 0.35 (leave SAFE with hysteresis) and 0.40 (enter SAFE)
        time.sleep(WINDOW + 3 * COOLDOWN + 3)  # old 60% traffic gone from the window, several ticks
        st = s.loop._monitor.get_stats("c5")
        out = {"safe_after_s": safe_after, "settings_after_drop": s.name(), "write_share_after_drop": round(st.write_share, 3),
               "reason": reason(s.loop, "c5"), "switches": len(s.loop.switch_log), "reasons_ok": every_switch_has_a_reason(s.loop)}
    ok = (safe_after is not None and out["settings_after_drop"] == "SAFE" and "hysteresis" in out["reason"]
          and out["reasons_ok"])
    return ok, out


SCENARIOS = {"C1": c1_distances, "C2": c2_own_writes, "C3": c3_policy, "C4": c4_budget, "C5": c5_hysteresis}


def main(argv=None):
    names = (sys.argv[1:] if argv is None else argv) or list(SCENARIOS)
    cluster.pin_primary()
    results = json.loads(OUT.read_text()) if OUT.exists() else {}
    try:
        for name in names:
            print(f"\n=== {name}: {SCENARIOS[name].__name__} ===", flush=True)
            started = time.monotonic()
            ok, detail = SCENARIOS[name]()
            detail["seconds"] = round(time.monotonic() - started, 1)
            results[name] = {"pass": ok, **detail}
            print(json.dumps(results[name], indent=2, default=str))
            print(f"{name}: {'PASS' if ok else 'FAIL'}", flush=True)
    finally:
        netem.clear()
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(results, indent=2, default=str))
    print("\n" + "  ".join(f"{n}={'PASS' if r['pass'] else 'FAIL'}" for n, r in results.items()))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
