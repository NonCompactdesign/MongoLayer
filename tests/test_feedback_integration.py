"""End-to-end integration test for FeedbackLoop: real replica set (Phase 1),
real AdaptiveClient (Phase 2/5), real AccessPatternMonitor (Phase 3), real
background thread via start()/stop() - this is the actual stack demo_app.py
(Phase 7) will run. Unlike test_feedback.py (fake clock, no threading), this
one proves the whole loop closes for real, not just in isolation.

Requires the Phase 1 replica set running (docker compose up -d).
"""

import time
import uuid

import pytest

from adaptive_layer.client import AdaptiveClient
from adaptive_layer.decision import FAST_SETTINGS, SAFE_SETTINGS
from adaptive_layer.feedback import FeedbackLoop
from adaptive_layer.monitor import AccessPatternMonitor

URI = "mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0"
DB_NAME = "phase6_feedback_test"

# Short window/interval/cooldown so the test finishes in a few seconds
# instead of matching the real 30s/60s defaults meant for production use.
WINDOW_SECONDS = 2
INTERVAL_SECONDS = 1
COOLDOWN_SECONDS = 0


def _wait_until_settings(client, collection, expected, timeout=6, poll_interval=0.2):
    """Poll rather than a single fixed sleep - more robust against thread
    scheduling / tick timing variance than assuming a tick landed exactly
    when expected."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get_active_settings(collection) == expected:
            return True
        time.sleep(poll_interval)
    return False


@pytest.fixture
def stack():
    monitor = AccessPatternMonitor(window_seconds=WINDOW_SECONDS)
    client = AdaptiveClient(URI, DB_NAME, monitor=monitor)
    loop = FeedbackLoop(client, monitor, interval_seconds=INTERVAL_SECONDS, cooldown_seconds=COOLDOWN_SECONDS)
    yield client, monitor, loop
    loop.stop()
    client._db.drop_collection("burst")
    client.close()


def test_feedback_loop_escalates_on_write_burst_then_relaxes_on_reads(stack):
    client, monitor, loop = stack
    loop.start()

    # Phase A: write burst well above HIGH_WRITE_THRESHOLD (5 writes/sec)
    # within the 2s window - 20 writes as fast as the client can go.
    for i in range(20):
        client.write("burst", {"marker": str(uuid.uuid4()), "i": i})

    assert _wait_until_settings(client, "burst", SAFE_SETTINGS), (
        f"expected SAFE_SETTINGS after write burst, got "
        f"{client.get_active_settings('burst')} (stats={monitor.get_stats('burst')})"
    )

    # Phase B: let the burst age out of the window entirely, then read-only.
    time.sleep(WINDOW_SECONDS + 0.5)
    for _ in range(10):
        client.read("burst", {})

    assert _wait_until_settings(client, "burst", FAST_SETTINGS), (
        f"expected FAST_SETTINGS after switching to read-only, got "
        f"{client.get_active_settings('burst')} (stats={monitor.get_stats('burst')})"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
