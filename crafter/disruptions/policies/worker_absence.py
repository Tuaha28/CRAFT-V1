from __future__ import annotations

from .policy_result import RecoveryCandidate, RecoveryDecision
from .recovery_levels import RecoveryLevel


def worker_recovery_candidates(problem, operation, event, *, incumbent_line: int, incumbent_k: int,
                               incumbent_worker_ids=(), same_line_workers=(), borrowed_workers=(),
                               replacement_start: int | None = None,
                               legal_k_values=None) -> RecoveryDecision:
    absent = {str(x) for x in event.worker_ids}
    candidates = []
    same = tuple(str(x) for x in same_line_workers if str(x) not in absent)
    if len(same) >= incumbent_k:
        candidates.append(RecoveryCandidate(operation.operation_key,
            RecoveryLevel.SAME_LINE_RESOURCE_SUBSTITUTION, "SAME_LINE_WORKER_REPLACEMENT",
            incumbent_line, incumbent_k, worker_ids=tuple(same[:incumbent_k]),
            evidence={"absent_workers": sorted(absent), "scope": "same_line"}))
    borrowed = tuple(str(x) for x in borrowed_workers if str(x) not in absent)
    if len(borrowed) >= incumbent_k:
        candidates.append(RecoveryCandidate(operation.operation_key,
            RecoveryLevel.CROSS_LINE_RESOURCE_BORROWING, "BORROWED_WORKER",
            incumbent_line, incumbent_k, worker_ids=tuple(borrowed[:incumbent_k]),
            evidence={"absent_workers": sorted(absent), "scope": "cross_line"}))
    if legal_k_values is None:
        legal_values = range(min(incumbent_k - 1, int(getattr(operation, "max_machine_count", incumbent_k))),
                             int(getattr(operation, "min_machine_count", 1)) - 1, -1)
    else:
        legal_values = sorted({int(value) for value in legal_k_values
                               if 0 < int(value) < int(incumbent_k)}, reverse=True)
    for k in legal_values:
        if k > 0 and len(same) >= k:
            candidates.append(RecoveryCandidate(operation.operation_key,
                RecoveryLevel.SAME_LINE_K_ADJUSTMENT, "SAME_LINE_K_REDUCTION",
                incumbent_line, k, worker_ids=tuple(same[:k]),
                evidence={"incumbent_k": incumbent_k}))
    if replacement_start is not None and (same or borrowed):
        workers = tuple((same or borrowed)[:incumbent_k])
        if len(workers) == incumbent_k:
            candidates.append(RecoveryCandidate(operation.operation_key,
                RecoveryLevel.TEMPORAL_RECOVERY, "WAIT_FOR_WORKER_RETURN",
                incumbent_line, incumbent_k, worker_ids=workers,
                start_lower_bound=int(replacement_start),
                evidence={"replacement_start": int(replacement_start)}))
    return RecoveryDecision(operation.operation_key, event.event_id, tuple(candidates),
                            None if candidates else "NO_SAME_LINE_WORKER_REPLACEMENT")
