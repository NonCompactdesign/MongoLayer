"""Phase 2 smoke test for AdaptiveClient.

This is an INTEGRATION test, not a unit test: it requires the real 3-node
replica set from Phase 1 to be running (`docker compose up -d`, hosts file
entries in place — see docs/replica_set_setup.md). Unlike the pure unit
tests coming in Phase 3+ (which test the Monitor/Decision Engine with no
database at all), this one intentionally hits real MongoDB to prove the
proxy layer actually works end-to-end.
"""

import uuid

import pytest

from adaptive_layer.client import AdaptiveClient
from adaptive_layer.monitor import AccessPatternMonitor

URI = "mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0"
DB_NAME = "phase2_smoke_test"


@pytest.fixture
def client():
    c = AdaptiveClient(URI, DB_NAME)
    yield c
    # clean up the collection this test wrote to, then close
    c._db.drop_collection("smoke")
    c.close()


def test_majority_write_then_secondary_read_round_trip(client):
    marker = str(uuid.uuid4())
    doc = {"marker": marker, "hello": "phase2"}

    write_result = client.write("smoke", doc, write_concern="majority")
    assert write_result.acknowledged
    assert write_result.inserted_id is not None

    found = client.read(
        "smoke",
        {"marker": marker},
        read_concern="local",
        read_preference="secondaryPreferred",
    )
    assert found is not None
    assert found["hello"] == "phase2"


def test_monitor_wiring_reflects_real_traffic():
    """Phase 3: prove _log_op actually feeds a real AccessPatternMonitor,
    not just prints. Separate client instance/monitor so this doesn't
    interfere with the round-trip test above."""
    monitor = AccessPatternMonitor(window_seconds=30)
    c = AdaptiveClient(URI, DB_NAME, monitor=monitor)
    try:
        marker = str(uuid.uuid4())
        c.write("smoke_monitor", {"marker": marker}, write_concern="majority")
        c.read("smoke_monitor", {"marker": marker}, read_preference="secondaryPreferred")
        c.read("smoke_monitor", {"marker": marker}, read_preference="secondaryPreferred")

        stats = monitor.get_stats("smoke_monitor")
        assert stats.write_count == 1
        assert stats.read_count == 2
        assert stats.read_write_ratio == 2.0
    finally:
        c._db.drop_collection("smoke_monitor")
        c.close()


if __name__ == "__main__":
    # allow `python tests/test_client_smoke.py` as a quick manual run too
    raise SystemExit(pytest.main([__file__, "-v"]))
