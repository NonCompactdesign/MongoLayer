"""A lock that stays a plain OS lock even when gevent has monkey-patched `threading`.

The benchmark runs the layer inside Locust, which monkey-patches `threading` with gevent, so
`threading.Lock()` becomes a gevent semaphore. When Locust stops a test it kills every simulated
user; a user killed around one of these locks raised "Semaphore released too many times", the
user's request code swallowed that error, and the user kept running after the test should have
ended (one 180 s run hung for 6 hours on 2026-10-19).

The locked sections in this package (monitor.record/get_stats, the client's recent-writes
memory) only touch dicts and deques: nothing inside them does I/O or yields
to another greenlet. Under gevent (one OS thread, cooperative switching) such a section can never
be interrupted, so the original OS lock is never contended there and never blocks the hub. With
real threads it is an ordinary lock.
"""

import sys
import threading


def native_lock():
    if "gevent.monkey" in sys.modules:
        from gevent import monkey

        if monkey.is_module_patched("threading"):
            return monkey.get_original("threading", "Lock")()
    return threading.Lock()
