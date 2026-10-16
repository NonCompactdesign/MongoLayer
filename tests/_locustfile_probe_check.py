"""Run by tests/test_locustfile_probe.py in a SEPARATE process (importing the
locustfile monkey-patches with gevent, which must not leak into pytest).

Drives the real AdaptiveMongoUser task with a fake client that never finds a
just-written document by _id (every probe is stale) and checks the bookkeeping.
Prints PROBE-CHECK OK on success; any failed assertion exits non-zero."""

import os
import random
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from locust.env import Environment  # noqa: E402

import benchmark.locustfile as lf  # noqa: E402
from adaptive_layer.decision import FAST_SETTINGS  # noqa: E402
from benchmark.probe_log import ProbeTally  # noqa: E402


class Result:
    def __init__(self, inserted_id):
        self.inserted_id = inserted_id


class FakeClient:
    def __init__(self):
        self.writes, self.reads, self.id_reads = 0, 0, 0

    def write(self, collection, doc):
        self.writes += 1
        return Result(self.writes)

    def read(self, collection, query):
        self.reads += 1
        if "_id" in query:
            self.id_reads += 1
            return None  # the just-written document is never visible: always stale
        return {"key": query["key"]}

    def get_active_settings(self, collection):
        return FAST_SETTINGS


assert lf.PROBE_FRACTION == 0.5, lf.PROBE_FRACTION  # set by the test through the environment

env = Environment()
env.shared_client = FakeClient()
env.probe_tally = ProbeTally()
names = Counter()
failures = []
env.events.request.add_listener(lambda request_type, name, exception, **kw: (names.update([name]), failures.append(exception) if exception else None))

user = lf.AdaptiveMongoUser(env)
random.seed(7)
N = 2000
for _ in range(N):
    user.read_or_write()

probes, stale = env.probe_tally.totals()["FAST"]
client = env.shared_client

assert 800 < probes < 1200, probes  # about half of the iterations
assert stale == probes  # the fake never finds the document
assert client.id_reads == probes  # every probe read is by _id, right after its write
assert names["probe_read"] == probes
assert names["write"] >= probes  # the probe's own write is reported as an ordinary write
assert not failures  # stale probes are a measurement, not Locust failures
assert names["write"] + names["read"] + names["probe_read"] == client.writes + client.reads
print("PROBE-CHECK OK", probes, "probes of", N, "iterations")
