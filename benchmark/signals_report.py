"""Summarise the signal-rule load tests (results/signals_eval/topology_<name>/rep<k>/) into one table.

Per (topology, config, preset): read and write p50 / p95 from Locust's stats CSV, failures, the
in-load staleness probes (write then read the same document), the share of the run spent in each
setting and the number of switches (adaptive configs), the most common switch reason, and how many
reads the per-key read-your-own-writes routed to the primary (adaptive_signals).

Usage:
    python -m benchmark.signals_report                 # prints a markdown table, writes summary.csv
"""

import csv
import json
from collections import Counter
from pathlib import Path

from benchmark.probe_log import read_probe_csv
from benchmark.run_benchmark import CONFIGS, PRESETS, SIGNALS_DIR
from benchmark.switch_log import time_in_settings


def _rows(stats_csv):
    with open(stats_csv, newline="", encoding="utf-8") as f:
        return {r["Name"]: r for r in csv.DictReader(f)}


def _num(row, col):
    return float(row[col]) if row and row.get(col) not in (None, "", "N/A") else None


def summarise(root=SIGNALS_DIR):
    out = []
    for topo_dir in sorted(Path(root).glob("topology_*")):
        topo = topo_dir.name[len("topology_"):]
        for rep_dir in sorted(topo_dir.glob("rep*")):
            for config in CONFIGS:
                for preset in PRESETS:
                    prefix = rep_dir / f"{config}_{preset}"
                    stats = Path(f"{prefix}_stats.csv")
                    if not stats.exists():
                        continue
                    rows = _rows(stats)
                    agg, rd, wr = rows.get("Aggregated"), rows.get("read"), rows.get("write")
                    probes = read_probe_csv(f"{prefix}_probe.csv") if Path(f"{prefix}_probe.csv").exists() else {}
                    n_probe = sum(p for p, _ in probes.values())
                    n_stale = sum(s for _, s in probes.values())
                    row = {
                        "topology": topo, "rep": rep_dir.name, "config": config, "preset": preset,
                        "requests": int(_num(agg, "Request Count") or 0), "failures": int(_num(agg, "Failure Count") or 0),
                        "read_p50": _num(rd, "50%"), "read_p95": _num(rd, "95%"),
                        "write_p50": _num(wr, "50%"), "write_p95": _num(wr, "95%"),
                        "probes": n_probe, "stale": n_stale,
                        "stale_pct": round(100.0 * n_stale / n_probe, 1) if n_probe else None,
                    }
                    for name in ("FAST", "MODERATE", "SAFE"):  # probes split by the settings in force
                        row[f"{name}_probes"], row[f"{name}_stale"] = probes.get(name, (0, 0))
                    sw = Path(f"{prefix}_switches.csv")
                    if sw.exists():
                        t = time_in_settings(sw)
                        total = sum(t[n] for n in ("FAST", "MODERATE", "SAFE")) or 1.0
                        row.update({f"{n}_pct": round(100.0 * t[n] / total) for n in ("FAST", "MODERATE", "SAFE")})
                        row["switches"] = t["switches"]
                        with open(sw, newline="", encoding="utf-8") as f:
                            reasons = [r.get("reason", "") for r in csv.DictReader(f) if r["event"] == "switch"]
                        # the rule part of the reason, without the numbers that change every time
                        kinds = Counter(_kind(r) for r in reasons if r)
                        row["reasons"] = ", ".join(f"{k} x{n}" for k, n in kinds.most_common(3))
                    cl = Path(f"{prefix}_client.json")
                    if cl.exists():
                        row["redirected_reads"] = json.loads(cl.read_text())["redirected_reads"]
                    out.append(row)
    return out


def _kind(reason):
    """A short label for which rules a switch reason mentions."""
    labels = [("budget", "budget"), ("policy", "policy"), ("hysteresis", "hysteresis")]
    found = [label for text, label in labels if text in reason]
    return "+".join(found) if found else "traffic"


def to_markdown(rows):
    head = ["topology", "config", "preset", "fail", "read p50/p95 ms", "write p50/p95 ms", "stale probes",
            "FAST/MOD/SAFE %", "switches", "reasons", "own-write reads to primary"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    f = lambda v: "-" if v is None else f"{v:.0f}"  # noqa: E731
    for r in rows:
        mix = f"{r['FAST_pct']}/{r['MODERATE_pct']}/{r['SAFE_pct']}" if "FAST_pct" in r else "-"
        lines.append("| " + " | ".join([
            r["topology"], r["config"], r["preset"], str(r["failures"]),
            f"{f(r['read_p50'])} / {f(r['read_p95'])}", f"{f(r['write_p50'])} / {f(r['write_p95'])}",
            f"{r['stale']}/{r['probes']} ({r['stale_pct']}%)" if r["probes"] else "-",
            mix, str(r.get("switches", "-")), r.get("reasons", "-") or "-", str(r.get("redirected_reads", "-")),
        ]) + " |")
    return "\n".join(lines)


def main():
    rows = summarise()
    if not rows:
        print(f"no results under {SIGNALS_DIR}")
        return
    path = SIGNALS_DIR / "summary.csv"
    keys = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    print(to_markdown(rows))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
