"""Exact physical-machine materialization for fixed V2 master envelopes."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from ortools.sat.python import cp_model

from .contracts import (
    CrafterStatus, MachineFailure, MachineMaterializationResult,
)


def _overlap(left_start, left_end, right_start, right_end):
    return left_start < right_end and right_start < left_end


def materialize_machines(problem, solution, *, time_limit_seconds=10.0,
                         random_seed=42):
    """Assign exact IDs without moving any master start/end envelope.

    The subproblem contains only Boolean assignment variables.  Every master
    interval includes Crafter's conservative post-operation buffer, so an ID
    chosen here cannot overlap another buffered use or a fixed commitment.
    """
    if solution.status != CrafterStatus.MASTER_FEASIBLE:
        return MachineMaterializationResult(solution.status, solution)

    model = cp_model.CpModel()
    candidates, use = {}, {}
    fixed_by_machine = defaultdict(list)
    for fixed in problem.resources.fixed_machine_commitments:
        fixed_by_machine[fixed.machine_id].append((
            int(fixed.start_seconds),
            int(fixed.end_seconds+problem.changeover_seconds), fixed))

    ordered = sorted(solution.assignments,
                     key=lambda row: (row.start_seconds, row.end_seconds,
                                      row.operation_key))
    for assignment in ordered:
        spec = problem.operation_by_key[assignment.operation_key]
        ids = tuple(sorted(
            machine.machine_id for machine in problem.machines
            if machine.is_usable
            and machine.factory_id == problem.order_by_id[assignment.order_id].factory_id
            and machine.line_id == assignment.line_id
            and machine.supports(spec.required_capability)
        ))
        candidates[assignment.operation_key] = ids
        if len(ids) < assignment.machine_count:
            failure = MachineFailure(
                assignment.operation_key, assignment.line_id,
                assignment.machine_count, assignment.start_seconds,
                assignment.end_seconds, spec.required_capability, ids, (),
                "insufficient-compatible-physical-machines")
            failed = replace(solution,
                status=CrafterStatus.MASTER_FEASIBLE_MACHINE_INFEASIBLE,
                diagnostics=solution.diagnostics+(failure.reason,))
            return MachineMaterializationResult(failed.status, failed, failure)
        literals = []
        for machine_id in ids:
            literal = model.NewBoolVar(f"use_{assignment.operation_key}_m{machine_id}")
            use[(assignment.operation_key, machine_id)] = literal
            literals.append(literal)
            envelope_end = assignment.end_seconds+problem.changeover_seconds
            if any(_overlap(assignment.start_seconds, envelope_end, start, end)
                   for start, end, _fixed in fixed_by_machine[machine_id]):
                model.Add(literal == 0)
        model.Add(sum(literals) == assignment.machine_count)

    # Fixed-time interval graph conflicts preserve multi-capability identity:
    # the same physical ID cannot be selected by two overlapping operations,
    # even when their required capabilities differ.
    by_machine = defaultdict(list)
    for assignment in ordered:
        for machine_id in candidates[assignment.operation_key]:
            by_machine[machine_id].append(assignment)
    for machine_id, rows in by_machine.items():
        for index, left in enumerate(rows):
            left_end = left.end_seconds+problem.changeover_seconds
            for right in rows[index+1:]:
                if _overlap(left.start_seconds, left_end, right.start_seconds,
                            right.end_seconds+problem.changeover_seconds):
                    model.Add(
                        use[(left.operation_key, machine_id)]
                        + use[(right.operation_key, machine_id)] <= 1)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = int(random_seed)
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # Pick the most constrained master operation for a reproducible,
        # structured diagnosis.  The complete CP subproblem status is retained.
        constrained = min(ordered, key=lambda row: (
            len(candidates[row.operation_key])-row.machine_count,
            row.start_seconds, row.operation_key))
        ids = candidates[constrained.operation_key]
        available = tuple(machine_id for machine_id in ids if not any(
            _overlap(constrained.start_seconds,
                     constrained.end_seconds+problem.changeover_seconds,
                     start, end)
            for start, end, _fixed in fixed_by_machine[machine_id]))
        spec = problem.operation_by_key[constrained.operation_key]
        failure = MachineFailure(
            constrained.operation_key, constrained.line_id,
            constrained.machine_count, constrained.start_seconds,
            constrained.end_seconds, spec.required_capability, ids, available,
            f"physical-assignment-{solver.StatusName(status).lower()}")
        failed = replace(solution,
            status=CrafterStatus.MASTER_FEASIBLE_MACHINE_INFEASIBLE,
            diagnostics=solution.diagnostics+(failure.reason,))
        return MachineMaterializationResult(
            failed.status, failed, failure,
            diagnostics=(f"materializer_status={solver.StatusName(status)}",))

    materialized = []
    for assignment in solution.assignments:
        selected = tuple(machine_id for machine_id in candidates[assignment.operation_key]
                         if solver.BooleanValue(
                             use[(assignment.operation_key, machine_id)]))
        materialized.append(replace(assignment, machine_ids=selected))
    exact = replace(
        solution, status=CrafterStatus.MACHINE_FEASIBLE,
        assignments=tuple(materialized),
        diagnostics=solution.diagnostics+(
            "MACHINE_FEASIBLE: exact physical IDs assigned with buffered no-overlap.",))
    return MachineMaterializationResult(
        CrafterStatus.MACHINE_FEASIBLE, exact,
        diagnostics=(f"materializer_status={solver.StatusName(status)}",
                     f"assignment_literals={len(use)}",))


def machine_equivalence_classes(problem):
    """Exact symmetry classes; no machines with different state are merged."""
    fixed_by_machine = defaultdict(list)
    for fixed in problem.resources.fixed_machine_commitments:
        fixed_by_machine[fixed.machine_id].append((
            fixed.start_seconds, fixed.end_seconds, fixed.production_order_id,
            getattr(fixed, "required_capability", None)))
    groups = defaultdict(list)
    for machine in problem.machines:
        key = (
            machine.factory_id, machine.line_id,
            tuple(sorted(x.strip().lower() for x in machine.capabilities)),
            bool(machine.is_usable),
            tuple(sorted(fixed_by_machine.get(machine.machine_id, ()))),
        )
        groups[key].append(machine.machine_id)
    return tuple(sorted(
        ((key, tuple(sorted(ids))) for key, ids in groups.items()),
        key=lambda row: repr(row[0])))
