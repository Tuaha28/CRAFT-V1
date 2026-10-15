"""Descriptive load context for a frozen, already-validated schedule.

This module never schedules work.  Exact precedence-ready counts are derived
from the neutral workload and the observed final schedule.  Resource checks
at an event instant are explicitly labelled *potential* because proving a
complete counterfactual fixed-crew execution would itself be a scheduling
problem.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from statistics import mean, median
from typing import Mapping, Sequence

from utils.shift_rotation import batch_available_at


def _percent(part, whole):
    return 100.0 * part / whole if whole else 0.0


def _distribution(values):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {"count": 0, "minimum": None, "median": None, "mean": None,
                "maximum": None, "p25": None, "p75": None, "p95": None}

    def percentile(fraction):
        index = (len(ordered) - 1) * fraction
        left = int(index)
        right = min(len(ordered) - 1, left + 1)
        weight = index - left
        return ordered[left] * (1.0 - weight) + ordered[right] * weight

    return {
        "count": len(ordered), "minimum": ordered[0], "median": median(ordered),
        "mean": mean(ordered), "maximum": ordered[-1],
        "p25": percentile(0.25), "p75": percentile(0.75), "p95": percentile(0.95),
    }


def audit_releases(neutral: Mapping, metrics: Mapping) -> dict:
    operations = neutral["operations"]
    by_order = defaultdict(list)
    for operation in operations:
        by_order[str(operation["order_id"])].append(operation)
    completion = metrics["order_completion_seconds"]
    rows = []
    for order in sorted(neutral["orders"], key=lambda row: str(row["order_id"])):
        order_id = str(order["order_id"])
        rows.append({
            "order_id": order_id,
            "release_seconds": int(order["release_seconds"]),
            "due_seconds": None if order.get("deadline_seconds") is None else int(order["deadline_seconds"]),
            "final_completion_seconds": int(completion[order_id]),
            "operation_count": len(by_order[order_id]),
        })
    releases = sorted({row["release_seconds"] for row in rows})
    gaps = [
        {"from_release_seconds": left, "to_release_seconds": right, "gap_seconds": right - left}
        for left, right in zip(releases, releases[1:])
    ]
    snapshot = int(neutral["snapshot_as_of_seconds"])
    future = [row for row in rows if row["release_seconds"] > snapshot]
    return {
        "orders": rows,
        "minimum_release_seconds": min(row["release_seconds"] for row in rows),
        "maximum_release_seconds": max(row["release_seconds"] for row in rows),
        "released_at_or_before_snapshot_count": sum(row["release_seconds"] <= snapshot for row in rows),
        "released_after_snapshot_count": len(future),
        "future_release_order_count": len(future),
        "future_release_order_ids": [row["order_id"] for row in future],
        "distinct_release_timestamps": releases,
        "release_event_gaps": gaps,
        "longest_interval_without_new_release_seconds": max((row["gap_seconds"] for row in gaps), default=0),
        "all_orders_released_at_snapshot": not future,
        "conclusion": (
            "LOW_UTILIZATION_NOT_EXPLAINED_BY_FUTURE_ORDER_RELEASES"
            if not future else "FUTURE_RELEASES_PRESENT"
        ),
    }


def resource_scale(neutral: Mapping) -> dict:
    operations = neutral["operations"]
    machines = neutral["machines"]
    workers = neutral["workers"]
    usable = [row for row in machines if bool(row.get("is_usable"))]
    capability_operations = Counter(str(row["required_capability"]) for row in operations)
    skill_workers = Counter()
    for worker in workers:
        for skill in worker.get("skills", ()):
            skill_workers[str(skill)] += 1
    eligible_line_counts = Counter(len(row.get("eligible_line_ids", ())) for row in operations)
    eligible_machine_counts = Counter(len(row.get("eligible_machine_ids", ())) for row in operations)
    eligible_worker_counts = Counter(len(row.get("eligible_worker_ids", ())) for row in operations)
    lines = neutral["lines"]
    return {
        "order_count": len(neutral["orders"]),
        "operation_count": len(operations),
        "line_count": len(lines),
        "total_machine_count": len(machines),
        "usable_machine_count": len(usable),
        "assignable_worker_count": len(workers),
        "worker_counts_by_skill": dict(sorted(skill_workers.items())),
        "operation_counts_by_required_capability": dict(sorted(capability_operations.items())),
        "operation_counts_by_eligible_line_count": {str(k): v for k, v in sorted(eligible_line_counts.items())},
        "operation_counts_by_eligible_machine_count": {str(k): v for k, v in sorted(eligible_machine_counts.items())},
        "operation_counts_by_eligible_worker_count": {str(k): v for k, v in sorted(eligible_worker_counts.items())},
        "eligible_line_count_distribution": _distribution(len(row.get("eligible_line_ids", ())) for row in operations),
        "eligible_machine_count_distribution": _distribution(len(row.get("eligible_machine_ids", ())) for row in operations),
        "eligible_worker_count_distribution": _distribution(len(row.get("eligible_worker_ids", ())) for row in operations),
        "descriptive_density": {
            "operations_per_usable_machine": len(operations) / len(usable) if usable else None,
            "operations_per_assignable_worker": len(operations) / len(workers) if workers else None,
            "orders_per_line": len(neutral["orders"]) / len(lines) if lines else None,
            "operations_per_line": len(operations) / len(lines) if lines else None,
            "classification": "DESCRIPTIVE_LOAD_CONTEXT_NOT_SCHEDULER_PERFORMANCE",
        },
    }


def resource_usage(neutral: Mapping, schedule_rows: Sequence[Mapping], metrics: Mapping) -> dict:
    operations = {str(row["operation_key"]): row for row in neutral["operations"]}
    productive = defaultdict(int)
    occupied = defaultdict(int)
    worker_line = defaultdict(lambda: defaultdict(int))
    machine_line = defaultdict(lambda: defaultdict(int))
    capability_worker = defaultdict(int)
    capability_machine = defaultdict(int)
    for row in schedule_rows:
        cfg = row["resource_configuration"]
        line_id = str(cfg["line_id"])
        capability = str(operations[str(row["operation_key"])]["required_capability"])
        worker_seconds = sum(int(right) - int(left) for left, right in row.get("worker_segments", ()))
        machine_seconds = int(row["end_seconds"]) - int(row["start_seconds"])
        for worker in cfg.get("worker_ids", ()):
            productive[str(worker)] += worker_seconds
            worker_line[line_id][str(worker)] += worker_seconds
            capability_worker[capability] += worker_seconds
        for machine in cfg.get("machine_ids", ()):
            occupied[int(machine)] += machine_seconds
            machine_line[line_id][str(machine)] += machine_seconds
            capability_machine[capability] += machine_seconds
    available_workers = int(metrics["worker_utilization"]["worker_count_in_denominator"])
    usable_machines = int(metrics["machine_utilization"]["machine_count_in_denominator"])
    return {
        "worker": {
            "total_productive_worker_seconds": sum(productive.values()),
            "distinct_workers_used": len(productive),
            "worker_count_available_in_denominator": available_workers,
            "productive_seconds_per_used_worker_distribution": _distribution(productive.values()),
            "productive_seconds_by_used_worker": dict(sorted(productive.items())),
            "available_workers_never_used": available_workers - len(productive),
            "percent_available_workers_never_used": _percent(available_workers - len(productive), available_workers),
            "percent_available_workers_used": _percent(len(productive), available_workers),
            "productive_seconds_by_capability": dict(sorted(capability_worker.items())),
            "by_line": {
                line: {"distinct_workers": len(values), "productive_worker_seconds": sum(values.values())}
                for line, values in sorted(worker_line.items())
            },
        },
        "machine": {
            "total_machine_occupied_seconds": sum(occupied.values()),
            "distinct_machines_used": len(occupied),
            "usable_machine_count_in_denominator": usable_machines,
            "occupied_seconds_per_used_machine_distribution": _distribution(occupied.values()),
            "occupied_seconds_by_used_machine": {str(k): v for k, v in sorted(occupied.items())},
            "usable_machines_never_used": usable_machines - len(occupied),
            "percent_usable_machines_never_used": _percent(usable_machines - len(occupied), usable_machines),
            "percent_usable_machines_used": _percent(len(occupied), usable_machines),
            "occupied_seconds_by_capability": dict(sorted(capability_machine.items())),
            "by_line": {
                line: {"distinct_machines": len(values), "occupied_machine_seconds": sum(values.values())}
                for line, values in sorted(machine_line.items())
            },
        },
    }


def _calendar_available(worker: Mapping, origin: datetime, second: int) -> bool:
    when = origin + timedelta(seconds=int(second))
    calendar = worker.get("calendar", {})
    kind = calendar.get("kind")
    if kind == "ROTATING_CREW":
        return batch_available_at(
            int(calendar["rotation_offset"]), int(calendar["rest_day_offset"]), when)
    if kind == "FIXED_SHIFT_FLOATER":
        start, end = float(calendar["start_hour"]), float(calendar["end_hour"])
        hour = when.hour + when.minute / 60 + when.second / 3600
        return start <= hour < end if start <= end else hour >= start or hour < end
    return False


def _overlap_at(intervals, instant):
    return any(int(left) <= int(instant) < int(right) for left, right in intervals)


def potential_resource_feasibility(
    operation: Mapping,
    instant: int,
    *,
    machines_by_id: Mapping[int, Mapping],
    workers_by_id: Mapping[str, Mapping],
    busy_machines: set[int],
    busy_workers: set[str],
    machine_commitments: Mapping[int, Sequence],
    worker_commitments: Mapping[str, Sequence],
    origin: datetime,
) -> dict:
    """Current-instant resource screen, not a full-execution feasibility proof."""
    k = int(operation.get("min_machine_count", 1))
    eligible_machines = [int(value) for value in operation.get("eligible_machine_ids", ())]
    eligible_workers = [str(value) for value in operation.get("eligible_worker_ids", ())]
    line_ids = [int(value) for value in operation.get("eligible_line_ids", ())]
    available_workers = [
        worker for worker in eligible_workers
        if worker in workers_by_id
        and worker not in busy_workers
        and not _overlap_at(worker_commitments.get(worker, ()), instant)
        and _calendar_available(workers_by_id[worker], origin, instant)
    ]
    best_machine_count = 0
    for line_id in line_ids:
        line_machines = [
            machine for machine in eligible_machines
            if machine in machines_by_id
            and int(machines_by_id[machine]["line_id"]) == line_id
            and bool(machines_by_id[machine].get("is_usable"))
            and machine not in busy_machines
            and not _overlap_at(machine_commitments.get(machine, ()), instant)
        ]
        best_machine_count = max(best_machine_count, len(line_machines))
        if len(line_machines) >= k and len(available_workers) >= k:
            return {
                "potentially_executable_now": True,
                "minimum_k": k,
                "available_eligible_workers": len(available_workers),
                "available_eligible_machines_best_line": len(line_machines),
                "reason": "CURRENT_INSTANT_MACHINE_AND_CREW_CAPACITY_EXISTS",
            }
    reason = "INCOMPATIBLE_OR_BUSY_MACHINE" if best_machine_count < k else "INSUFFICIENT_AVAILABLE_WORKER_CREW"
    return {
        "potentially_executable_now": False,
        "minimum_k": k,
        "available_eligible_workers": len(available_workers),
        "available_eligible_machines_best_line": best_machine_count,
        "reason": reason,
    }


def _weighted_median(intervals, field):
    total = sum(int(row["duration_seconds"]) for row in intervals)
    if not total:
        return 0
    cumulative = 0
    for value, duration in sorted((int(row[field]), int(row["duration_seconds"])) for row in intervals):
        cumulative += duration
        if cumulative * 2 >= total:
            return value
    return 0


def build_ready_work_timeline(neutral: Mapping, schedule_rows: Sequence[Mapping], metrics: Mapping) -> dict:
    origin = datetime.fromisoformat(neutral["evaluation_origin"])
    horizon = int(metrics["absolute_last_completion_seconds"])
    operations = {str(row["operation_key"]): row for row in neutral["operations"]}
    schedule = {str(row["operation_key"]): row for row in schedule_rows}
    workers = {str(row["worker_id"]): row for row in neutral["workers"]}
    machines = {int(row["machine_id"]): row for row in neutral["machines"]}
    predecessors = {key: tuple(str(x) for x in row.get("predecessor_operation_keys", ()))
                    for key, row in operations.items()}
    completions = {key: int(row["end_seconds"]) for key, row in schedule.items()}
    starts = {key: int(row["start_seconds"]) for key, row in schedule.items()}
    order_release = {str(row["order_id"]): int(row["release_seconds"]) for row in neutral["orders"]}

    worker_busy = defaultdict(list)
    machine_busy = defaultdict(list)
    events = {0, horizon}
    for key, row in schedule.items():
        start, end = int(row["start_seconds"]), int(row["end_seconds"])
        events.update((max(0, start), min(horizon, end)))
        cfg = row["resource_configuration"]
        for machine in cfg.get("machine_ids", ()):
            machine_busy[int(machine)].append((start, end))
        for worker in cfg.get("worker_ids", ()):
            for left, right in row.get("worker_segments", ()):
                worker_busy[str(worker)].append((int(left), int(right)))
                events.update((max(0, int(left)), min(horizon, int(right))))
    machine_commitments = defaultdict(list)
    worker_commitments = defaultdict(list)
    for row in neutral["resource_commitments"]["machine"]:
        machine_commitments[int(row["machine_id"])].append((int(row["start_seconds"]), int(row["end_seconds"])))
        events.update((max(0, int(row["start_seconds"])), min(horizon, int(row["end_seconds"]))))
    for row in neutral["resource_commitments"]["worker"]:
        worker_commitments[str(row["employee_id"])].append((int(row["start_seconds"]), int(row["end_seconds"])))
        events.update((max(0, int(row["start_seconds"])), min(horizon, int(row["end_seconds"]))))
    for release in order_release.values():
        if 0 <= release <= horizon:
            events.add(release)

    # Add only actual calendar transition clock-times represented by the
    # neutral worker calendars, for each day in the horizon.
    clock_seconds = {0}
    for worker in workers.values():
        calendar = worker.get("calendar", {})
        if calendar.get("kind") == "ROTATING_CREW":
            clock_seconds.update((6 * 3600, 14 * 3600, 22 * 3600))
        elif calendar.get("kind") == "FIXED_SHIFT_FLOATER":
            clock_seconds.add(int(round(float(calendar["start_hour"]) * 3600)) % 86400)
            clock_seconds.add(int(round(float(calendar["end_hour"]) * 3600)) % 86400)
    day = origin.date() - timedelta(days=1)
    last_day = (origin + timedelta(seconds=horizon)).date() + timedelta(days=1)
    while day <= last_day:
        midnight = datetime.combine(day, time.min)
        for seconds in clock_seconds:
            instant = int((midnight + timedelta(seconds=seconds) - origin).total_seconds())
            if 0 < instant < horizon:
                events.add(instant)
        day += timedelta(days=1)
    points = sorted(event for event in events if 0 <= event <= horizon)

    intervals = []
    feasibility_reason_counts = Counter()
    for left, right in zip(points, points[1:]):
        if right <= left:
            continue
        running = sorted(key for key in schedule if starts[key] <= left < completions[key])
        pending = [key for key in schedule if starts[key] > left]
        ready = sorted(
            key for key in pending
            if order_release[str(operations[key]["order_id"])] <= left
            and all(completions[pred] <= left for pred in predecessors[key])
        )
        busy_machine_ids = {machine for machine, spans in machine_busy.items() if _overlap_at(spans, left)}
        busy_worker_ids = {worker for worker, spans in worker_busy.items() if _overlap_at(spans, left)}
        potential = []
        screens = {}
        for key in ready:
            screen = potential_resource_feasibility(
                operations[key], left, machines_by_id=machines, workers_by_id=workers,
                busy_machines=busy_machine_ids, busy_workers=busy_worker_ids,
                machine_commitments=machine_commitments, worker_commitments=worker_commitments,
                origin=origin,
            )
            screens[key] = screen
            feasibility_reason_counts[screen["reason"]] += 1
            if screen["potentially_executable_now"]:
                potential.append(key)
        intervals.append({
            "start_seconds": left,
            "end_seconds": right,
            "duration_seconds": right - left,
            "unfinished_operation_count": sum(completions[key] > left for key in schedule),
            "precedence_ready_operation_count": len(ready),
            "precedence_ready_operation_keys": ready,
            "potentially_executable_now_count": len(potential),
            "potentially_executable_now_keys": potential,
            "potential_resource_screen": screens,
            "legally_executable_operation_count": None,
            "running_operation_count": len(running),
            "running_operation_keys": running,
            "active_worker_count": len(busy_worker_ids),
            "active_machine_count": len(busy_machine_ids),
        })
    total = sum(row["duration_seconds"] for row in intervals)
    ready_buckets = {"zero": 0, "one": 0, "two_to_five": 0, "six_to_ten": 0, "over_ten": 0}
    potential_buckets = {key: 0 for key in ready_buckets}

    def bucket(count):
        if count == 0: return "zero"
        if count == 1: return "one"
        if count <= 5: return "two_to_five"
        if count <= 10: return "six_to_ten"
        return "over_ten"

    for row in intervals:
        ready_buckets[bucket(row["precedence_ready_operation_count"])] += row["duration_seconds"]
        potential_buckets[bucket(row["potentially_executable_now_count"])] += row["duration_seconds"]
    ready_counts = [row["precedence_ready_operation_count"] for row in intervals]
    running_counts = [row["running_operation_count"] for row in intervals]
    zero_running = sum(row["duration_seconds"] for row in intervals if row["running_operation_count"] == 0)
    longest_zero_running = max((row["duration_seconds"] for row in intervals if row["running_operation_count"] == 0), default=0)
    return {
        "timeline_semantics": {
            "precedence_ready": "released, not yet started, and all route predecessors completed in frozen schedule",
            "potentially_executable_now": "current-instant screen: compatible usable idle machine count and eligible currently available idle worker count meet minimum k",
            "legally_executable": "NOT_EXACTLY_PROVEN: full fixed-crew future execution and counterfactual resource interactions are not solved",
        },
        "interval_count": len(intervals),
        "evaluation_horizon_seconds": horizon,
        "intervals": intervals,
        "ready_work_density": {
            "bucket_seconds": ready_buckets,
            "bucket_percent": {key: _percent(value, total) for key, value in ready_buckets.items()},
            "mean_ready_operation_count": sum(row["precedence_ready_operation_count"] * row["duration_seconds"] for row in intervals) / total if total else 0,
            "median_ready_operation_count": _weighted_median(intervals, "precedence_ready_operation_count"),
            "maximum_ready_operation_count": max(ready_counts, default=0),
            "percent_time_zero_ready_work": _percent(ready_buckets["zero"], total),
            "percent_time_with_ready_work": _percent(total - ready_buckets["zero"], total),
        },
        "potential_executable_density": {
            "status": "CURRENT_INSTANT_SCREEN_ONLY",
            "bucket_seconds": potential_buckets,
            "bucket_percent": {key: _percent(value, total) for key, value in potential_buckets.items()},
            "feasibility_reason_observation_counts": dict(feasibility_reason_counts),
        },
        "actual_concurrency": {
            "maximum_running_operations": max(running_counts, default=0),
            "mean_running_operations": sum(row["running_operation_count"] * row["duration_seconds"] for row in intervals) / total if total else 0,
            "median_running_operations": _weighted_median(intervals, "running_operation_count"),
            "time_with_zero_operations_running_seconds": zero_running,
            "time_with_at_least_one_operation_running_seconds": total - zero_running,
            "longest_interval_with_zero_operations_running_seconds": longest_zero_running,
        },
    }


def parallelism(neutral: Mapping, schedule_rows: Sequence[Mapping], timeline: Mapping) -> dict:
    operations = {str(row["operation_key"]): row for row in neutral["operations"]}
    by_line = defaultdict(list)
    by_capability = defaultdict(list)
    for row in schedule_rows:
        key = str(row["operation_key"])
        by_line[str(row["resource_configuration"]["line_id"])].append(row)
        by_capability[str(operations[key]["required_capability"])].append(row)

    def max_concurrency(rows):
        events = []
        for row in rows:
            events.append((int(row["start_seconds"]), 1))
            events.append((int(row["end_seconds"]), -1))
        current = best = 0
        # Half-open: process endings before starts at the same instant.
        for _at, delta in sorted(events, key=lambda item: (item[0], item[1])):
            current += delta
            best = max(best, current)
        return best

    density = timeline["ready_work_density"]
    concurrency = timeline["actual_concurrency"]
    return {
        "maximum_theoretical_precedence_ready_parallelism_observed": density["maximum_ready_operation_count"],
        "maximum_actual_simultaneously_running_operations": concurrency["maximum_running_operations"],
        "mean_actual_simultaneously_running_operations": concurrency["mean_running_operations"],
        "median_actual_simultaneously_running_operations": concurrency["median_running_operations"],
        "maximum_actual_concurrency_by_line": {key: max_concurrency(rows) for key, rows in sorted(by_line.items())},
        "maximum_actual_concurrency_by_capability": {key: max_concurrency(rows) for key, rows in sorted(by_capability.items())},
        "warning": "precedence-ready parallelism is not claimed as physically executable parallelism",
    }


def flow_tardiness_context(neutral: Mapping, metrics: Mapping) -> dict:
    completion = metrics["order_completion_seconds"]
    rows = []
    for order in sorted(neutral["orders"], key=lambda row: str(row["order_id"])):
        order_id = str(order["order_id"])
        release = int(order["release_seconds"])
        due = None if order.get("deadline_seconds") is None else int(order["deadline_seconds"])
        end = int(completion[order_id])
        flow = end - release
        if flow < 0:
            raise ValueError(f"completion precedes release for order {order_id}")
        rows.append({
            "order_id": order_id, "release_seconds": release, "due_seconds": due,
            "completion_seconds": end, "due_date_allowance_seconds": None if due is None else due - release,
            "flow_time_seconds": flow,
            "tardiness_seconds": None if due is None else max(0, end - due),
            "routing_operation_count": sum(str(op["order_id"]) == order_id for op in neutral["operations"]),
        })
    flow_values = [row["flow_time_seconds"] for row in rows]
    allowances = [row["due_date_allowance_seconds"] for row in rows if row["due_date_allowance_seconds"] is not None]
    return {
        "orders": rows,
        "flow_time_distribution": _distribution(flow_values),
        "due_date_allowance_distribution": _distribution(allowances),
        "orders_released_before_evaluation_origin": sum(row["release_seconds"] < 0 for row in rows),
        "orders_released_at_evaluation_origin": sum(row["release_seconds"] == 0 for row in rows),
        "orders_released_after_evaluation_origin": sum(row["release_seconds"] > 0 for row in rows),
        "completion_precedes_release_count": sum(row["completion_seconds"] < row["release_seconds"] for row in rows),
        "interpretation": "Flow time includes time from authoritative release even when release precedes evaluation origin; route depth is reported per order. Causal attribution between route depth, resource waits, and policy would require a counterfactual schedule and is not claimed.",
    }
