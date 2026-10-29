"""Unequal distances: per-link delays between the nodes AND between the client and each node.

testbed/netem.py delays every node-to-node link equally and leaves the client alone, so the
client is the same distance from every node. Then FAST's `nearest` reads can never be faster
than reading from the primary, and the round-trip rule (adaptive_layer/rules.py, rule 2)
correctly never picks FAST. A topology gives every link its own delay:

  node <-> node   each node delays packets to a peer by half that pair's round trip (both
                  ends do it, so the round trip is the stated value).
  client <-> node the client runs on the host, which cannot get tc rules of its own (Docker
                  Desktop), so each node delays both directions of its link to the host (the
                  bridge gateway address): replies on egress, requests on ingress through an
                  ifb device, half of the extra round trip each. Delaying only one direction
                  would make read-after-write look fresher than in a real geography (the
                  write would reach the primary without delay).

Topologies (round trips, from the same AWS / Azure figures netem.py cites):

  EQUAL(one_way_ms)  what netem.apply_latency does: all node pairs equal, client equidistant.
  T1 "far primary"   nodes 86 ms apart (transatlantic); the client is next to mongo2 and ~86 ms
                     from mongo1 (the primary) and mongo3. `nearest` reads are local (~1 ms),
                     primary reads and majority writes are far.
  T2 "regional pair" mongo1-mongo2 10 ms apart, mongo3 86 ms from both; the client next to
                     mongo2, ~10 ms from the primary. The primary is within pymongo's 15 ms
                     local threshold of mongo2, so `nearest` gains little and rule 2 should
                     keep FAST off; a majority needs only the near pair, so SAFE is cheap.

The primary is pinned to mongo1 (testbed/cluster.py), or "far from the primary" would mean
whatever node won the last election.

Usage:
    python -m testbed.topology T1        # apply and print measured client round trips
    python -m testbed.topology clear
"""

import sys
from contextlib import contextmanager
from dataclasses import dataclass, field

from testbed import netem

RTT_FAR_MS = 86.0  # ~ transatlantic round trip (netem's 43 ms one way)
RTT_REGION_MS = 10.0  # neighbouring regions
CLIENT_FAR_EXTRA_MS = 85.0  # extra client round trip; the ~1 ms base link makes it ~86


@dataclass(frozen=True)
class Topology:
    name: str
    description: str
    node_rtt_ms: dict = field(default_factory=dict)  # frozenset({a, b}) -> round trip
    client_extra_rtt_ms: dict = field(default_factory=dict)  # node -> extra client round trip

    def rtt(self, a, b):
        return self.node_rtt_ms.get(frozenset((a, b)), 0.0)

    def client_extra(self, node):
        return self.client_extra_rtt_ms.get(node, 0.0)


def _pairs(**rtts):
    """_pairs(mongo1_mongo2=10) -> {frozenset({'mongo1', 'mongo2'}): 10.0}"""
    return {frozenset(k.split("_")): float(v) for k, v in rtts.items()}


def equal(one_way_ms):
    rtt = 2.0 * one_way_ms
    return Topology(
        name=f"EQUAL-{one_way_ms:g}ms",
        description=f"client equidistant, node pairs {rtt:g} ms apart",
        node_rtt_ms=_pairs(mongo1_mongo2=rtt, mongo1_mongo3=rtt, mongo2_mongo3=rtt),
    )


T1 = Topology(
    name="T1",
    description="far primary: nodes 86 ms apart; client next to mongo2, ~86 ms from mongo1 (primary) and mongo3",
    node_rtt_ms=_pairs(mongo1_mongo2=RTT_FAR_MS, mongo1_mongo3=RTT_FAR_MS, mongo2_mongo3=RTT_FAR_MS),
    client_extra_rtt_ms={"mongo1": CLIENT_FAR_EXTRA_MS, "mongo3": CLIENT_FAR_EXTRA_MS},
)

T2 = Topology(
    name="T2",
    description="regional pair: mongo1-mongo2 10 ms, mongo3 86 ms from both; client next to mongo2, ~10 ms from the primary",
    node_rtt_ms=_pairs(mongo1_mongo2=RTT_REGION_MS, mongo1_mongo3=RTT_FAR_MS, mongo2_mongo3=RTT_FAR_MS),
    client_extra_rtt_ms={"mongo1": RTT_REGION_MS, "mongo3": CLIENT_FAR_EXTRA_MS},
)

