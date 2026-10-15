"""Freeze, hash, score and independently validate realized Crafter output."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import timedelta

from ga.worker_allocation import operator_available_at
from validation.schedule_validator import (
    ScheduleValidationContext, validate_exported_schedule,
)

from .contracts import CrafterStatus


def _rows(problem, phenotype, execution_contracts=None):
    execution_contracts = execution_contracts or {}
    rows = []
    for item in phenotype.operations:
        a = item.assignment
        row = {
            "production_order_id": a.order_id,
            "operation_id": a.operation_id,
            "assigned_line_id": a.line_id,
            "machines_used": a.machine_count,
            "machine_ids_used": list(a.machine_ids),
            "operators_used": len(item.employee_ids),
            "employee_ids_used": list(item.employee_ids),
            "worker_segments_by_employee": {
                employee_id: [
                    {"start": start.isoformat(), "end": end.isoformat()}
                    for start, end in segments
                ]
                for employee_id, segments in item.worker_segments
            },
            "operator_sourcing_tier": item.sourcing_tier,
            "proposed_start_time": (
                problem.origin + timedelta(seconds=a.start_seconds)).isoformat(),
            "base_end_time": (
                problem.origin + timedelta(seconds=a.end_seconds)).isoformat(),
            "cushioned_end_time": (
                problem.origin + timedelta(seconds=a.end_seconds)).isoformat(),
        }
        contract = execution_contracts.get(a.operation_key)
        if contract is not None:
            from crafter.execution_contract import OperationExecutionContract
            contract = (contract if isinstance(contract, OperationExecutionContract)
                        else OperationExecutionContract.from_mapping(contract))
            row["quantity_scheduled"] = int(contract.quantity)
        rows.append(row)
    return rows


def phenotype_hash(problem, phenotype):
    payload = json.dumps(_rows(problem, phenotype), sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def comparison_metrics(problem, phenotype):
    """Metrics from the frozen phenotype; never re-decodes assignments."""
    if not phenotype.operations:
        return {
            "operations": 0, "makespan_seconds": 0,
            "total_tardiness_seconds": 0, "late_order_count": 0,
            "on_time_rate": 0.0,
        }
    final_by_order = {}
    line_busy = {line.line_id: 0 for line in problem.lines}
    employee_seconds = 0.0
    for item in phenotype.operations:
        a = item.assignment
        current = final_by_order.get(a.order_id)
        sequence = problem.operation_by_key[a.operation_key].sequence
        if current is None or sequence > current[0]:
            final_by_order[a.order_id] = (sequence, a.end_seconds)
        line_busy[a.line_id] += a.machine_count * (a.end_seconds-a.start_seconds)
        for _employee, segments in item.worker_segments:
            employee_seconds += sum((end-start).total_seconds() for start, end in segments)
    tardiness = {}
    for order_id, (_sequence, completion) in final_by_order.items():
        deadline = problem.order_by_id[order_id].deadline_seconds
        if deadline is not None:
            tardiness[order_id] = max(0, completion-max(0, deadline))
    late = sum(value > 0 for value in tardiness.values())
    loads = list(line_busy.values())
    return {
        "operations": len(phenotype.operations),
        "makespan_seconds": max(a.assignment.end_seconds for a in phenotype.operations),
        "total_tardiness_seconds": sum(tardiness.values()),
        "late_order_count": late,
        "on_time_rate": (1.0-late/len(tardiness)) if tardiness else 1.0,
        "line_load_imbalance_machine_seconds": max(loads)-min(loads) if loads else 0,
        "employee_productive_seconds": employee_seconds,
    }


def validate(problem, phenotype, operator_index, *, execution_contracts=None,
             allow_test_assumptions=False, require_execution_contracts=False,
             worker_interval_is_available=None, machine_interval_is_available=None,
             expected_source_fingerprint=None, expected_snapshot_fingerprint=None,
             expected_scenario_fingerprint=None):
    if phenotype.status != CrafterStatus.WORKER_FEASIBLE:
        return phenotype
    expected = []
    for op in problem.operations:
        order = problem.order_by_id[op.order_id]
        # Required productive time depends on the selected machine count.
        assignment = next(
            row.assignment for row in phenotype.operations
            if row.assignment.operation_key == op.operation_key)
        contract = (execution_contracts or {}).get(op.operation_key)
        if contract is not None:
            from crafter.execution_contract import OperationExecutionContract
            contract = (contract if isinstance(contract, OperationExecutionContract)
                        else OperationExecutionContract.from_mapping(contract))
        try:
            contract_productive_seconds = (contract.expected_productive_seconds(
                next(item.employee_ids for item in phenotype.operations
                     if item.assignment.operation_key == op.operation_key),
                assignment.machine_ids) if contract else assignment.productive_seconds)
        except ValueError:
            # Keep the validator's independent contract diagnostics reachable
            # for a malformed selected assignment instead of failing during
            # expected-row construction.
            contract_productive_seconds = assignment.productive_seconds
        expected.append({
            "production_order_id": op.order_id,
            "operation_id": op.operation_id,
            "operation_sequence_number": op.sequence,
            "release_time": problem.origin + timedelta(seconds=order.release_seconds),
            "deadline": (
                problem.origin + timedelta(seconds=order.deadline_seconds)
                if order.deadline_seconds is not None else None),
            "eligible_line_ids": op.eligible_line_ids,
            "required_machine_type": op.required_capability,
            "required_operation_type": op.required_capability,
            "required_machine_count": (contract.required_machine_count if contract else assignment.machine_count),
            "required_workers": (contract.required_worker_count if contract else assignment.machine_count),
            "setup_minutes": (contract.setup_seconds if contract else assignment.setup_seconds) / 60.0,
            "productive_minutes": contract_productive_seconds / 60.0,
        })

    ownership = {order.order_id: order.factory_id for order in problem.orders}
    line_factory = {line.line_id: line.factory_id for line in problem.lines}
    machines = {
        machine.machine_id: {
            "line_id": machine.line_id,
            "factory_id": machine.factory_id,
            "capabilities": machine.capabilities,
        }
        for machine in problem.machines
    }
    skills, worker_factory = {}, {}
    for skill, employees in operator_index.get("by_skill", {}).items():
        for employee in employees:
            skills.setdefault(str(employee), set()).add(str(skill).strip().lower())
    for (factory_id, _skill), employees in operator_index.get("by_factory", {}).items():
        for employee in employees:
            worker_factory.setdefault(str(employee), int(factory_id))
    workers = {
        employee: {"factory_id": worker_factory.get(employee), "skills": tuple(values)}
        for employee, values in skills.items()
    }
    committed_machines = [
        {
            "resource_id": row.machine_id,
            "start": problem.origin + timedelta(seconds=row.start_seconds),
            "end": problem.origin + timedelta(seconds=row.end_seconds),
            "operation_key": f"fixed:{row.production_order_id}",
        }
        for row in problem.resources.fixed_machine_commitments
    ]
    committed_workers = [
        {
            "resource_id": row.employee_id,
            "start": problem.origin + timedelta(seconds=row.start_seconds),
            "end": problem.origin + timedelta(seconds=row.end_seconds),
            "operation_key": row.operation_key,
        }
        for row in problem.resources.fixed_worker_commitments
    ]
    context = ScheduleValidationContext(
        expected_operations=expected,
        ownership_by_order=ownership,
        line_factory=line_factory,
        machines=machines,
        workers=workers,
        committed_machine_intervals=committed_machines,
        committed_worker_intervals=committed_workers,
        worker_is_available=lambda employee, when: operator_available_at(
            operator_index, int(employee) if str(employee).isdigit() else employee, when),
        require_worker_segments=True,
        operation_contracts=execution_contracts or {},
        allow_test_assumptions=allow_test_assumptions,
        require_execution_contracts=require_execution_contracts,
        worker_interval_is_available=worker_interval_is_available,
        machine_interval_is_available=machine_interval_is_available,
        expected_source_fingerprint=expected_source_fingerprint,
        expected_snapshot_fingerprint=expected_snapshot_fingerprint,
        expected_scenario_fingerprint=expected_scenario_fingerprint,
    )
    report = validate_exported_schedule(_rows(problem, phenotype, execution_contracts), context).as_dict()
    metrics = comparison_metrics(problem, phenotype)
    digest = phenotype_hash(problem, phenotype)
    status = CrafterStatus.VALIDATED_FEASIBLE if report["valid"] else CrafterStatus.VALIDATION_FAILED
    return replace(
        phenotype, status=status, phenotype_hash=digest,
        comparison_metrics=tuple(sorted(metrics.items())),
        validation_report=report,
    )


def exported_rows(problem, phenotype):
    """Public read-only rendering of the already-frozen phenotype."""
    return tuple(_rows(problem, phenotype))
