"""Adapter around the existing authoritative deterministic worker allocator."""
from __future__ import annotations

from datetime import timedelta

from ga.worker_allocation import allocate_crew

from .contracts import (
    CrafterPhenotype, CrafterStatus, PhenotypeOperation, WorkerFailure,
)


def _initial_reservations(problem):
    out = {}
    for row in problem.resources.fixed_worker_commitments:
        out.setdefault(str(row.employee_id), []).append((
            problem.origin + timedelta(seconds=row.start_seconds),
            problem.origin + timedelta(seconds=row.end_seconds),
        ))
    return out


def allocate_exact_workers(problem, solution, operator_index):
    """Materialize exact workers without modifying allocate_crew().

    A worker allocation is accepted only when it begins at the master
    productive-ready instant and completes at the master end.  Any calendar
    or reservation delay is a structured decomposition failure, not a hidden
    mutation of the CP-SAT schedule.
    """
    if solution.status not in (CrafterStatus.MASTER_FEASIBLE,
                               CrafterStatus.MACHINE_FEASIBLE):
        return CrafterPhenotype(solution.status, ())
    reservations = _initial_reservations(problem)
    busy_minutes = None
    operations = []
    op_by_key = problem.operation_by_key
    ordered = sorted(
        solution.assignments,
        key=lambda x: (x.start_seconds, op_by_key[x.operation_key].sequence, x.operation_key),
    )
    for assignment in ordered:
        spec = op_by_key[assignment.operation_key]
        ready = problem.origin + timedelta(seconds=assignment.productive_start_seconds)
        expected_end = problem.origin + timedelta(seconds=assignment.end_seconds)
        crew = allocate_crew(
            operator_index=operator_index,
            optype=spec.required_capability,
            line_id=assignment.line_id,
            headcount=assignment.machine_count,
            productive_ready=ready,
            productive_minutes=assignment.productive_seconds / 60.0,
            reservations=reservations,
            busy_minutes=busy_minutes,
            priority=float(problem.order_by_id[assignment.order_id].priority_weight > 1),
        )
        shortage = crew.get("shortage")
        if shortage is not None:
            failure = WorkerFailure(
                operation_key=assignment.operation_key,
                line_id=assignment.line_id,
                machine_count=assignment.machine_count,
                start_seconds=assignment.start_seconds,
                end_seconds=assignment.end_seconds,
                required_skill=spec.required_capability,
                required_workers=assignment.machine_count,
                available_workers=int(shortage.get("available", 0)),
                reason=str(shortage.get("limiting_factor", "unknown")),
                machine_ids=assignment.machine_ids,
            )
            return CrafterPhenotype(
                CrafterStatus.MASTER_FEASIBLE_WORKER_INFEASIBLE,
                tuple(operations), worker_failure=failure,
            )
        segments = tuple(crew["segments"])
        first_start = segments[0][0] if segments else ready
        actual_end = crew["end"]
        if first_start != ready or actual_end != expected_end:
            failure = WorkerFailure(
                operation_key=assignment.operation_key,
                line_id=assignment.line_id,
                machine_count=assignment.machine_count,
                start_seconds=assignment.start_seconds,
                end_seconds=assignment.end_seconds,
                required_skill=spec.required_capability,
                required_workers=assignment.machine_count,
                available_workers=len(crew.get("employees", ())),
                reason="calendar-or-reservation-delayed-master-envelope",
                machine_ids=assignment.machine_ids,
                actual_worker_completion_seconds=int(
                    round((actual_end - problem.origin).total_seconds())),
                extension_required_seconds=max(
                    0, int(round((actual_end - expected_end).total_seconds()))),
            )
            return CrafterPhenotype(
                CrafterStatus.MASTER_FEASIBLE_WORKER_INFEASIBLE,
                tuple(operations), worker_failure=failure,
            )
        reservations = crew["reservations"]
        busy_minutes = crew["busy_minutes"]
        operations.append(PhenotypeOperation(
            assignment=assignment,
            employee_ids=tuple(str(x) for x in crew["employees"]),
            worker_segments=tuple(
                (str(employee_id), segments)
                for employee_id in crew["employees"]
            ),
            sourcing_tier=str(crew["tier"]),
        ))
    return CrafterPhenotype(CrafterStatus.WORKER_FEASIBLE, tuple(operations))
