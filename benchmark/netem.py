"""Inter-node network latency injection for the 3-node Docker replica set.

Uses `tc netem` inside each mongo container (see docker/mongo/Dockerfile and
the NET_ADMIN capability in docker-compose.yml). The delay is applied ONLY to
packets a node sends to its two peers (matched by destination IP), so it
models replication / majority-ack / secondary-lag latency between nodes
without also slowing the client's own connection to each node.

`delay_ms` is the ONE-WAY delay added on each node's egress toward its peers,
so the round trip between two nodes is 2 x delay_ms.

Latency levels and where their values come from (round trip -> one-way):
  0  ms  baseline, no injection (everything on one host)
  1  ms  same region, different AZ    RTT ~2 ms   (AWS: "single-digit ms" between AZs;
                                                  measured 0.39-2.42 ms, bitsand.cloud 2023)
  5  ms  neighbouring regions         RTT ~8-9 ms (Azure P50, East US <-> East US 2)
  33 ms  cross-country                RTT ~66 ms  (Azure P50, East US <-> West US)
  43 ms  transatlantic                RTT ~85-89 ms (Azure P50, East US <-> West Europe)
"""

import re
import subprocess

NODES = ["mongo1", "mongo2", "mongo3"]
IFACE = "eth0"
IFB = "ifb0"  # used by benchmark/topology.py to delay incoming client requests

LATENCY_LEVELS_MS = [0, 5]  # default sweep; 1, 33 and 43 remain valid via --latency (T1 uses 43 one way between nodes)
ALL_LATENCY_LEVELS_MS = [0, 1, 5, 33, 43]
LATENCY_LABELS = {
    0: "baseline (localhost)",
    1: "same region, cross-AZ",
    5: "neighbouring regions",
    33: "cross-country",
    43: "transatlantic",
}


def _run(args, check=True):
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def _exec(node, cmd, check=True):
    return _run(["docker", "exec", "-u", "root", node, "sh", "-c", cmd], check=check)


def _ip(node):
    out = _run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", node])
    return out.stdout.strip()


def clear():
    """Remove any injected latency from every node (safe to call repeatedly),
    including benchmark/topology.py's ingress shaping."""
    for node in NODES:
        _exec(node, f"tc qdisc del dev {IFACE} root", check=False)
        _exec(node, f"tc qdisc del dev {IFACE} ingress", check=False)
        _exec(node, f"ip link del {IFB}", check=False)


def apply_latency(delay_ms):
    """Set the one-way inter-node delay. 0 just clears any existing delay."""
    clear()
    if delay_ms <= 0:
        return
    ips = {node: _ip(node) for node in NODES}
    for node in NODES:
        _exec(node, f"tc qdisc add dev {IFACE} root handle 1: prio")
        _exec(node, f"tc qdisc add dev {IFACE} parent 1:3 handle 30: netem delay {delay_ms}ms limit 100000")
        for peer, ip in ips.items():
            if peer != node:
                _exec(
                    node,
                    f"tc filter add dev {IFACE} protocol ip parent 1: prio 3 "
                    f"u32 match ip dst {ip}/32 flowid 1:3",
                )


def measure_rtt_ms(src="mongo1", dst="mongo2", count=5):
    """Average ping RTT (ms) from `src` to `dst` - used to verify the injection."""
    out = _exec(src, f"ping -c {count} -q {dst}").stdout
    match = re.search(r"= [\d.]+/([\d.]+)/", out)
    if not match:
        raise RuntimeError(f"could not parse ping output: {out!r}")
    return float(match.group(1))
