"""Decision Engine: a pure function mapping observed access-pattern Stats
to a MongoDB consistency configuration (write concern / read concern /
read preference).

Deliberately a pure function with zero side effects (no DB access, no
state) - see the project vault's Design Rationale note for why: this is
the piece most likely to be scrutinized, so it needs to be the easiest
piece to exhaustively unit-test. If someone asks "why did it pick majority
write concern here," the answer should be one threshold constant away.

Threshold values below are placeholders. Phase 12 (Tuning & Hardening)
replaces them with values backed by real benchmark evidence instead of
guesses - don't read too much into the exact numbers yet.
"""

from dataclasses import dataclass

from adaptive_layer.monitor import Stats

# Writes/sec at or above this is considered "hot" - prioritize safety.
HIGH_WRITE_THRESHOLD = 5.0

# Read:write ratio above this is considered "read-heavy".
READ_HEAVY_RATIO_THRESHOLD = 5.0

# Writes/sec below this is considered "cold" (low write activity).
LOW_WRITE_THRESHOLD = 0.5


@dataclass(frozen=True)
class Settings:
    """A MongoDB consistency configuration. Field values match exactly what
    AdaptiveClient.write()/read() already accept (see adaptive_layer/client.py):
    write_concern is "majority" or an int (pymongo's `w` parameter), read_concern
    is a ReadConcern level string, read_preference is one of the string keys
    AdaptiveClient understands (primary/primaryPreferred/secondary/
    secondaryPreferred/nearest)."""

    write_concern: str | int
    read_concern: str
    read_preference: str


# The three named configurations the engine chooses between. Defined once,
# module-level, so tests and callers can compare against them directly
# instead of constructing new Settings() by hand and hoping the fields match.
SAFE_SETTINGS = Settings(write_concern="majority", read_concern="majority", read_preference="primary")
FAST_SETTINGS = Settings(write_concern=1, read_concern="local", read_preference="nearest")
MODERATE_SETTINGS = Settings(write_concern=1, read_concern="local", read_preference="primaryPreferred")


def decide(stats: Stats) -> Settings:
    """Map observed Stats to a Settings choice.

    Branch order matters: hot/write-heavy is checked first and wins even if
    the collection also happens to look read-heavy (e.g. right after a burst
    of writes with reads mixed in) - safety takes priority over speed when
    both signals are present at once.
    """
    if stats.write_freq > HIGH_WRITE_THRESHOLD:
        return SAFE_SETTINGS

    if stats.read_write_ratio > READ_HEAVY_RATIO_THRESHOLD and stats.write_freq < LOW_WRITE_THRESHOLD:
        return FAST_SETTINGS

    return MODERATE_SETTINGS
