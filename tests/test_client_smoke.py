"""Integration tests for AdaptiveClient against the real replica set.

Requires the 3-node replica set from Phase 1 to be running (`docker compose
up -d`, hosts file entries in place — see docs/replica_set_setup.md).
Unlike the pure unit tests (Monitor/Decision Engine, no database), these
intentionally hit real MongoDB to prove the proxy layer works end-to-end.

Since Phase 5, write()/read() no longer take settings as call arguments -
call set_active_settings(collection, Settings(...)) first, then write()/
read() use whatever's currently active for that collection.
"""

import time
import uuid

import pytest

from adaptive_layer.client import AdaptiveClient
from adaptive_layer.decision import FAST_SETTINGS, SAFE_SETTINGS
from adaptive_layer.monitor import AccessPatternMonitor


def _read_with_retry(client, collection, query, attempts=10, delay_s=0.05):
    """FAST_SETTINGS uses read_preference="nearest" + read_concern="local",
    which can legitimately route a read to a secondary that hasn't replicated
    a just-written document yet - this is the exact staleness tradeoff the
    project studies, not a bug. A real caller choosing "fast" settings has
    to expect (and tolerate) this; so does this test."""
    for _ in range(attempts):
        doc = client.read(collection, query)
        if doc is not None:
            return doc
        time.sleep(delay_s)
    return None

URI = "mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0"
DB_NAME = "phase2_smoke_test"


@pytest.fixture
def client():
    c = AdaptiveClient(URI, DB_NAME)
    yield c
    # clean up any collections this test session wrote to, then close
    for name in ("smoke", "smoke_monitor", "smoke_a", "smoke_b"):
        c._db.drop_collection(name)
    c.close()


def test_majority_write_then_secondary_read_round_trip(client):
    marker = str(uuid.uuid4())
    doc = {"marker": marker, "hello": "phase2"}

    client.set_active_settings("smoke", SAFE_SETTINGS)
    write_result = client.write("smoke", doc)
    assert write_result.acknowledged
    assert write_result.inserted_id is not None

    found = client.read("smoke", {"marker": marker})
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
        c.set_active_settings("smoke_monitor", SAFE_SETTINGS)
        c.write("smoke_monitor", {"marker": marker})
        c.read("smoke_monitor", {"marker": marker})
        c.read("smoke_monitor", {"marker": marker})

        stats = monitor.get_stats("smoke_monitor")
        assert stats.write_count == 1
        assert stats.read_count == 2
        assert stats.read_write_ratio == 2.0
    finally:
        c._db.drop_collection("smoke_monitor")
        c.close()


def test_two_collections_with_different_active_settings_behave_independently(client):
    """Phase 5's manual test, made automatic: give two collections opposite
    settings through the same AdaptiveClient instance and confirm each one
    actually used its own configuration, not the other's or some shared
    default."""
    client.set_active_settings("smoke_a", SAFE_SETTINGS)
    client.set_active_settings("smoke_b", FAST_SETTINGS)

    assert client.get_active_settings("smoke_a") == SAFE_SETTINGS
    assert client.get_active_settings("smoke_b") == FAST_SETTINGS

    marker_a, marker_b = str(uuid.uuid4()), str(uuid.uuid4())
    client.write("smoke_a", {"marker": marker_a})
    client.write("smoke_b", {"marker": marker_b})

    # smoke_a uses SAFE_SETTINGS (primary read) - must be visible immediately
    assert client.read("smoke_a", {"marker": marker_a})["marker"] == marker_a
    # smoke_b uses FAST_SETTINGS (nearest read, local concern) - may briefly
    # race the secondary's replication lag, so retry rather than assert
    # instant visibility (see _read_with_retry's docstring)
    found_b = _read_with_retry(client, "smoke_b", {"marker": marker_b})
    assert found_b is not None, "smoke_b write never became visible even after retrying"
    assert found_b["marker"] == marker_b

    # settings are still independent after both writes - one didn't clobber the other
    assert client.get_active_settings("smoke_a") == SAFE_SETTINGS
    assert client.get_active_settings("smoke_b") == FAST_SETTINGS


def test_collection_never_configured_defaults_to_moderate_settings(client):
    from adaptive_layer.decision import MODERATE_SETTINGS

    assert client.get_active_settings("never_touched") == MODERATE_SETTINGS


if __name__ == "__main__":
    # allow `python tests/test_client_smoke.py` as a quick manual run too
    raise SystemExit(pytest.main([__file__, "-v"]))
