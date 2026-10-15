"""Exact local realization and commit/rollback transaction contract."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timedelta

from ortools.sat.python import cp_model

from crafter.authoritative_semantics import (
    authoritative_changeover_seconds, authoritative_operation_work_minutes,
)
from crafter.contracts import (
    CrafterPhenotype, CrafterStatus, OperationAssignment, PhenotypeOperation,
)
from crafter.e0_5_semantic_reconciliation import _frozen_phenotype
from crafter.phenotype import comparison_metrics, validate
from ga.worker_allocation import allocate_crew, normalize_reservations

from .analysis import seconds


def frozen_incumbent(problem, rows):
    return _frozen_phenotype(problem, rows)


def _machine_accepts(reservations, start, end, order_id):
    for left, right, other_order, key in reservations:
        if start < right and end > left:
            return False
        # Availability/outage intervals are not production orders and carry
        # no setup/changeover semantics.  They block overlap only; the
        # machine is usable at the exact end of the outage.
        if str(key).startswith("event:"):
            continue
        if right <= start:
            gap = authoritative_changeover_seconds(other_order, order_id)
            if right+gap > start:
                return False
        elif end <= left:
            gap = authoritative_changeover_seconds(order_id, other_order)
            if end+gap > left:
                return False
    return True


def materialize_machines(problem, index, mutable_keys, cp_assignment):
    mutable = set(mutable_keys)
    reservations = defaultdict(list)
    for row in index.rows:
        key = str(row["operation_key"])
        if key in mutable:
            continue
        start, end = seconds(row["start_minutes"]), seconds(row["end_minutes"])
        for machine_id in row["machine_ids"]:
            reservations[int(machine_id)].append((
                start, end, str(row["production_order_id"]), key))
    for fixed in problem.resources.fixed_machine_commitments:
        reservations[int(fixed.machine_id)].append((
            int(fixed.start_seconds), int(fixed.end_seconds),
            str(fixed.production_order_id), f"fixed:{fixed.production_order_id}"))
    for values in reservations.values():
        values.sort()

    candidate_by_operation = {}
    required_by_operation = {}
    ordered = sorted(mutable, key=lambda key: (
        cp_assignment[key]["start_seconds"],
        # Allocate scarcer operations first at the same instant.
        index.operation_features[key]["eligible_machine_count"], key))
    for key in ordered:
        operation = problem.operation_by_key[key]
        row = index.by_key[key]
        line_id = int(row["line_id"])
        count = len(set(int(item) for item in row["machine_ids"]))
        start = int(cp_assignment[key]["start_seconds"])
        end = int(cp_assignment[key]["end_seconds"])
        incumbent = [int(item) for item in row["machine_ids"]]
        candidates = [
            machine.machine_id for machine in problem.machines
            if machine.is_usable and machine.factory_id ==
            problem.order_by_id[operation.order_id].factory_id and
            machine.line_id == line_id and
            machine.supports(operation.required_capability)
        ]
        candidates = sorted(set(candidates), key=lambda value: (
            0 if value in incumbent else 1, value))
        available = [machine_id for machine_id in candidates
                     if _machine_accepts(reservations[machine_id], start, end,
                                         operation.order_id)]
        if len(available) < count:
            return {
                "success": False, "operation_key": key,
                "reason": (
                    "proven insufficient exact physical machines after outside "
                    "occupation/changeover filtering"),
                "required": count, "available": len(available),
                "candidate_machine_ids": candidates,
                "available_machine_ids": available,
                "proof": "available < required",
            }, None
        candidate_by_operation[key] = tuple(available)
        required_by_operation[key] = count

    # Complete fixed-time identity assignment. This is deliberately separate
    # from the scheduling master: every Boolean is an exact physical ID, and
    # conflicting local transitions cannot share it.
    model = cp_model.CpModel()
    use = {}
    incumbent_literals = []
    for key in ordered:
        incumbent = {int(value) for value in index.by_key[key]["machine_ids"]}
        literals = []
        for machine_id in candidate_by_operation[key]:
            literal = model.NewBoolVar(f"materialize_{key}_{machine_id}")
            use[(key, machine_id)] = literal
            literals.append(literal)
            if machine_id in incumbent:
                incumbent_literals.append(literal)
        model.Add(sum(literals) == required_by_operation[key])
    pair_constraints = 0
    for left_no, left in enumerate(ordered):
        left_op = problem.operation_by_key[left]
        left_start = int(cp_assignment[left]["start_seconds"])
        left_end = int(cp_assignment[left]["end_seconds"])
        for right in ordered[left_no+1:]:
            shared = set(candidate_by_operation[left]) & set(
                candidate_by_operation[right])
            if not shared:
                continue
            right_op = problem.operation_by_key[right]
            right_start = int(cp_assignment[right]["start_seconds"])
            right_end = int(cp_assignment[right]["end_seconds"])
            if left_end <= right_start:
                compatible = (left_end+authoritative_changeover_seconds(
                    left_op.order_id, right_op.order_id) <= right_start)
            elif right_end <= left_start:
                compatible = (right_end+authoritative_changeover_seconds(
                    right_op.order_id, left_op.order_id) <= left_start)
            else:
                compatible = False
            if compatible:
                continue
            for machine_id in shared:
                model.Add(use[(left, machine_id)]+use[(right, machine_id)] <= 1)
                pair_constraints += 1
    if incumbent_literals:
        model.Maximize(sum(incumbent_literals))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5.0
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "success": False, "operation_key": None,
            "reason": "exact physical-machine identity assignment failed",
            "cp_status": solver.StatusName(status),
            "variables": len(model.Proto().variables),
            "constraints": len(model.Proto().constraints),
            "pair_conflict_constraints": pair_constraints,
        }, None
    materialized = {
        key: tuple(sorted(machine_id for machine_id in candidate_by_operation[key]
                          if solver.Value(use[(key, machine_id)])))
        for key in ordered
    }
    return {
        "success": True, "operations": len(materialized),
        "cp_status": solver.StatusName(status),
        "variables": len(model.Proto().variables),
        "constraints": len(model.Proto().constraints),
        "pair_conflict_constraints": pair_constraints,
    }, materialized


def _outside_worker_state(problem, index, mutable):
    reservations = defaultdict(list)
    for row in index.rows:
        if str(row["operation_key"]) in mutable:
            continue
        for employee, segments in row.get(
                "worker_segments_by_employee", {}).items():
            for pair in segments:
                start, end = pair if not isinstance(pair, dict) else (
                    pair["start"], pair["end"])
                reservations[str(employee)].append((
                    datetime.fromisoformat(str(start)),
                    datetime.fromisoformat(str(end))))
    for fixed in problem.resources.fixed_worker_commitments:
        reservations[str(fixed.employee_id)].append((
            problem.origin+timedelta(seconds=fixed.start_seconds),
            problem.origin+timedelta(seconds=fixed.end_seconds)))
    return normalize_reservations(reservations)


def allocate_workers(problem, operator_index, op_details, index,
                     mutable_keys, cp_assignment, machine_ids):
    mutable = set(mutable_keys)
    reservations = _outside_worker_state(problem, index, mutable)
    busy = None
    realized = {}
    ordered = sorted(mutable, key=lambda key: (
        cp_assignment[key]["productive_start_seconds"],
        problem.operation_by_key[key].sequence, key))
    for key in ordered:
        operation = problem.operation_by_key[key]
        row = index.by_key[key]
        k = len(machine_ids[key])
        _setup_minutes, productive_minutes = authoritative_operation_work_minutes(
            op_details[key], k)
        ready = problem.origin+timedelta(
            seconds=cp_assignment[key]["productive_start_seconds"])
        result = allocate_crew(
            operator_index, operation.required_capability, int(row["line_id"]),
            k, ready, productive_minutes, reservations, busy,
            priority=0.5, state_is_normalized=True)
        reservations, busy = result["reservations"], result["busy_minutes"]
        if result["shortage"] is not None:
            return {
                "success": False, "operation_key": key,
                "reason": result["shortage"],
            }, None
        actual_end = int(round((result["end"]-problem.origin).total_seconds()))
        envelope_end = int(cp_assignment[key]["end_seconds"])
        if actual_end > envelope_end:
            return {
                "success": False, "operation_key": key,
                "reason": "worker completion exceeds CP machine envelope",
                "cp_end_seconds": envelope_end,
                "worker_completion_seconds": actual_end,
                "extension_required_seconds": actual_end-envelope_end,
            }, None
        realized[key] = {
            "employees": tuple(str(value) for value in result["employees"]),
            "segments": tuple(result["segments"]),
            "tier": result["tier"], "end_seconds": actual_end,
            "productive_minutes": productive_minutes,
        }
    return {"success": True, "operations": len(realized)}, realized


def _changeover_check(phenotype):
    by_machine = defaultdict(list)
    for item in phenotype.operations:
        a = item.assignment
        for machine_id in a.machine_ids:
            by_machine[machine_id].append((
                a.start_seconds, a.end_seconds, a.order_id, a.operation_key))
    violations = []
    for machine_id, rows in by_machine.items():
        rows.sort()
        for left, right in zip(rows, rows[1:]):
            gap = authoritative_changeover_seconds(left[2], right[2])
            if left[1]+gap > right[0]:
                violations.append({
                    "machine_id": machine_id, "left": left[3], "right": right[3],
                    "required_gap": gap, "available_gap": right[0]-left[1],
                })
    return violations


def realize_transaction(problem, operator_index, op_details, index,
                        neighborhood, cp_assignment, baseline_validated):
    mutable = set(neighborhood.operation_keys)
    machine_result, machines = materialize_machines(
        problem, index, mutable, cp_assignment)
    if not machine_result["success"]:
        return {"stage": "machine", "accepted": False,
                "machine": machine_result}, None
    worker_result, workers = allocate_workers(
        problem, operator_index, op_details, index, mutable,
        cp_assignment, machines)
    if not worker_result["success"]:
        return {"stage": "worker", "accepted": False,
                "machine": machine_result, "worker": worker_result}, None

    baseline_by_key = {
        item.assignment.operation_key: item for item in baseline_validated.operations}
    replaced = []
    for operation in problem.operations:
        key = operation.operation_key
        if key not in mutable:
            replaced.append(baseline_by_key[key])
            continue
        old = baseline_by_key[key]
        cp = cp_assignment[key]
        worker = workers[key]
        assignment = OperationAssignment(
            operation_key=key, order_id=operation.order_id,
            operation_id=operation.operation_id,
            line_id=old.assignment.line_id, machine_ids=machines[key],
            machine_count=len(machines[key]),
            start_seconds=int(cp["start_seconds"]),
            productive_start_seconds=int(cp["productive_start_seconds"]),
            end_seconds=int(worker["end_seconds"]),
            setup_seconds=old.assignment.setup_seconds,
            productive_seconds=old.assignment.productive_seconds,
        )
        replaced.append(PhenotypeOperation(
            assignment=assignment, employee_ids=worker["employees"],
            worker_segments=tuple((employee, worker["segments"])
                                  for employee in worker["employees"]),
            sourcing_tier=worker["tier"],
        ))
    candidate = CrafterPhenotype(CrafterStatus.WORKER_FEASIBLE, tuple(replaced))
    changeover_violations = _changeover_check(candidate)
    if changeover_violations:
        return {"stage": "changeover", "accepted": False,
                "machine": machine_result, "worker": worker_result,
                "violations": changeover_violations[:20]}, None
    validated = validate(problem, candidate, operator_index)
    before, after = (dict(baseline_validated.comparison_metrics),
                     dict(validated.comparison_metrics))
    outside_unchanged = all(
        baseline_by_key[key] == next(
            item for item in validated.operations
            if item.assignment.operation_key == key)
        for key in baseline_by_key if key not in mutable)
    improved = (
        validated.status == CrafterStatus.VALIDATED_FEASIBLE and
        after["total_tardiness_seconds"] < before["total_tardiness_seconds"])
    diagnostics = {
        "stage": "validated", "accepted": bool(improved),
        "machine": machine_result, "worker": worker_result,
        "validator_valid": bool(validated.validation_report["valid"]),
        "validator_violations": validated.validation_report.get(
            "violation_count", sum(len(value) for key, value in
            validated.validation_report.items() if isinstance(value, list))),
        "outside_operations_unchanged": outside_unchanged,
        "before": before, "after": after,
        "phenotype_hash": validated.phenotype_hash,
        "rollback_hash": baseline_validated.phenotype_hash if not improved else None,
        "changed_operations": sum(
            baseline_by_key[item.assignment.operation_key] != item
            for item in validated.operations),
        "line_changes": 0, "machine_count_changes": 0,
    }
    return diagnostics, validated
