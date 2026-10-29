"""The demo scenarios as one-click presets: name -> (label, hint, knob settings).

A button can be pressed in any order, so every preset sets the whole scenario (BASE plus its own
changes), not just the one or two knobs it is about.
Only Hysteresis still depends on what is active: it shows SAFE holding, so it needs SAFE first."""

BASE = dict(write_share=0.05, ops_per_sec=200, hot_keys=0, key_space=1000, probe_fraction=0.03,
            read_own_writes=True, topology="EQUAL-0", paused=False)


def _preset(label, hint, **changes):
    return label, hint, {**BASE, **changes}


PRESETS = {
    "read_heavy": _preset("Read-heavy", "5% writes on equal distances: FAST."),
    "modest": _preset("Modest writes", "25% writes: MODERATE.", write_share=0.25),
    "balanced": _preset("Balanced", "50% writes: SAFE (cheap on equal distances).", write_share=0.5),
    "hysteresis": _preset("Hysteresis", "Press Balanced first and wait for SAFE: at 37% it stays SAFE "
                          "(leaves only below 35%). From anything else it is plain MODERATE.", write_share=0.37),
    "far_primary": _preset("Far primary", "T1, 5% writes: FAST reads go local, writes ~88 ms "
                           "(pymongo needs 10-30 s to notice new distances).", topology="T1"),
    "staleness": _preset("Staleness", "T1, 5% writes, read-your-own-writes off: FAST reads a secondary "
                         "that lags, so immediate read-backs miss.", topology="T1", read_own_writes=False),
    "ryw_on": _preset("RYW back on", "T1, 5% writes, read-your-own-writes on: own-write reads go to the "
                      "primary, stale rate returns to 0 (the 15 s window still holds older probes).", topology="T1"),
    "budget": _preset("Latency budget", "T1, 50% writes: SAFE writes ~260 ms > 100 ms budget -> MODERATE, "
                      "retried after 60 s.", write_share=0.5, topology="T1"),
    "hot_keys": _preset("Hot keys", "T1, 80% of operations on 20 keys: many reads redirected to the primary.",
                        hot_keys=20, topology="T1"),
    "regional": _preset("Regional pair", "T2, 50% writes, primary 10 ms away: SAFE is affordable "
                        "(after any T1 budget backoff, up to 60 s).", write_share=0.5, topology="T2"),
    "reset": _preset("Back to start", "Equal distances, 5% writes, 200 ops/s, uniform keys."),
}


def as_list():
    return [{"name": name, "label": label, "hint": hint, "changes": changes}
            for name, (label, hint, changes) in PRESETS.items()]
