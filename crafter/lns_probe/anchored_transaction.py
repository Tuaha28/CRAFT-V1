"""Reconstruct and validate a complete phenotype from anchored timing options."""
from __future__ import annotations

from crafter.contracts import (
    CrafterPhenotype, CrafterStatus, OperationAssignment, PhenotypeOperation,
)
from crafter.phenotype import comparison_metrics, exported_rows, validate
from .transaction import _changeover_check


def realize_anchored_transaction(problem, operator_index, neighborhood,
                                 selected_options, baseline):
    mutable = set(neighborhood.operation_keys)
    baseline_by_key = {
        item.assignment.operation_key: item for item in baseline.operations}
    operations = []
    for operation in problem.operations:
        key = operation.operation_key
        old = baseline_by_key[key]
        if key not in mutable:
            operations.append(old)
            continue
        option = selected_options[key]
        assignment = OperationAssignment(
            operation_key=key, order_id=old.assignment.order_id,
            operation_id=old.assignment.operation_id,
            line_id=old.assignment.line_id,
            machine_ids=old.assignment.machine_ids,
            machine_count=old.assignment.machine_count,
            start_seconds=option.start_seconds,
            productive_start_seconds=option.productive_start_seconds,
            end_seconds=option.end_seconds,
            setup_seconds=old.assignment.setup_seconds,
            productive_seconds=old.assignment.productive_seconds,
        )
        operations.append(PhenotypeOperation(
            assignment=assignment,
            employee_ids=old.employee_ids,
            worker_segments=tuple(
                (employee, option.worker_segments)
                for employee in old.employee_ids),
            sourcing_tier=old.sourcing_tier,
        ))
    candidate = CrafterPhenotype(CrafterStatus.WORKER_FEASIBLE,
                                 tuple(operations))
    outside_unchanged = all(
        baseline_by_key[item.assignment.operation_key] == item
        for item in candidate.operations
        if item.assignment.operation_key not in mutable)
    machine_ids_unchanged = all(
        baseline_by_key[key].assignment.machine_ids == next(
            item.assignment.machine_ids for item in candidate.operations
            if item.assignment.operation_key == key)
        for key in mutable)
    worker_ids_unchanged = all(
        baseline_by_key[key].employee_ids == next(
            item.employee_ids for item in candidate.operations
            if item.assignment.operation_key == key)
        for key in mutable)
    changeover = _changeover_check(candidate)
    if not outside_unchanged or not machine_ids_unchanged or not worker_ids_unchanged:
        return {
            "accepted": False, "stage": "anchor_identity",
            "outside_operations_unchanged": outside_unchanged,
            "machine_ids_unchanged": machine_ids_unchanged,
            "worker_ids_unchanged": worker_ids_unchanged,
            "rollback_hash": baseline.phenotype_hash,
        }, None
    if changeover:
        return {
            "accepted": False, "stage": "machine_changeover",
            "machine_anchored_feasible": False,
            "worker_anchored_feasible": True,
            "changeover_violations": changeover,
            "rollback_hash": baseline.phenotype_hash,
        }, None
    validated = validate(problem, candidate, operator_index)
    before = dict(baseline.comparison_metrics)
    after = dict(validated.comparison_metrics)
    score_replay = comparison_metrics(problem, validated)
    rows_once = exported_rows(problem, validated)
    rows_twice = exported_rows(problem, validated)
    valid = (validated.status == CrafterStatus.VALIDATED_FEASIBLE and
             bool(validated.validation_report["valid"]))
    improved = valid and after["total_tardiness_seconds"] < before[
        "total_tardiness_seconds"]
    changed = [
        item.assignment.operation_key for item in validated.operations
        if item != baseline_by_key[item.assignment.operation_key]
    ]
    churn = sum(
        abs(item.assignment.start_seconds-
            baseline_by_key[item.assignment.operation_key].assignment.start_seconds)
        + abs(item.assignment.end_seconds-
              baseline_by_key[item.assignment.operation_key].assignment.end_seconds)
        for item in validated.operations
        if item.assignment.operation_key in mutable)
    diagnostics = {
        "accepted": improved,
        "stage": "validated" if valid else "validator",
        "machine_anchored_feasible": True,
        "worker_anchored_feasible": True,
        "outside_operations_unchanged": outside_unchanged,
        "machine_ids_unchanged": machine_ids_unchanged,
        "worker_ids_unchanged": worker_ids_unchanged,
        "validator_status": validated.status.value,
        "validator_valid": bool(validated.validation_report["valid"]),
        "validator_report": validated.validation_report,
        "before": before, "after": after,
        "improvement_seconds": (
            before["total_tardiness_seconds"]-
            after["total_tardiness_seconds"]),
        "changed_operations": changed,
        "changed_operation_count": len(changed),
        "timing_churn_seconds": churn,
        "phenotype_hash": validated.phenotype_hash,
        "rollback_hash": None if improved else baseline.phenotype_hash,
        "score_replay_equal": score_replay == after,
        "export_replay_equal": rows_once == rows_twice,
    }
    return diagnostics, validated
