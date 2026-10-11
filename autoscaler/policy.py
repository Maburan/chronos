"""The scaling policy: a pure function from backlog to a desired replica
count. No I/O, no state, no clock -- given the same backlog it always
returns the same number, which is what makes it trivially unit-testable
and interview-explainable.

The default is a simple step/tier table (chosen defaults, all
configurable):

    backlog 0-2   -> 1 replica
    backlog 3-5   -> 2 replicas
    backlog 6-10  -> 3 replicas
    backlog 11-20 -> 4 replicas
    backlog 21+   -> 5 replicas

Represented as an ordered list of ``(min_backlog_inclusive, replicas)``
tiers, so the shape is data (configurable via
``CHRONOS_AUTOSCALER_BACKLOG_TIERS`` -- see config.py) rather than
hardcoded ``if`` branches scattered around. The result is always clamped
to ``[min_replicas, max_replicas]``; the tiers themselves never override
that hard bound.

Deliberately NOT in the first version (documented as future work, not
oversight): oldest-queued-task-age as an additional signal. The interface
below takes only ``backlog`` today, but because the policy is a single
pure function the age signal can later be added as a second parameter
without touching the reconcile loop, the runtime, or any test of those.
"""

from __future__ import annotations

from dataclasses import dataclass

# The documented default tier table (see module docstring). Kept as a
# module constant so config.py and tests reference exactly one definition.
DEFAULT_BACKLOG_TIERS: tuple[tuple[int, int], ...] = (
    (0, 1),
    (3, 2),
    (6, 3),
    (11, 4),
    (21, 5),
)


@dataclass(frozen=True)
class ScalingPolicy:
    """``tiers`` must be sorted ascending by ``min_backlog`` (config.py
    guarantees this) -- ``desired_replicas`` walks them and takes the
    replica count of the highest tier whose threshold the backlog reaches.
    ``min_replicas``/``max_replicas`` are the inviolable clamp applied
    after the tier lookup."""

    min_replicas: int = 1
    max_replicas: int = 5
    tiers: tuple[tuple[int, int], ...] = DEFAULT_BACKLOG_TIERS

    def __post_init__(self) -> None:
        if self.min_replicas < 0:
            raise ValueError("min_replicas must be >= 0")
        if self.max_replicas < self.min_replicas:
            raise ValueError("max_replicas must be >= min_replicas")
        if not self.tiers:
            raise ValueError("tiers must not be empty")

    def desired_replicas(self, backlog: int) -> int:
        """Backlog (count of READY tasks for one task type) -> desired
        replica count, clamped to ``[min_replicas, max_replicas]``. Never
        raises for a negative backlog (treated as 0) -- a control loop
        should degrade to the floor, not crash."""
        effective_backlog = max(0, backlog)
        replicas = self.min_replicas
        for min_backlog, tier_replicas in self.tiers:
            if effective_backlog >= min_backlog:
                replicas = tier_replicas
            else:
                break
        return max(self.min_replicas, min(self.max_replicas, replicas))
