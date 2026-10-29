"""Make the replica set deterministic before an experiment: pin the primary and turn off
chained replication.

Why both matter:
  - "the client is far from the primary" (testbed/topology.py) only means something if the
    primary is always the same node. MongoDB re-elects whenever a node restarts (after a
    Docker restart the primary can be mongo2), so mongo1 gets a higher election priority.
  - with chaining allowed, a secondary may replicate from the OTHER secondary instead of the
    primary. Which one it picks depends on history, and the extra hop adds a whole inter-node
    delay before a write reaches it: measured on the earlier test bed, FAST went from about
    40% to 100% stale. chainingAllowed=false makes every secondary copy from the primary.

Usage:
    python -m testbed.cluster          # pin mongo1, disable chaining, print the state
"""

import time

from pymongo import MongoClient, ReadPreference

URI = "mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0"


def _status(c):
    return c.admin.command("replSetGetStatus", read_preference=ReadPreference.PRIMARY_PREFERRED)


def node_states():
    """{"mongo1": "PRIMARY", ...}"""
    c = MongoClient(URI, serverSelectionTimeoutMS=5000)
    try:
        return {m["name"].split(":")[0]: m["stateStr"] for m in _status(c)["members"]}
    finally:
        c.close()


def sync_sources():
    """{secondary: the node it replicates from}"""
    c = MongoClient(URI, serverSelectionTimeoutMS=5000)
    try:
        return {m["name"].split(":")[0]: (m.get("syncSourceHost") or "").split(":")[0]
                for m in _status(c)["members"] if m["stateStr"] == "SECONDARY"}
    finally:
        c.close()


def wait_until(check, what, timeout=120.0, interval=1.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = check()
            if last is True:
                return
        except Exception as exc:  # noqa: BLE001 - mid-election the driver can briefly fail
            last = type(exc).__name__
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what}; last seen: {last}")


def pin_primary(node="mongo1", priority=10, timeout=120.0):
    """Give `node` the highest election priority, set chainingAllowed=false, and wait until
    `node` is primary, both others are SECONDARY and both replicate straight from it.
    Safe to call repeatedly: it only reconfigures when something differs."""
    wait_until(lambda: "PRIMARY" in node_states().values() or node_states(), "a primary", timeout)
    c = MongoClient(URI, serverSelectionTimeoutMS=15000)
    try:
        cfg = c.admin.command("replSetGetConfig")["config"]
        changed = False
        for member in cfg["members"]:
            want = priority if member["host"].split(":")[0] == node else 1
            if member.get("priority") != want:
                member["priority"] = want
                changed = True
        settings = cfg.setdefault("settings", {})
        if settings.get("chainingAllowed", True):
            settings["chainingAllowed"] = False
            changed = True
        if changed:
            cfg["version"] += 1
            c.admin.command("replSetReconfig", cfg)
    finally:
        c.close()

    def healthy():
        states = node_states()
        return (states.get(node) == "PRIMARY" and sorted(s for n, s in states.items() if n != node) == ["SECONDARY", "SECONDARY"]) or states

    wait_until(healthy, f"{node} primary with two healthy secondaries", timeout)
    wait_until(lambda: set(sync_sources().values()) == {node} or sync_sources(), f"direct replication from {node}", timeout, 2.0)
    return node


if __name__ == "__main__":
    print(f"primary pinned to {pin_primary()}; states {node_states()}; sync sources {sync_sources()}")
