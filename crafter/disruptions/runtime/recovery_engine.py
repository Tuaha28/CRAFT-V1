"""Event-specific policy evaluation before executable-mode generation."""
from __future__ import annotations

from dataclasses import dataclass

from ..events import EventType
from ..policies.base import machine_recovery_candidates
from ..policies.worker_absence import worker_recovery_candidates
from ..policies.predecessor_delay import propagate_predecessor_delay


@dataclass(frozen=True)
class RecoveryEngine:
    """Thin policy facade; physical feasibility remains in JointExecutableModes."""

    def evaluate_machine(self, problem, operation, event, *, incumbent_line, incumbent_k,
                         incumbent_machine_ids, legal_k_values=(), repair_time=None):
        return machine_recovery_candidates(
            problem, operation, event, incumbent_line, incumbent_k,
            incumbent_machine_ids, repair_time=repair_time,
            legal_k_values=legal_k_values)

    def evaluate_worker(self, problem, operation, event, *, incumbent_line, incumbent_k,
                        incumbent_worker_ids, same_line_workers=(), borrowed_workers=(),
                        legal_k_values=(), replacement_start=None):
        return worker_recovery_candidates(
            problem, operation, event, incumbent_line=incumbent_line,
            incumbent_k=incumbent_k, incumbent_worker_ids=incumbent_worker_ids,
            same_line_workers=same_line_workers, borrowed_workers=borrowed_workers,
            legal_k_values=legal_k_values, replacement_start=replacement_start)

    def evaluate_predecessor(self, index, predecessor_key, delayed_end_seconds):
        return propagate_predecessor_delay(index, predecessor_key, delayed_end_seconds)
