"""Exact verification and full-phenotype transaction for E1.2."""
from __future__ import annotations

from crafter.contracts import (
    CrafterPhenotype, CrafterStatus, OperationAssignment, PhenotypeOperation,
)
from crafter.phenotype import comparison_metrics, exported_rows, validate
from .anchored_model import outside_machine_reservations
from .transaction import _changeover_check, _machine_accepts


def realize_selective_transaction(problem, operator_index, index,
                                  neighborhood, selected, baseline):
    mutable = set(neighborhood.operation_keys)
    baseline_by_key = {
        item.assignment.operation_key: item for item in baseline.operations}
    outside = outside_machine_reservations(problem, index, mutable)
    machine_witnesses = []
    local_by_machine = {}
    for key in sorted(mutable):
        old = baseline_by_key[key]
        option = selected[key]["option"]
        machines = tuple(sorted(selected[key]["machine_ids"]))
        if len(machines) != old.assignment.machine_count:
            machine_witnesses.append({
                "operation_key": key, "reason": "wrong selected k",
                "expected": old.assignment.machine_count, "actual": len(machines)})
            continue
        operation = problem.operation_by_key[key]
        for machine_id in machines:
            machine = problem.machine_by_id.get(machine_id)
            if not (machine and machine.is_usable and
                    machine.factory_id == problem.order_by_id[operation.order_id].factory_id and
                    machine.line_id == old.assignment.line_id and
                    machine.supports(operation.required_capability)):
                machine_witnesses.append({
                    "operation_key": key, "machine_id": machine_id,
                    "reason": "physical machine eligibility"})
            elif not _machine_accepts(outside.get(machine_id, ()),
                                      option.start_seconds, option.end_seconds,
                                      operation.order_id):
                machine_witnesses.append({
                    "operation_key": key, "machine_id": machine_id,
                    "reason": "frozen outside occupancy/changeover boundary",
                    "proposed": [option.start_seconds, option.end_seconds],
                    "outside": [list(row) for row in outside.get(machine_id, ())],
                })
            local_by_machine.setdefault(machine_id, []).append((
                option.start_seconds, option.end_seconds, operation.order_id, key))
    if machine_witnesses:
        return {
            "accepted": False, "stage": "machine_verification",
            "machine_verification_success": False,
            "worker_feasible": None, "witnesses": machine_witnesses,
            "rollback_hash": baseline.phenotype_hash,
        }, None
    for machine_id, rows in local_by_machine.items():
        rows.sort()
        from crafter.authoritative_semantics import authoritative_changeover_seconds
        for left, right in zip(rows, rows[1:]):
            required = authoritative_changeover_seconds(left[2], right[2])
            if left[1]+required > right[0]:
                machine_witnesses.append({
                    "machine_id": machine_id, "left": left[3], "right": right[3],
                    "reason": "local exact-machine transition",
                    "required_gap": required, "available_gap": right[0]-left[1],
                })
    if machine_witnesses:
        return {
            "accepted": False, "stage": "machine_verification",
            "machine_verification_success": False,
            "worker_feasible": None, "witnesses": machine_witnesses,
            "rollback_hash": baseline.phenotype_hash,
        }, None

    operations = []
    for operation in problem.operations:
        key = operation.operation_key
        old = baseline_by_key[key]
        if key not in mutable:
            operations.append(old)
            continue
        option = selected[key]["option"]
        machines = tuple(sorted(selected[key]["machine_ids"]))
        assignment = OperationAssignment(
            operation_key=key, order_id=old.assignment.order_id,
            operation_id=old.assignment.operation_id,
            line_id=old.assignment.line_id, machine_ids=machines,
            machine_count=old.assignment.machine_count,
            start_seconds=option.start_seconds,
            productive_start_seconds=option.productive_start_seconds,
            end_seconds=option.end_seconds,
            setup_seconds=old.assignment.setup_seconds,
            productive_seconds=old.assignment.productive_seconds,
        )
        operations.append(PhenotypeOperation(
            assignment=assignment, employee_ids=old.employee_ids,
            worker_segments=tuple(
                (employee, option.worker_segments) for employee in old.employee_ids),
            sourcing_tier=old.sourcing_tier,
        ))
    candidate = CrafterPhenotype(CrafterStatus.WORKER_FEASIBLE,
                                 tuple(operations))
    outside_unchanged = all(
        baseline_by_key[item.assignment.operation_key] == item
        for item in candidate.operations
        if item.assignment.operation_key not in mutable)
    worker_ids_unchanged = all(
        baseline_by_key[key].employee_ids == next(
            item.employee_ids for item in candidate.operations
            if item.assignment.operation_key == key)
        for key in mutable)
    if not outside_unchanged or not worker_ids_unchanged:
        return {
            "accepted": False, "stage": "identity",
            "machine_verification_success": True,
            "worker_feasible": False,
            "outside_operations_unchanged": outside_unchanged,
            "worker_ids_unchanged": worker_ids_unchanged,
            "rollback_hash": baseline.phenotype_hash,
        }, None
    # This independent complete-phenotype check includes local and outside
    # exact machine transitions before the authoritative validator is called.
    transition_errors = _changeover_check(candidate)
    if transition_errors:
        return {
            "accepted": False, "stage": "machine_verification",
            "machine_verification_success": False,
            "worker_feasible": True,
            "witnesses": transition_errors,
            "rollback_hash": baseline.phenotype_hash,
        }, None

    validated = validate(problem, candidate, operator_index)
    before, after = dict(baseline.comparison_metrics), dict(
        validated.comparison_metrics)
    score_replay = comparison_metrics(problem, validated)
    export_one, export_two = (exported_rows(problem, validated),
                              exported_rows(problem, validated))
    valid = (validated.status == CrafterStatus.VALIDATED_FEASIBLE and
             bool(validated.validation_report["valid"]))
    improved = valid and after["total_tardiness_seconds"] < before[
        "total_tardiness_seconds"]
    machine_changed = [key for key in mutable if
        tuple(sorted(baseline_by_key[key].assignment.machine_ids)) !=
        tuple(sorted(selected[key]["machine_ids"]))]
    changed = [item.assignment.operation_key for item in validated.operations
               if item != baseline_by_key[item.assignment.operation_key]]
    timing_churn = sum(
        abs(item.assignment.start_seconds-baseline_by_key[
            item.assignment.operation_key].assignment.start_seconds) +
        abs(item.assignment.end_seconds-baseline_by_key[
            item.assignment.operation_key].assignment.end_seconds)
        for item in validated.operations
        if item.assignment.operation_key in mutable)
    return {
        "accepted": improved,
        "stage": "validated" if valid else "validator",
        "machine_verification_success": True,
        "worker_feasible": valid,
        "worker_ids_unchanged": worker_ids_unchanged,
        "outside_operations_unchanged": outside_unchanged,
        "validator_status": validated.status.value,
        "validator_valid": bool(validated.validation_report["valid"]),
        "validator_report": validated.validation_report,
        "before": before, "after": after,
        "improvement_seconds": before["total_tardiness_seconds"]-
            after["total_tardiness_seconds"],
        "changed_operations": changed,
        "changed_operation_count": len(changed),
        "machine_changed_operations": machine_changed,
        "machine_change_count": len(machine_changed),
        "timing_churn_seconds": timing_churn,
        "phenotype_hash": validated.phenotype_hash,
        "rollback_hash": None if improved else baseline.phenotype_hash,
        "score_replay_equal": score_replay == after,
        "export_replay_equal": export_one == export_two,
    }, validated
