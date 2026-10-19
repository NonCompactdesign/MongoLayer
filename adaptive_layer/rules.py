"""Rules on top of the traffic rule (decision.py). Pure functions, no I/O.

The traffic rule proposes FAST / MODERATE / SAFE from the write share (with hysteresis when
SIGNALS_THRESHOLDS is used). Two rules can then change that proposal:

  budget  (9) SAFE is a bet that a majority write is affordable. When the measured p95 latency
              of SAFE writes is above the budget, SAFE steps down to MODERATE (w=1, primary
              reads): durability is traded for latency, on purpose.
  policy  (5) a per-collection floor / ceiling, applied last, so the application has the final
              word: a collection marked floor="SAFE" stays SAFE whatever the budget says.

Every rule that applies adds a reason, so each switch says why it happened (10).
"""

from dataclasses import dataclass

from adaptive_layer.decision import (
    BY_NAME,
    DEFAULT_THRESHOLDS,
    LEVELS,
    MODERATE_SETTINGS,
    SAFE_SETTINGS,
    Settings,
    Thresholds,
    decide_explained,
    settings_name,
)
from adaptive_layer.monitor import Stats

# The traffic rule's cut-offs plus 5 points of hysteresis (a third of the 15-point FAST zone):
# SAFE is entered above 0.40 and left below 0.35; FAST is entered below 0.15 and left above 0.20.
SIGNALS_THRESHOLDS = Thresholds(hysteresis=0.05)


@dataclass(frozen=True)
class SignalConfig:
    """The latency budget's limits. Fixed before the runs that report on them."""

    write_p95_budget_ms: float | None = 100.0  # the classic "feels instant" limit; None disables
    budget_min_samples: int = 20  # fewer SAFE writes than this in the window: not enough evidence
    budget_backoff_seconds: float = 60.0  # how long a breach keeps SAFE off (see FeedbackLoop)


@dataclass(frozen=True)
class CollectionPolicy:
    """Limits the application puts on one collection: floor="SAFE" means never
    weaker than SAFE, ceiling="MODERATE" means never FAST, and so on."""

    floor: str | None = None
    ceiling: str | None = None

    def __post_init__(self):
        for name in (self.floor, self.ceiling):
            if name is not None and name not in LEVELS:
                raise ValueError(f"policy levels must be one of {LEVELS}, got {name!r}")
        if self.floor and self.ceiling and LEVELS.index(self.floor) > LEVELS.index(self.ceiling):
            raise ValueError(f"floor {self.floor} is above ceiling {self.ceiling}")

    def clamp(self, settings: Settings) -> tuple[Settings, str | None]:
        name = settings_name(settings)
        if name not in LEVELS:
            return settings, None
        level = LEVELS.index(name)
        if self.floor and level < LEVELS.index(self.floor):
            return BY_NAME[self.floor], f"policy floor {self.floor}"
        if self.ceiling and level > LEVELS.index(self.ceiling):
            return BY_NAME[self.ceiling], f"policy ceiling {self.ceiling}"
        return settings, None


NO_POLICY = CollectionPolicy()


@dataclass(frozen=True)
class Decision:
    settings: Settings
    reasons: tuple = ()

    @property
    def reason(self):
        return "; ".join(self.reasons)


def apply_rules(
    stats: Stats,
    current: Settings | None = None,
    config: SignalConfig | None = None,
    policy: CollectionPolicy = NO_POLICY,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    safe_write_p95_ms: float | None = None,
) -> Decision:
    """The full decision for one collection: traffic rule -> latency budget -> policy.

    config             None skips the budget (the traffic rule and the policy still apply).
    safe_write_p95_ms  the p95 of SAFE writes the budget should judge (the feedback loop
                       holds a breach for budget_backoff_seconds).
    """
    settings, why = decide_explained(stats, thresholds, current)
    reasons = [why]

    if (config is not None and settings == SAFE_SETTINGS and config.write_p95_budget_ms is not None
            and safe_write_p95_ms is not None and safe_write_p95_ms > config.write_p95_budget_ms):
        settings = MODERATE_SETTINGS
        reasons.append(f"SAFE write p95 {safe_write_p95_ms:.0f} ms > budget {config.write_p95_budget_ms:g} ms -> MODERATE")

    settings, clamp_reason = policy.clamp(settings)
    if clamp_reason:
        reasons.append(f"{clamp_reason} -> {settings_name(settings)}")
    return Decision(settings, tuple(reasons))
