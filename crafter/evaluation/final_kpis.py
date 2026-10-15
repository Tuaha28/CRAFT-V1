"""One scheduler-independent KPI calculator for final valid schedules."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from statistics import mean
from typing import Mapping, Sequence

from ga.worker_allocation import operator_available_at
from utils.shift_rotation import SHIFT_BANDS

from .neutral_factory import NeutralFactoryWorkload


class EvaluationError(ValueError):
    """The schedule cannot receive complete-schedule KPIs."""


def _employee_token(index: Mapping, worker_id: str):
    for mapping_name in ("crew_of", "shift_of"):
        mapping = index.get(mapping_name, {})
        if worker_id in mapping:
            return worker_id
        if str(worker_id).isdigit() and int(worker_id) in mapping:
            return int(worker_id)
    return worker_id


def _merge(intervals):
    merged = []
    for start, end in sorted((int(a), int(b)) for a, b in intervals if int(b) > int(a)):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _intersection_seconds(intervals, blockers):
    total = 0
    blockers = _merge(blockers)
    for left, right in _merge(intervals):
        for start, end in blockers:
            if end <= left:
                continue
            if start >= right:
                break
            total += max(0, min(right, end) - max(left, start))
    return total


def _hour_value(value: float) -> time:
    seconds = int(round(float(value) * 3600)) % 86400
    return time(seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def _worker_available_intervals(index: Mapping, worker_id: str, origin: datetime, end_seconds: int):
    token = _employee_token(index, worker_id)
    crew = index.get("crew_of", {}).get(token)
    shift = index.get("shift_of", {}).get(token)
    if crew is None and shift is None:
        return None
    end_at = origin + timedelta(seconds=int(end_seconds))
    points = {origin, end_at}
    day = origin.date() - timedelta(days=1)
    final_day = end_at.date() + timedelta(days=1)
    if crew is not None:
        boundary_times = {time.min}
        for _name, start, end in SHIFT_BANDS:
            boundary_times.add(start); boundary_times.add(end)
    else:
        boundary_times = {time.min, _hour_value(float(shift[0])), _hour_value(float(shift[1]))}
    while day <= final_day:
        for boundary in boundary_times:
            point = datetime.combine(day, boundary)
            if origin < point < end_at:
                points.add(point)
        day += timedelta(days=1)
    ordered = sorted(points)
    intervals = []
    for left, right in zip(ordered, ordered[1:]):
        probe = left + (right - left) / 2
        if operator_available_at(index, token, probe):
            intervals.append((int((left - origin).total_seconds()), int((right - origin).total_seconds())))
    return _merge(intervals)


def _assert_validation(validation: Mapping):
    required = {
        "scope_status": "SCOPE_VALID",
        "replay_status": "PASS",
        "physical_status": "VALID",
        "accepted": True,
    }
    failures = {key: {"expected": expected, "actual": validation.get(key)}
                for key, expected in required.items() if validation.get(key) != expected}
    scope = validation.get("scope", {}) or {}
    if scope.get("missing_required_operations"):
        failures["missing_required_operations"] = scope["missing_required_operations"]
    if scope.get("duplicate_operations"):
        failures["duplicate_operations"] = scope["duplicate_operations"]
    if scope.get("unexpected_operations"):
        failures["unexpected_operations"] = scope["unexpected_operations"]
    violations = validation.get("violation_counts", {}) or {}
    nonzero = {key: value for key, value in violations.items() if int(value or 0) != 0}
    if nonzero:
        failures["violations"] = nonzero
    if failures:
        raise EvaluationError(f"schedule failed authoritative validation gate: {failures}")


def evaluate_final_schedule(
    workload: NeutralFactoryWorkload,
    schedule_rows: Sequence[Mapping],
    *,
    validation: Mapping,
    runtime_profile: Mapping | None = None,
) -> dict:
    """Calculate final KPIs after strict identity and physical gates."""

    _assert_validation(validation)
    payload = workload.payload
    problem = workload.problem
    expected = tuple(payload["accounting"]["benchmark_operation_keys"])
    scheduled = [str(row["operation_key"]) for row in schedule_rows]
    counts = Counter(scheduled)
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    missing = sorted(set(expected) - set(scheduled))
    extra = sorted(set(scheduled) - set(expected))
    if duplicates or missing or extra:
        raise EvaluationError(
            f"schedule identity invalid: missing={missing}, duplicates={duplicates}, extra={extra}")
    if not schedule_rows:
        raise EvaluationError("empty final schedule")

    row_by_key = {str(row["operation_key"]): row for row in schedule_rows}
    operation_by_key = problem.operation_by_key
    order_by_id = problem.order_by_id
    routes: dict[str, list] = defaultdict(list)
    release_violations = []
    invalid_intervals = []
    for key, row in row_by_key.items():
        op = operation_by_key[key]
        order = order_by_id[op.order_id]
        start, end = int(row["start_seconds"]), int(row["end_seconds"])
        if end <= start:
            invalid_intervals.append(key)
        if start < int(order.release_seconds):
            release_violations.append({"operation_key": key, "start": start, "release": int(order.release_seconds)})
        routes[str(op.order_id)].append((int(op.sequence), key, start, end))
    if invalid_intervals or release_violations:
        raise EvaluationError(
            f"invalid intervals/releases: intervals={invalid_intervals}, releases={release_violations}")

    precedence_violations = []
    order_completion = {}
    final_operation = {}
    for order_id, route in sorted(routes.items()):
        route.sort(key=lambda item: (item[0], item[1]))
        if len({item[0] for item in route}) != len(route):
            raise EvaluationError(f"duplicate routing sequence for order {order_id}")
        for predecessor, successor in zip(route, route[1:]):
            if successor[2] < predecessor[3]:
                precedence_violations.append({
                    "predecessor": predecessor[1], "successor": successor[1],
                    "predecessor_end": predecessor[3], "successor_start": successor[2],
                })
        final = route[-1]
        final_operation[order_id] = final[1]
        order_completion[order_id] = final[3]
    if precedence_violations:
        raise EvaluationError(f"precedence violations: {precedence_violations}")

    evaluation_origin_seconds = int(payload["snapshot_as_of_seconds"])
    absolute_last_completion = max(int(row["end_seconds"]) for row in schedule_rows)
    makespan = absolute_last_completion - evaluation_origin_seconds
    if makespan < 0:
        raise EvaluationError("last completion precedes evaluation origin")

    tardiness = {}
    for order_id, completion in order_completion.items():
        due = order_by_id[order_id].deadline_seconds
        if due is not None:
            tardiness[order_id] = max(0, int(completion) - int(due))
    on_time = sum(value == 0 for value in tardiness.values())
    late = sum(value > 0 for value in tardiness.values())

    flow = {}
    for order_id, completion in order_completion.items():
        release = order_by_id[order_id].release_seconds
        if release is not None:
            value = int(completion) - int(release)
            if value < 0:
                raise EvaluationError(
                    f"order {order_id} completion {completion} precedes release {release}")
            flow[order_id] = value

    # Productive labour counts every fixed-crew member for each productive
    # segment. Machine occupancy counts the full operation span, including
    # authoritative worker pauses.
    productive_by_worker: dict[str, int] = defaultdict(int)
    occupied_by_machine: dict[int, int] = defaultdict(int)
    for row in schedule_rows:
        cfg = row["resource_configuration"]
        segments = [(int(a), int(b)) for a, b in row.get("worker_segments", ())]
        productive = sum(max(0, right - left) for left, right in segments)
        for worker in cfg.get("worker_ids", ()):
            productive_by_worker[str(worker)] += productive
        occupied = int(row["end_seconds"]) - int(row["start_seconds"])
        for machine in cfg.get("machine_ids", ()):
            occupied_by_machine[int(machine)] += occupied

    worker_commitments: dict[str, list] = defaultdict(list)
    for commitment in problem.resources.fixed_worker_commitments:
        if int(commitment.factory_id) == int(payload["factory_id"]):
            worker_commitments[str(commitment.employee_id)].append(
                (max(0, int(commitment.start_seconds)), min(absolute_last_completion, int(commitment.end_seconds))))
    per_worker = []
    worker_available_total = 0
    unresolved_workers = []
    for worker in payload["workers"]:
        worker_id = str(worker["worker_id"])
        intervals = _worker_available_intervals(
            workload.operator_index, worker_id, problem.origin, absolute_last_completion)
        if intervals is None:
            unresolved_workers.append(worker_id)
            continue
        raw = sum(right - left for left, right in intervals)
        reserved = _intersection_seconds(intervals, worker_commitments.get(worker_id, ()))
        available = raw - reserved
        productive = productive_by_worker.get(worker_id, 0)
        if productive > available:
            raise EvaluationError(
                f"worker {worker_id} productive seconds {productive} exceed available {available}")
        worker_available_total += available
        per_worker.append({
            "worker_id": worker_id, "productive_seconds": productive,
            "raw_calendar_seconds": raw, "fixed_reservation_seconds": reserved,
            "available_seconds": available,
            "utilization": productive / available if available else None,
        })
    productive_worker_total = sum(productive_by_worker.values())
    worker_status = "VALID" if not unresolved_workers and worker_available_total > 0 else "NOT_YET_PROVEN"
    worker_utilization = (
        productive_worker_total / worker_available_total
        if worker_status == "VALID" else None
    )

    machine_commitments: dict[int, list] = defaultdict(list)
    for commitment in problem.resources.fixed_machine_commitments:
        machine_commitments[int(commitment.machine_id)].append(
            (max(0, int(commitment.start_seconds)), min(absolute_last_completion, int(commitment.end_seconds))))
    per_machine = []
    machine_available_total = 0
    usable_machines = [machine for machine in problem.machines
                       if int(machine.factory_id) == int(payload["factory_id"]) and machine.is_usable]
    for machine in sorted(usable_machines, key=lambda item: int(item.machine_id)):
        machine_id = int(machine.machine_id)
        raw = absolute_last_completion
        reserved = sum(right - left for left, right in _merge(machine_commitments.get(machine_id, ())))
        available = raw - reserved
        occupied = occupied_by_machine.get(machine_id, 0)
        if occupied > available:
            raise EvaluationError(
                f"machine {machine_id} occupied seconds {occupied} exceed available {available}")
        machine_available_total += available
        per_machine.append({
            "machine_id": machine_id, "occupied_seconds": occupied,
            "raw_calendar_seconds": raw, "fixed_reservation_seconds": reserved,
            "available_seconds": available,
            "utilization": occupied / available if available else None,
        })
    machine_occupied_total = sum(occupied_by_machine.values())
    machine_status = "VALID" if usable_machines and machine_available_total > 0 else "NOT_YET_PROVEN"
    machine_utilization = (
        machine_occupied_total / machine_available_total
        if machine_status == "VALID" else None
    )

    runtime = dict(runtime_profile or {})
    reported_cp_seconds = runtime.get("cp_seconds")
    cp_timing_status = (
        "NOT_SEPARATELY_CAPTURED_IN_FROZEN_PROFILE"
        if reported_cp_seconds in (None, 0, 0.0)
        else "AVAILABLE"
    )
    return {
        "contract_version": "FINAL_SCHEDULE_KPI_V1",
        "validation_status": "VALID",
        "operations_completed": len(schedule_rows),
        "orders_completed": len(order_completion),
        "evaluation_origin_seconds": evaluation_origin_seconds,
        "evaluation_origin_datetime": problem.origin.isoformat(),
        "absolute_last_completion_seconds": absolute_last_completion,
        "makespan_seconds": makespan,
        "order_completion_seconds": order_completion,
        "final_operation_by_order": final_operation,
        "orders_with_due_dates": len(tardiness),
        "on_time_orders": on_time,
        "late_orders": late,
        "on_time_delivery_percentage": 100.0 * on_time / len(tardiness) if tardiness else None,
        "tardiness_by_order_seconds": tardiness,
        "total_tardiness_seconds": sum(tardiness.values()),
        "mean_tardiness_seconds": mean(tardiness.values()) if tardiness else None,
        "maximum_tardiness_seconds": max(tardiness.values()) if tardiness else None,
        "orders_with_flow_time": len(flow),
        "flow_time_by_order_seconds": flow,
        "total_flow_time_seconds": sum(flow.values()),
        "mean_flow_time_seconds": mean(flow.values()) if flow else None,
        "minimum_flow_time_seconds": min(flow.values()) if flow else None,
        "maximum_flow_time_seconds": max(flow.values()) if flow else None,
        "worker_utilization": {
            "status": worker_status,
            "productive_worker_seconds": productive_worker_total,
            "available_worker_seconds": worker_available_total if worker_status == "VALID" else None,
            "utilization": worker_utilization,
            "worker_count_in_denominator": len(per_worker) if worker_status == "VALID" else None,
            "calendar_source": "operator_index crew_of/shift_of + utils.shift_rotation + fixed worker commitments",
            "availability_assumptions": [
                "assignable source excludes inactive/on-leave workers",
                "crew members use rotating shifts and staggered rest days",
                "floaters use fixed Work_Schedules shift bands",
                "fixed reservations are removed from benchmark-available capacity",
                "idle but calendar-available workers remain in denominator",
            ],
            "unresolved_worker_calendar_ids": unresolved_workers,
            "per_worker": per_worker,
        },
        "machine_utilization": {
            "status": machine_status,
            "machine_occupied_seconds": machine_occupied_total,
            "machine_available_seconds": machine_available_total if machine_status == "VALID" else None,
            "utilization": machine_utilization,
            "machine_count_in_denominator": len(usable_machines) if machine_status == "VALID" else None,
            "calendar_source": "SchedulingProblem MachineSpec.is_usable; factory-level machine calendar is continuous in frozen V1; fixed machine commitments removed",
            "availability_assumptions": [
                "unusable/reserved machines are excluded by authoritative problem construction",
                "no separate machine shift calendar exists in frozen V1",
                "machine occupancy spans operation start to end, including worker pauses",
                "fixed reservations are removed from benchmark-available capacity",
            ],
            "per_machine": per_machine,
        },
        "runtime": {
            "status": "VALID" if runtime else "NOT_AVAILABLE",
            "total_runner_runtime_seconds": runtime.get("benchmark_wall_seconds"),
            "accepted_window_runtime_seconds": runtime.get("window_total_seconds"),
            "planning_seconds": runtime.get("planning_seconds"),
            "resource_generation_seconds": runtime.get("resource_generation_seconds"),
            "calendar_regime_generation_seconds": runtime.get("regime_generation_seconds"),
            "cp_seconds_reported_by_frozen_profile": reported_cp_seconds,
            "cp_timing_status": cp_timing_status,
            "replay_seconds": runtime.get("replay_seconds"),
            "validation_seconds": runtime.get("validator_seconds"),
            "note": "runner instrumentation, not Codex/session duration",
        },
        "database_writes": 0,
    }
