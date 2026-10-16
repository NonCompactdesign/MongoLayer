"""Per-run record of what the adaptive layer chose and when.

write_switch_log() is called at the end of an adaptive Locust run with the
FeedbackLoop's switch_log; time_in_settings() turns the resulting CSV into
seconds spent in each named configuration, which is the direct evidence of
whether adaptive actually used FAST / MODERATE / SAFE during a run.

CSV rows: "start" (t=0, the client's MODERATE default), one "switch" row per
real settings change (settings = what became active, plus the write share and
counts that triggered it), and "stop" (t=run length).
"""

import csv

from adaptive_layer.decision import MODERATE_SETTINGS, settings_name

FIELDS = ["event", "seconds", "settings", "write_share", "reads", "writes"]
NAMES = ("FAST", "MODERATE", "SAFE")


def write_switch_log(path, switch_log, started_at, stopped_at):
    current = settings_name(MODERATE_SETTINGS)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(FIELDS)
        writer.writerow(["start", 0.0, current, "", "", ""])
        for ev in switch_log:
            current = settings_name(ev.new)
            writer.writerow([
                "switch", round(ev.timestamp - started_at, 3), current,
                round(ev.stats.write_share, 4), ev.stats.read_count, ev.stats.write_count,
            ])
        writer.writerow(["stop", round(stopped_at - started_at, 3), current, "", "", ""])


def time_in_settings(path):
    """{"FAST": s, "MODERATE": s, "SAFE": s, "switches": n} for one switch-log CSV."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    totals = {name: 0.0 for name in NAMES}
    for cur, nxt in zip(rows, rows[1:]):
        totals[cur["settings"]] = totals.get(cur["settings"], 0.0) + float(nxt["seconds"]) - float(cur["seconds"])
    totals["switches"] = sum(r["event"] == "switch" for r in rows)
    return totals
