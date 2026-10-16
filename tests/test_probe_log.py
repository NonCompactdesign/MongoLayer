"""Probe tally and the end-to-end locustfile probe path (no database)."""

import os
import subprocess
import sys
from pathlib import Path

from benchmark.probe_log import ProbeTally, read_probe_csv

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_tally_counts_probes_and_stale_per_settings(tmp_path):
    tally = ProbeTally()
    for fresh in (True, True, False, True):
        tally.record("FAST", fresh)
    tally.record("SAFE", True)

    assert tally.totals() == {"FAST": (4, 1), "SAFE": (1, 0)}
    path = tmp_path / "probe.csv"
    tally.write(path)
    assert read_probe_csv(path) == {"FAST": (4, 1), "SAFE": (1, 0)}


def test_no_probes_gives_an_empty_but_valid_file(tmp_path):
    path = tmp_path / "probe.csv"
    ProbeTally().write(path)
    assert read_probe_csv(path) == {}


def test_locustfile_probe_path_end_to_end_with_a_fake_client():
    """Runs the real locustfile task in a separate process (it monkey-patches
    with gevent on import) against a client that is never fresh."""
    env = {**os.environ, "BENCHMARK_PROBE_FRACTION": "0.5", "WORKLOAD_PRESET": "BALANCED"}
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tests" / "_locustfile_probe_check.py")],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout[-1500:] + result.stderr[-1500:]
    assert "PROBE-CHECK OK" in result.stdout
