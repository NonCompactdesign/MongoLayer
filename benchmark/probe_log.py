"""Staleness measured inside a load test.

During a Locust run a small fraction of iterations are "probes": a normal
write followed immediately by a read of that same document (by _id). A probe
whose read does not find the document is a stale read. Each probe is tallied
under the settings that were in force when its read ran, so for an adaptive run
the stale rate while in FAST can be told apart from the rate while in SAFE.

Probe misses are NOT reported to Locust as failures (they are a measurement, not
an error), so they never affect a run's failure count; they go to a separate CSV.
"""

import csv

FIELDS = ["settings", "probes", "stale"]


class ProbeTally:
    def __init__(self):
        self._counts = {}  # settings name -> [probes, stale]

    def record(self, settings, fresh):
        counts = self._counts.setdefault(settings, [0, 0])
        counts[0] += 1
        counts[1] += 0 if fresh else 1

    def totals(self):
        return {name: tuple(counts) for name, counts in self._counts.items()}

    def write(self, path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(FIELDS)
            for name, (probes, stale) in sorted(self.totals().items()):
                writer.writerow([name, probes, stale])


def read_probe_csv(path):
    """{settings: (probes, stale)} from one probe CSV (empty if there were no probes)."""
    with open(path, newline="", encoding="utf-8") as f:
        return {r["settings"]: (int(r["probes"]), int(r["stale"])) for r in csv.DictReader(f)}
