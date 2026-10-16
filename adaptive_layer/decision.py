"""Decision Engine: a pure function mapping observed access-pattern Stats
to a MongoDB consistency configuration (write concern / read concern /
read preference).

Deliberately a pure function with zero side effects (no DB access, no
state) - see the project vault's Design Rationale note for why: this is
the piece most likely to be scrutinized, so it needs to be the easiest
piece to exhaustively unit-test. If someone asks "why did it pick majority
write concern here," the answer should be one threshold constant away.

The rule looks at the WRITE SHARE of the window (writes / all operations),
not an absolute writes-per-second rate. An absolute rate does not scale: the
original rule called anything above 5 writes/s "hot", so any busy collection
- even a 5%-write one at 250 ops/s (about 12 writes/s) - was always SAFE, and
FAST was unreachable above roughly 0.2% writes at that load. A share gives
the same decision for the same traffic mix at 10 ops/s or 1000 ops/s.

The default thresholds were fixed before the re-evaluation they are used in
and are not adjusted to its results:
  LOW_WRITE_SHARE  0.15  the old "more than 5 reads per write" rule restated
                         as a share (1/6 is about 0.17), rounded down.
  HIGH_WRITE_SHARE 0.40  writes are at least 2 of every 5 operations; chosen
                         so a 1-write-per-2-reads mix (33%) is MODERATE and a
                         50/50 mix is clearly SAFE.
"""

from dataclasses import dataclass

from adaptive_layer.monitor import Stats

HIGH_WRITE_SHARE = 0.40
LOW_WRITE_SHARE = 0.15


@dataclass(frozen=True)
class Thresholds:
    """Write-share cut-offs for decide(). Strict comparisons: a share exactly
    on a threshold falls into the more moderate side."""

    high_write_share: float = HIGH_WRITE_SHARE  # share above this -> SAFE
    low_write_share: float = LOW_WRITE_SHARE  # share below this -> FAST

    def __post_init__(self):
        if not 0.0 <= self.low_write_share < self.high_write_share <= 1.0:
            raise ValueError(
                "thresholds must satisfy 0 <= low_write_share < high_write_share <= 1, "
                f"got low={self.low_write_share} high={self.high_write_share}"
            )


DEFAULT_THRESHOLDS = Thresholds()


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


def settings_name(settings: Settings) -> str:
    """"SAFE" / "FAST" / "MODERATE" for the three named configurations, else "CUSTOM"."""
    return {SAFE_SETTINGS: "SAFE", FAST_SETTINGS: "FAST", MODERATE_SETTINGS: "MODERATE"}.get(settings, "CUSTOM")


def decide(stats: Stats, thresholds: Thresholds = DEFAULT_THRESHOLDS) -> Settings:
    """Map observed Stats to a Settings choice.

    A window with no operations at all gives MODERATE: there is no evidence
    for either extreme, and an empty window would otherwise look like a 0%
    write share.
    """
    if stats.read_count + stats.write_count == 0:
        return MODERATE_SETTINGS

    if stats.write_share > thresholds.high_write_share:
        return SAFE_SETTINGS

    if stats.write_share < thresholds.low_write_share:
        return FAST_SETTINGS

    return MODERATE_SETTINGS