TOPOLOGIES = {"T1": T1, "T2": T2}


def by_name(name):
    """"T1", "T2", or "EQUAL-<one way ms>" (e.g. EQUAL-0, EQUAL-43)."""
    if name in TOPOLOGIES:
        return TOPOLOGIES[name]
    if name.upper().startswith("EQUAL-"):
        return equal(float(name.split("-", 1)[1].rstrip("ms")))
    raise ValueError(f"unknown topology {name!r}; expected one of {sorted(TOPOLOGIES)} or EQUAL-<ms>")


def plan_commands(topology, node_ips, host_ip):
    """{node: [shell command, ...]}. Pure, so it is tested without Docker.
    Egress: one prio band per delayed destination (peers, and the host for replies), each with
    its own netem; unmatched traffic stays in band 1 undelayed. Ingress: requests from the host
    are redirected through ifb0 and delayed there."""
    iface, ifb = netem.IFACE, netem.IFB
    plan = {}
    for node in node_ips:
        dests = [(ip, topology.rtt(node, peer) / 2.0) for peer, ip in node_ips.items()
                 if peer != node and topology.rtt(node, peer) > 0]
        extra = topology.client_extra(node)
        if extra > 0:
            dests.append((host_ip, extra / 2.0))
        cmds = []
        if dests:
            cmds.append(f"tc qdisc add dev {iface} root handle 1: prio bands {1 + len(dests)} priomap " + " ".join(["0"] * 16))
            for i, (ip, delay) in enumerate(dests):
                band = i + 2
                cmds.append(f"tc qdisc add dev {iface} parent 1:{band} handle {band * 10}: netem delay {delay:g}ms limit 100000")
                cmds.append(f"tc filter add dev {iface} protocol ip parent 1: prio {band} u32 match ip dst {ip}/32 flowid 1:{band}")
        if extra > 0:
            cmds += [
                f"ip link add {ifb} type ifb",
                f"ip link set {ifb} up",
                f"tc qdisc add dev {iface} handle ffff: ingress",
                f"tc filter add dev {iface} parent ffff: protocol ip u32 match ip src {host_ip}/32 "
                f"action mirred egress redirect dev {ifb}",
                f"tc qdisc add dev {ifb} root netem delay {extra / 2.0:g}ms limit 100000",
            ]
        plan[node] = cmds
    return plan


def host_ip():
    """The address the nodes see the host's (the client's) traffic come from: the bridge gateway."""
    out = netem._run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}", netem.NODES[0]])
    return out.stdout.strip()


def apply(topology):
    """Install a topology on the running containers (clears whatever was there first)."""
    netem.clear()
    node_ips = {node: netem._ip(node) for node in netem.NODES}
    for node, cmds in plan_commands(topology, node_ips, host_ip()).items():
        for cmd in cmds:
            netem._exec(node, cmd)


def clear():
    netem.clear()


@contextmanager
def applied(topology):
    """`with applied(T1): ...` always removes the delays afterwards, even on failure."""
    apply(topology)
    try:
        yield topology
    finally:
        clear()


def measure_client_rtts(samples=5):
    """{node: median client round trip in ms}, measured with `ping` commands sent directly to each node."""
    import statistics
    import time

    from pymongo import MongoClient

    rtts = {}
    for node, port in zip(netem.NODES, (27017, 27018, 27019)):
        c = MongoClient(f"mongodb://{node}:{port}/?directConnection=true", serverSelectionTimeoutMS=5000)
        try:
            c.admin.command("ping")
            times = []
            for _ in range(samples):
                start = time.perf_counter()
                c.admin.command("ping")
                times.append((time.perf_counter() - start) * 1000)
            rtts[node] = statistics.median(times)
        finally:
            c.close()
    return rtts


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] == "clear":
        clear()
        print("cleared all injected delays")
        return
    topo = by_name(args[0])
    apply(topo)
    print(f"applied {topo.name}: {topo.description}")
    for node, ms in measure_client_rtts().items():
        print(f"  client -> {node}: {ms:.1f} ms")
    print(f"  mongo1 -> mongo2 ping: {netem.measure_rtt_ms('mongo1', 'mongo2'):.1f} ms")


if __name__ == "__main__":
    main()
