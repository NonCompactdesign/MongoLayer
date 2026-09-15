"""demo_app.py - Phase 7: a runnable, narratable demonstration that the
Adaptive Consistency Layer actually adapts in real time.

Requires the Phase 1 replica set running (`docker compose up -d`).

Runs three scripted phases against one demo collection, through the full
real stack (AdaptiveClient + AccessPatternMonitor + FeedbackLoop, actually
running in a background thread - nothing mocked):

  Phase A: read-heavy, cold  -> expect settings to relax to FAST_SETTINGS
  Phase B: write burst       -> expect settings to escalate to SAFE_SETTINGS
  Phase C: mixed / moderate  -> expect settings to settle at MODERATE_SETTINGS

Between phases the script deliberately waits long enough for the monitor's
sliding window to fully clear the previous phase's events, so each phase's
resulting settings are attributable to that phase's traffic alone - not
leftover history bleeding in from the phase before it. That matters for
this to read cleanly to someone watching it live.
"""

import time
import uuid

from adaptive_layer.client import AdaptiveClient
from adaptive_layer.decision import FAST_SETTINGS, MODERATE_SETTINGS, SAFE_SETTINGS
from adaptive_layer.feedback import FeedbackLoop
from adaptive_layer.monitor import AccessPatternMonitor

URI = "mongodb://mongo1:27017,mongo2:27018,mongo3:27019/?replicaSet=rs0"
DB_NAME = "demo"
COLLECTION = "live_demo"

WINDOW_SECONDS = 8
INTERVAL_SECONDS = 2
COOLDOWN_SECONDS = 3
TICK_WAIT_SECONDS = INTERVAL_SECONDS + 1
# Deliberately longer than WINDOW_SECONDS, so this pause guarantees the
# previous phase's events have fully aged out before the next phase starts.
INTER_PHASE_PAUSE = WINDOW_SECONDS + 2


def banner(text):
    print()
    print("=" * 72)
    print(text)
    print("=" * 72)


def print_status(client, monitor, collection, expected):
    stats = monitor.get_stats(collection)
    settings = client.get_active_settings(collection)
    match = "MATCHES expectation" if settings == expected else "does NOT match expectation (see note below)"
    print(
        f"  stats: reads={stats.read_count} writes={stats.write_count} "
        f"read_write_ratio={stats.read_write_ratio:.2f} write_freq={stats.write_freq:.2f}/s"
    )
    print(
        f"  ACTIVE SETTINGS -> write_concern={settings.write_concern!r} "
        f"read_concern={settings.read_concern!r} read_preference={settings.read_preference!r}  ({match})"
    )
    if settings != expected:
        print(
            f"  note: expected write_concern={expected.write_concern!r} "
            f"read_concern={expected.read_concern!r} read_preference={expected.read_preference!r} - "
            f"if this happens consistently (not just a one-off timing race), the Phase 4 "
            f"thresholds may need attention before Phase 12."
        )


def wait_for_next_tick():
    print(f"  (waiting {TICK_WAIT_SECONDS}s for the Feedback Loop to react...)")
    time.sleep(TICK_WAIT_SECONDS)


def pause_for_window_to_clear():
    print()
    print(f"--- letting the {WINDOW_SECONDS}s monitoring window clear ({INTER_PHASE_PAUSE}s pause) ---")
    time.sleep(INTER_PHASE_PAUSE)


def phase_read_heavy(client):
    banner("PHASE A: READ-HEAVY TRAFFIC (expect -> FAST_SETTINGS)")
    print("Issuing 20 reads, no writes...")
    for _ in range(20):
        client.read(COLLECTION, {})
        time.sleep(0.1)


def phase_write_burst(client):
    banner("PHASE B: WRITE BURST (expect -> SAFE_SETTINGS)")
    print("Issuing 60 writes as fast as possible "
          f"(needs > {WINDOW_SECONDS * 5} writes within the {WINDOW_SECONDS}s window to clear "
          "the HIGH_WRITE_THRESHOLD of 5.0/s with margin)...")
    for i in range(60):
        client.write(COLLECTION, {"marker": str(uuid.uuid4()), "i": i})


def phase_mixed(client):
    banner("PHASE C: MIXED / MODERATE TRAFFIC (expect -> MODERATE_SETTINGS)")
    print("Issuing an interleaved mix of reads and writes at a moderate pace...")
    for i in range(15):
        if i % 3 == 0:
            client.write(COLLECTION, {"marker": str(uuid.uuid4()), "i": i})
        else:
            client.read(COLLECTION, {})
        time.sleep(0.3)


def main():
    monitor = AccessPatternMonitor(window_seconds=WINDOW_SECONDS)
    client = AdaptiveClient(URI, DB_NAME, monitor=monitor)
    loop = FeedbackLoop(client, monitor, interval_seconds=INTERVAL_SECONDS, cooldown_seconds=COOLDOWN_SECONDS)

    banner("ADAPTIVE CONSISTENCY LAYER - LIVE DEMO")
    print(f"Collection: {DB_NAME}.{COLLECTION}")
    print(f"Monitor window={WINDOW_SECONDS}s | Feedback interval={INTERVAL_SECONDS}s | cooldown={COOLDOWN_SECONDS}s")
    print("Starting settings for any never-configured collection: MODERATE_SETTINGS (the safe default).")

    # seed one document so the read-heavy phase has something to find
    client.write(COLLECTION, {"seed": True})

    loop.start()
    try:
        phase_read_heavy(client)
        wait_for_next_tick()
        print("\nAfter Phase A (read-heavy):")
        print_status(client, monitor, COLLECTION, expected=FAST_SETTINGS)

        pause_for_window_to_clear()

        phase_write_burst(client)
        wait_for_next_tick()
        print("\nAfter Phase B (write burst):")
        print_status(client, monitor, COLLECTION, expected=SAFE_SETTINGS)

        pause_for_window_to_clear()

        phase_mixed(client)
        wait_for_next_tick()
        print("\nAfter Phase C (mixed/moderate):")
        print_status(client, monitor, COLLECTION, expected=MODERATE_SETTINGS)

        banner("DEMO COMPLETE - the settings above changed automatically, with no human touching them")
    finally:
        loop.stop()
        client._db.drop_collection(COLLECTION)
        client.close()


if __name__ == "__main__":
    main()
