from __future__ import annotations

from typing import Iterable

from crafter.lns_probe.line_k_model import _available_machine_pool

from ..events import DisruptionEvent
from .policy_result import RecoveryCandidate, RecoveryDecision
from .recovery_levels import RecoveryLevel


def _machine_pool(problem, operation, line_id: int, blocked: Iterable[int] = ()) -> tuple[int, ...]:
    blocked = {int(x) for x in blocked}
    return tuple(x for x in _available_machine_pool(problem, operation, int(line_id)) if x not in blocked)


def _legal_k_values(operation, incumbent_k: int,
                    legal_k_values: Iterable[int] | None = None) -> tuple[int, ...]:
    if legal_k_values is not None:
        values = sorted({int(value) for value in legal_k_values if int(value) > 0}, reverse=True)
        return tuple(value for value in values if value <= max(1, int(incumbent_k)))
    lo = int(getattr(operation, "min_machine_count", 1))
    hi = min(int(getattr(operation, "max_machine_count", incumbent_k)), max(1, int(incumbent_k)))
    return tuple(range(hi, lo - 1, -1))


def machine_recovery_candidates(problem, operation, event: DisruptionEvent,
                                incumbent_line: int, incumbent_k: int,
                                incumbent_machine_ids: Iterable[int], *, repair_time: int | None = None,
                                legal_k_values: Iterable[int] | None = None) -> RecoveryDecision:
    """Return ordered same-line/k/wait/cross-line evidence.

    This is policy screening only; executable modes still pass through the
    authoritative JointExecutableMode generator and validator.
    """
    broken = set(int(x) for x in event.machine_ids)
    incumbent = tuple(int(x) for x in incumbent_machine_ids)
    candidates: list[RecoveryCandidate] = []
    same_pool = _machine_pool(problem, operation, incumbent_line, broken)
    if len(same_pool) >= incumbent_k:
        machines = tuple(same_pool[:incumbent_k])
        candidates.append(RecoveryCandidate(operation.operation_key,
            RecoveryLevel.SAME_LINE_RESOURCE_SUBSTITUTION,
            "SAME_LINE_MACHINE_SUBSTITUTION", incumbent_line, incumbent_k,
            machines, evidence={"pool": list(same_pool), "blocked": sorted(broken)}))
    for k in _legal_k_values(operation, incumbent_k, legal_k_values):
        if k >= incumbent_k:
            continue
        if len(same_pool) >= k:
            candidates.append(RecoveryCandidate(operation.operation_key,
                RecoveryLevel.SAME_LINE_K_ADJUSTMENT, "SAME_LINE_K_REDUCTION",
                incumbent_line, k, tuple(same_pool[:k]),
                evidence={"pool": list(same_pool), "incumbent_k": incumbent_k}))
    if repair_time is not None and len(_machine_pool(problem, operation, incumbent_line, ())) >= incumbent_k:
        candidates.append(RecoveryCandidate(operation.operation_key,
            RecoveryLevel.TEMPORAL_RECOVERY, "WAIT_FOR_MACHINE_RETURN",
            incumbent_line, incumbent_k, incumbent,
            start_lower_bound=int(repair_time),
            evidence={"repair_time": int(repair_time)}))
    for line_id in sorted(int(x) for x in operation.eligible_line_ids):
        if line_id == incumbent_line:
            continue
        pool = _machine_pool(problem, operation, line_id, broken)
        if len(pool) >= incumbent_k:
            candidates.append(RecoveryCandidate(operation.operation_key,
                RecoveryLevel.CROSS_LINE_OPERATION_MOVE, "CROSS_LINE_OPERATION_MOVE",
                line_id, incumbent_k, tuple(pool[:incumbent_k]),
                evidence={"pool": list(pool), "incumbent_line": incumbent_line}))
    failure = None if candidates else "NO_LOCAL_RECOVERY"
    return RecoveryDecision(operation.operation_key, event.event_id, tuple(candidates), failure)
