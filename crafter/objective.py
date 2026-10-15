"""Positive-only minimization objective used by the Crafter smoke model."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ObjectiveWeights:
    tardiness: int = 100
    late_order: int = 10_000
    makespan: int = 1
    load_imbalance: int = 1

    def __post_init__(self):
        values = (self.tardiness, self.late_order, self.makespan, self.load_imbalance)
        if any(v < 0 for v in values):
            raise ValueError("Crafter minimization penalty weights must be non-negative")


def scalar_objective(total_priority_tardiness: int, late_order_count: int,
                     makespan_seconds: int, load_imbalance: int,
                     weights: ObjectiveWeights = ObjectiveWeights()) -> int:
    """Pure arithmetic mirror used for sign-regression tests and reporting."""
    terms = (total_priority_tardiness, late_order_count, makespan_seconds, load_imbalance)
    if any(v < 0 for v in terms):
        raise ValueError("objective components must be non-negative")
    return (
        weights.tardiness * total_priority_tardiness
        + weights.late_order * late_order_count
        + weights.makespan * makespan_seconds
        + weights.load_imbalance * load_imbalance
    )
