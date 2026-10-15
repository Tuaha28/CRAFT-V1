from __future__ import annotations

from enum import IntEnum


class RecoveryLevel(IntEnum):
    """Ordered minimum-disruption recovery ladder."""

    INCUMBENT_PRESERVATION = 0
    SAME_LINE_RESOURCE_SUBSTITUTION = 1
    SAME_LINE_K_ADJUSTMENT = 2
    TEMPORAL_RECOVERY = 3
    CROSS_LINE_RESOURCE_BORROWING = 4
    CROSS_LINE_OPERATION_MOVE = 5
    BROADER_REPAIR = 6
