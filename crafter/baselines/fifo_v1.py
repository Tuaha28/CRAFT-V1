"""Event-driven FIFO baseline over the neutral factory workload.

Only the dispatch priority is FIFO.  Calendar execution, fixed-crew
pause/resume, continuous machine occupancy, fixed commitments, and the
independent exported-schedule validator reuse authoritative physical
primitives.  No CRAFT neighborhood, CP model, objective, or accepted schedule
is consumed.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
import math
import time
from typing import Mapping, Sequence

from crafter.authoritative_semantics import authoritative_changeover_seconds
from crafter.contracts import (
    FixedMachineCommitment,
    FixedWorkerCommitment,
    LineSpec,
    MachineSpec,
    OperationSpec,
    OrderSpec,
    ResourceState,
    SchedulingProblem,
)
from crafter.evaluation.neutral_factory import NeutralFactoryWorkload
from crafter.lns_probe.joint_modes import _fixed_crew_execution
from crafter.repair.resource_configs import ResourceConfiguration
from crafter.repair.scoped_validation import replay_assembled_phenotype
from crafter.repair.validation_scope import ValidationScopeV1, scope_completeness
from crafter.repair.window_phenotype import WindowPhenotypeV1
from ga.worker_allocation import operator_available_at
from validation.schedule_validator import ScheduleValidationContext, validate_exported_schedule


def _worker_token(value: str):
    return int(value) if str(value).isdigit() else str(value)


def build_workload_from_neutral_payload(payload: Mapping) -> NeutralFactoryWorkload:
    """Hydrate authoritative contracts from the serialized neutral artifact."""
    lines = tuple(LineSpec(**{
        "line_id": int(row["line_id"]), "factory_id": int(row["factory_id"]),
        "name": str(row.get("name", "")),
    }) for row in payload["lines"])
    machines = tuple(MachineSpec(
        int(row["machine_id"]), int(row["factory_id"]), int(row["line_id"]),
        tuple(str(x) for x in row.get("capabilities", ())), bool(row.get("is_usable", True)),
    ) for row in payload["machines"])
    orders = tuple(OrderSpec(
        str(row["order_id"]), int(row["factory_id"]), int(row["release_seconds"]),
        None if row.get("deadline_seconds") is None else int(row["deadline_seconds"]),
        int(row.get("priority_weight", 1)),
    ) for row in payload["orders"])
    operations = tuple(OperationSpec(
        str(row["operation_key"]), str(row["order_id"]), str(row["operation_id"]),
        int(row["sequence"]), str(row["required_capability"]),
        tuple(int(x) for x in row.get("eligible_line_ids", ())),
        int(row["serial_productive_seconds"]), int(row.get("setup_seconds", 0)),
        int(row.get("min_machine_count", 1)), int(row.get("max_machine_count", 1)),
    ) for row in payload["operations"])
    machine_commitments = tuple(FixedMachineCommitment(
        int(row["machine_id"]), int(row["start_seconds"]), int(row["end_seconds"]),
        str(row["production_order_id"]),
    ) for row in payload["resource_commitments"]["machine"])
    worker_commitments = tuple(FixedWorkerCommitment(
        str(row["employee_id"]), int(row["factory_id"]), str(row["skill"]),
        int(row["start_seconds"]), int(row["end_seconds"]),
        str(row.get("operation_key", "fixed")),
    ) for row in payload["resource_commitments"]["worker"])
    # The baseline has no planning horizon.  This field is only a structural
    # domain contract for helpers; the event loop terminates on operation
    # accounting, not this generous non-policy bound.
    horizon = max(31_536_000, max((int(o.release_seconds) for o in orders), default=0) + 31_536_000)
    problem = SchedulingProblem(
        problem_id="neutral-factory-baseline", origin=datetime.fromisoformat(payload["evaluation_origin"]),
        horizon_seconds=horizon, lines=lines, machines=machines, orders=orders,
        operations=operations, labour_capacities=(),
        resources=ResourceState(machine_commitments, worker_commitments),
    )

    by_factory = defaultdict(list)
    by_skill = defaultdict(list)
    crew_of = {}
    shift_of = {}
    for row in payload["workers"]:
        worker_id = str(row["worker_id"])
        token = _worker_token(worker_id)
        for skill in row.get("skills", ()):
            normalized = str(skill).strip().lower()
            by_factory[(int(row["factory_id"]), normalized)].append(token)
            by_skill[normalized].append(token)
        calendar = row.get("calendar", {})
        if calendar.get("kind") == "ROTATING_CREW":
            crew_of[token] = (int(calendar["rotation_offset"]), int(calendar["rest_day_offset"]))
        elif calendar.get("kind") == "FIXED_SHIFT_FLOATER":
            shift_of[token] = (float(calendar["start_hour"]), float(calendar["end_hour"]))
    operator_index = {
        "by_factory": {key: tuple(sorted(set(values), key=str)) for key, values in by_factory.items()},
        "by_skill": {key: tuple(sorted(set(values), key=str)) for key, values in by_skill.items()},
        "by_line": {},
        "crew_of": crew_of,
        "shift_of": shift_of,
        "line_factory": {int(row["line_id"]): int(row["factory_id"]) for row in payload["lines"]},
    }
    return NeutralFactoryWorkload(dict(payload), problem, operator_index)


def fifo_priority_key(operation: Mapping, queue_entry_time: int) -> tuple:
    """The complete stable FIFO priority contract."""
    return (
        int(queue_entry_time), str(operation["order_id"]), int(operation["sequence"]),
        str(operation["operation_key"]),
    )


def edd_priority_key(operation: Mapping, queue_entry_time: int, order_due: Mapping) -> tuple:
    """EDD priority for a currently ready operation.

    Missing due dates are deliberately ordered after all valid due dates and
    remain deterministic through the FIFO queue-entry/tie-break fields.
    """
    due = order_due.get(str(operation["order_id"]))
    return (
        due is None,
        int(due) if due is not None else 0,
        int(queue_entry_time),
        str(operation["order_id"]), int(operation["sequence"]),
        str(operation["operation_key"]),
    )


def spt_priority_key(operation: Mapping, queue_entry_time: int) -> tuple:
    """SPT priority using intrinsic neutral serial productive work only."""
    return (
        int(operation["serial_productive_seconds"]), int(queue_entry_time),
        str(operation["order_id"]), int(operation["sequence"]),
        str(operation["operation_key"]),
    )


def _dispatch_priority_key(policy: str, operation: Mapping, queue_entry_time: int,
                           order_due: Mapping) -> tuple:
    policy = str(policy).upper()
    if policy == "FIFO":
        return fifo_priority_key(operation, queue_entry_time)
    if policy == "EDD":
        return edd_priority_key(operation, queue_entry_time, order_due)
    if policy == "SPT":
        return spt_priority_key(operation, queue_entry_time)
    raise ValueError(f"unsupported baseline dispatch policy: {policy}")


def _calendar_key(worker: Mapping) -> tuple:
    calendar = worker.get("calendar", {})
    kind = str(calendar.get("kind", "UNRESOLVED"))
    if kind == "ROTATING_CREW":
        return (kind, int(calendar["rotation_offset"]), int(calendar["rest_day_offset"]))
    if kind == "FIXED_SHIFT_FLOATER":
        return (kind, float(calendar["start_hour"]), float(calendar["end_hour"]))
    return (kind, str(worker["worker_id"]))


def _compatible_calendar_key(key: tuple) -> tuple:
    if key and key[0] == "ROTATING_CREW":
        return ("ROTATING_CREW_SHIFT", key[1])
    return key


def _append_interval(mapping, key, interval):
    values = mapping.setdefault(key, [])
    values.append(interval)
    values.sort()


def _machine_accepts(reservations: Sequence[Mapping], start: int, end: int, order_id: str) -> bool:
    """Check overlap and sequence-dependent changeover against adjacent uses."""
    rows = sorted(reservations, key=lambda row: (int(row["start"]), int(row["end"]), str(row["key"])))
    previous = None
    following = None
    for row in rows:
        left, right = int(row["start"]), int(row["end"])
        if left < end and start < right:
            return False
        if right <= start and (previous is None or right > int(previous["end"])):
            previous = row
        if left >= end and (following is None or left < int(following["start"])):
            following = row
    if previous is not None:
        gap = authoritative_changeover_seconds(previous.get("order_id"), order_id)
        if start < int(previous["end"]) + int(gap):
            return False
    if following is not None:
        gap = authoritative_changeover_seconds(order_id, following.get("order_id"))
        if end + int(gap) > int(following["start"]):
            return False
    return True


def _crew_candidates(operation, k, productive_ready, productive_minutes, *, workload,
                     workers_by_id, worker_reservations, expanded=False,
                     stop_after_disjoint_from=frozenset()):
    """Compact deterministic fixed-crew construction without combinations."""
    eligible = [str(x) for x in operation.get("eligible_worker_ids", ()) if str(x) in workers_by_id]
    exact = defaultdict(list)
    compatible = defaultdict(list)
    for worker_id in sorted(set(eligible)):
        key = _calendar_key(workers_by_id[worker_id])
        exact[key].append(worker_id)
        compatible[_compatible_calendar_key(key)].append(worker_id)
    pools = []
    seen_pools = set()
    for groups in (exact, compatible):
        for group_key in sorted(groups, key=str):
            pool = tuple(sorted(set(groups[group_key])))
            if len(pool) >= k and pool not in seen_pools:
                seen_pools.add(pool)
                pools.append(pool)
    # Exact roster cohorts are the authoritative normal case.  The compatible
    # shift fallback permits different rest-day offsets but still verifies the
    # full fixed crew with the exact calendar walker.
    candidates = []
    selected_options = []
    for pool in pools:
        offsets = range(len(pool) - k + 1) if expanded else (0,)
        for offset in offsets:
            selected = tuple(pool[offset:offset + k])
            if selected not in selected_options:
                selected_options.append(selected)
    for selected in sorted(selected_options):
        try:
            end_at, segments = _fixed_crew_execution(
                workload.problem, workload.operator_index, selected,
                productive_ready, productive_minutes, worker_reservations,
            )
        except (ValueError, OverflowError):
            continue
        candidates.append((
            selected,
            int(round((end_at - workload.problem.origin).total_seconds())),
            tuple((int(round((a - workload.problem.origin).total_seconds())),
                   int(round((b - workload.problem.origin).total_seconds()))) for a, b in segments),
        ))
        # Every earlier lexicographic sliding witness was retained.  A crew
        # disjoint from all previously assigned workers cannot alter a prior
        # replay, so later witnesses cannot beat it under the worker-ID tie.
        if expanded and not (set(selected) & set(stop_after_disjoint_from)):
            break
    candidates.sort(key=lambda row: row[0])
    return candidates, {
        "eligible_workers": len(set(eligible)), "exact_calendar_groups": len(exact),
        "compatible_calendar_groups": len(compatible), "candidate_crews": len(candidates),
        "method": "COMPACT_DETERMINISTIC_FIXED_CREW_MATCHING",
        "combinations_enumerated": 0, "expanded_sliding_matching": bool(expanded),
    }


def _preserves_existing_replay(candidate, schedule_rows, worker_reservations, workload):
    """Ensure a later greedy choice does not change any prior exact execution.

    Worker pauses permit useful interleaving, but a later reservation may not
    shift an already materialized operation's authoritative segments.  This
    incremental fixed-point check is the same semantic condition enforced by
    assembled replay and is not an optimization objective.
    """
    candidate_workers = set(map(str, candidate["worker_ids"]))
    affected = [row for row in schedule_rows
                if candidate_workers & set(map(str, row["resource_configuration"]["worker_ids"]))]
    if not affected:
        return True
    origin = workload.problem.origin
    after = {str(worker): list(values) for worker, values in worker_reservations.items()}
    for worker in candidate_workers:
        for left, right in candidate["worker_segments"]:
            after.setdefault(worker, []).append((
                origin + timedelta(seconds=int(left)),
                origin + timedelta(seconds=int(right))))
        after[worker].sort()
    for row in affected:
        cfg = row["resource_configuration"]
        workers = tuple(str(x) for x in cfg["worker_ids"])
        local = {worker: list(after.get(worker, ())) for worker in workers}
        own = [(origin + timedelta(seconds=int(a)), origin + timedelta(seconds=int(b)))
               for a, b in row["worker_segments"]]
        for worker in workers:
            values = local[worker]
            for segment in own:
                if segment in values:
                    values.remove(segment)
        ready = origin + timedelta(seconds=int(row["start_seconds"]) + int(row["setup_seconds"]))
        try:
            end, segments = _fixed_crew_execution(
                workload.problem, workload.operator_index, workers, ready,
                float(row["productive_minutes"]), local)
        except (ValueError, OverflowError):
            return False
        actual_end = int(round((end - origin).total_seconds()))
        actual_segments = tuple((
            int(round((a - origin).total_seconds())),
            int(round((b - origin).total_seconds()))) for a, b in segments)
        if actual_end != int(row["end_seconds"]) or actual_segments != tuple(
                tuple(x) for x in row["worker_segments"]):
            return False
    return True


def _find_assignment_at(operation, current, *, workload, payload_operation,
                        workers_by_id, machines_by_id, worker_reservations,
                        machine_reservations, schedule_rows=()):
    k = int(operation.min_machine_count)
    if k != int(operation.max_machine_count):
        # Explore every legal k deterministically rather than selecting a
        # policy-quality value.  Factory 2 currently has fixed k per op.
        legal_k = range(int(operation.min_machine_count), int(operation.max_machine_count) + 1)
    else:
        legal_k = (k,)
    all_candidates = []
    matching_audits = []
    stability_rejections = 0
    previously_used_workers = {
        str(worker) for row in schedule_rows
        for worker in row["resource_configuration"]["worker_ids"]
    }
    for expanded in (False, True):
        tier_candidates = []
        for k in legal_k:
            productive_minutes = float(operation.serial_productive_seconds) / float(k) / 60.0
            productive_seconds = int(math.ceil(float(operation.serial_productive_seconds) / float(k)))
            ready = workload.problem.origin + timedelta(seconds=int(current + operation.setup_seconds))
            crews, matching = _crew_candidates(
                payload_operation, k, ready, productive_minutes, workload=workload,
                workers_by_id=workers_by_id, worker_reservations=worker_reservations,
                expanded=expanded,
                stop_after_disjoint_from=previously_used_workers,
            )
            matching_audits.append({"k": k, **matching})
            for workers, end, segments in crews:
                temporal_candidate = {
                    "worker_ids": tuple(workers), "worker_segments": tuple(segments),
                }
                if not _preserves_existing_replay(
                        temporal_candidate, schedule_rows, worker_reservations, workload):
                    stability_rejections += 1
                    continue
                for line_id in sorted(int(x) for x in operation.eligible_line_ids):
                    pool = []
                    for machine_id in sorted(int(x) for x in payload_operation.get("eligible_machine_ids", ())):
                        machine = machines_by_id.get(machine_id)
                        if machine is None or not bool(machine.get("is_usable")):
                            continue
                        if int(machine["line_id"]) != line_id:
                            continue
                        if _machine_accepts(
                            machine_reservations.get(machine_id, ()), int(current), int(end),
                            str(operation.order_id),
                        ):
                            pool.append(machine_id)
                    if len(pool) < k:
                        continue
                    machines = tuple(pool[:k])
                    candidate = {
                        "line_id": line_id, "k": k, "machine_ids": machines,
                        "worker_ids": tuple(workers), "start_seconds": int(current),
                        "end_seconds": int(end), "worker_segments": tuple(segments),
                        "setup_seconds": int(operation.setup_seconds),
                        "productive_seconds": productive_seconds,
                        "productive_minutes": productive_minutes,
                    }
                    tier_candidates.append(candidate)
        if tier_candidates:
            all_candidates = tier_candidates
            break
    all_candidates.sort(key=lambda row: (
        int(row["line_id"]), tuple(row["machine_ids"]), tuple(row["worker_ids"]), int(row["k"])
    ))
    return (all_candidates[0] if all_candidates else None), {
        "matching": matching_audits,
        "legal_lines_examined": len(operation.eligible_line_ids),
        "candidate_assignments": len(all_candidates),
        "machine_sets_enumerated": 0,
        "replay_stability_rejections": stability_rejections,
    }


def _next_calendar_boundary(origin: datetime, current: int) -> int:
    now = origin + timedelta(seconds=int(current))
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = []
    for offset in (0, 1, 2):
        base = day + timedelta(days=offset)
        for hour in (0, 6, 14, 22, 24):
            value = int((base + timedelta(hours=hour) - origin).total_seconds())
            if value > current:
                candidates.append(value)
    return min(candidates)


def _next_event(current, *, unscheduled, operations, order_release, completions,
                worker_reservations, machine_reservations, origin):
    values = []
    for key in unscheduled:
        release = order_release[str(operations[key]["order_id"])]
        if release > current:
            values.append(release)
        for predecessor in operations[key].get("predecessor_operation_keys", ()):
            completion = completions.get(str(predecessor))
            if completion is not None and completion > current:
                values.append(completion)
    for intervals in worker_reservations.values():
        for start, end in intervals:
            for instant in (
                int((start - origin).total_seconds()), int((end - origin).total_seconds())
            ):
                if instant > current:
                    values.append(instant)
    for intervals in machine_reservations.values():
        for row in intervals:
            end = int(row["end"])
            if end > current:
                values.append(end)
            change_end = end + int(authoritative_changeover_seconds(row.get("order_id"), None))
            if change_end > current:
                values.append(change_end)
    values.append(_next_calendar_boundary(origin, current))
    return min(values) if values else None


def run_fifo_schedule(workload: NeutralFactoryWorkload, *, policy: str = "FIFO") -> dict:
    """Run a deterministic event-driven dispatch baseline.

    The default is the validated FIFO baseline.  EDD changes only the ready
    queue priority; all physical/resource allocation and replay-stability
    semantics are shared.
    """
    policy = str(policy).upper()
    if policy not in {"FIFO", "EDD", "SPT"}:
        raise ValueError(f"unsupported baseline dispatch policy: {policy}")
    started = time.perf_counter()
    payload = workload.payload
    problem = workload.problem
    operations = {str(row["operation_key"]): row for row in payload["operations"]}
    spec = problem.operation_by_key
    order_release = {str(row["order_id"]): int(row["release_seconds"]) for row in payload["orders"]}
    order_due = {
        str(row["order_id"]): (None if row.get("deadline_seconds") is None
                                else int(row["deadline_seconds"]))
        for row in payload["orders"]
    }
    workers_by_id = {str(row["worker_id"]): row for row in payload["workers"]}
    machines_by_id = {int(row["machine_id"]): row for row in payload["machines"]}
    worker_reservations = defaultdict(list)
    machine_reservations = defaultdict(list)
    origin = problem.origin
    for row in payload["resource_commitments"]["worker"]:
        _append_interval(worker_reservations, str(row["employee_id"]), (
            origin + timedelta(seconds=int(row["start_seconds"])),
            origin + timedelta(seconds=int(row["end_seconds"])),
        ))
    for row in payload["resource_commitments"]["machine"]:
        machine_reservations[int(row["machine_id"])].append({
            "start": int(row["start_seconds"]), "end": int(row["end_seconds"]),
            "order_id": str(row["production_order_id"]), "key": "fixed",
        })
    for values in machine_reservations.values():
        values.sort(key=lambda row: (row["start"], row["end"], row["key"]))

    unscheduled = set(operations)
    completions = {}
    queue_entry = {}
    schedule = []
    event_log = []
    allocation_profile = Counter()
    current = max(0, int(payload.get("snapshot_as_of_seconds", 0)))
    visited = set()
    failure = None

    while unscheduled:
        state_signature = (current, tuple(sorted(unscheduled)), len(schedule))
        if state_signature in visited:
            failure = {"classification": "EVENT_ADVANCE_ERROR", "reason": "repeated event state"}
            break
        visited.add(state_signature)

        ready = []
        for key in unscheduled:
            row = operations[key]
            predecessors = tuple(str(x) for x in row.get("predecessor_operation_keys", ()))
            if any(pred not in completions for pred in predecessors):
                continue
            entered = max(
                order_release[str(row["order_id"])],
                max((completions[pred] for pred in predecessors), default=order_release[str(row["order_id"])]),
            )
            queue_entry[key] = entered
            if entered <= current:
                ready.append(key)
        ready.sort(key=lambda key: _dispatch_priority_key(
            policy, operations[key], queue_entry[key], order_due))

        scheduled_now = []
        blocked = []
        for key in ready:
            assignment, audit = _find_assignment_at(
                spec[key], current, workload=workload, payload_operation=operations[key],
                workers_by_id=workers_by_id, machines_by_id=machines_by_id,
                worker_reservations=worker_reservations,
                machine_reservations=machine_reservations,
                schedule_rows=schedule,
            )
            allocation_profile["operations_considered"] += 1
            allocation_profile["candidate_assignments"] += int(audit["candidate_assignments"])
            allocation_profile["machine_sets_enumerated"] += int(audit["machine_sets_enumerated"])
            allocation_profile["worker_combinations_enumerated"] += sum(
                int(row["combinations_enumerated"]) for row in audit["matching"])
            allocation_profile["replay_stability_rejections"] += int(
                audit.get("replay_stability_rejections", 0))
            if assignment is None:
                blocked.append({"operation_key": key, "queue_entry_time": queue_entry[key], **audit})
                continue
            cfg = ResourceConfiguration(
                key, int(payload["factory_id"]), int(assignment["line_id"]), int(assignment["k"]),
                tuple(assignment["machine_ids"]), tuple(assignment["worker_ids"]),
                int(assignment["setup_seconds"]), int(assignment["productive_seconds"]),
                (f"{policy}_POLICY_NEUTRAL_EARLIEST_FEASIBLE",),
                float(assignment["productive_minutes"]),
            )
            row = {
                "operation_key": key, "order_id": str(spec[key].order_id),
                "regime_index": None,
                "queue_entry_time": int(queue_entry[key]),
                "start_seconds": int(assignment["start_seconds"]),
                "end_seconds": int(assignment["end_seconds"]),
                "worker_segments": [list(x) for x in assignment["worker_segments"]],
                "setup_seconds": int(assignment["setup_seconds"]),
                "productive_seconds": int(assignment["productive_seconds"]),
                "productive_minutes": float(assignment["productive_minutes"]),
                "resource_configuration": asdict(cfg),
            }
            schedule.append(row)
            unscheduled.remove(key)
            completions[key] = int(row["end_seconds"])
            scheduled_now.append(key)
            for worker in assignment["worker_ids"]:
                for left, right in assignment["worker_segments"]:
                    _append_interval(worker_reservations, str(worker), (
                        origin + timedelta(seconds=int(left)), origin + timedelta(seconds=int(right))))
            for machine in assignment["machine_ids"]:
                machine_reservations[int(machine)].append({
                    "start": int(row["start_seconds"]), "end": int(row["end_seconds"]),
                    "order_id": str(row["order_id"]), "key": key,
                })
                machine_reservations[int(machine)].sort(
                    key=lambda value: (value["start"], value["end"], value["key"]))

        event_log.append({
            "time_seconds": current, "ready_count": len(ready), "ready_operations": ready,
            "scheduled_count": len(scheduled_now), "scheduled_operations": scheduled_now,
            "blocked_count": len(blocked), "blocked": blocked,
            "remaining_after": len(unscheduled),
        })
        if not unscheduled:
            break
        if ready and not scheduled_now:
            # Calendar/reservation timing can make a structurally legal
            # assignment unavailable at this event, but no future event can
            # manufacture a missing crew cohort or missing legal machine
            # pool.  Stop rather than advancing through calendar boundaries
            # forever.  This is not a physical-infeasibility claim.
            structurally_empty = []
            for row in blocked:
                crew_count = sum(int(item.get("candidate_crews", 0))
                                 for item in row.get("matching", ()))
                key = str(row["operation_key"])
                op_row = operations[key]
                k = int(op_row.get("min_machine_count", 1))
                per_line = Counter(
                    int(machines_by_id[mid]["line_id"])
                    for mid in map(int, op_row.get("eligible_machine_ids", ()))
                    if mid in machines_by_id and bool(machines_by_id[mid].get("is_usable")))
                machine_pool_exists = any(
                    line in set(map(int, op_row.get("eligible_line_ids", ()))) and count >= k
                    for line, count in per_line.items())
                if crew_count == 0 or not machine_pool_exists:
                    structurally_empty.append({
                        "operation_key": key, "candidate_crews": crew_count,
                        "legal_machine_pool_exists": machine_pool_exists,
                    })
            if len(structurally_empty) == len(blocked):
                failure = {
                    "classification": "RESOURCE_DOMAIN_TRUNCATION",
                    "reason": "ready operations have no constructible fixed crew or legal machine pool",
                    "current_time": current, "operations": structurally_empty,
                }
                break
        next_time = _next_event(
            current, unscheduled=unscheduled, operations=operations,
            order_release=order_release, completions=completions,
            worker_reservations=worker_reservations,
            machine_reservations=machine_reservations, origin=origin,
        )
        if next_time is None or int(next_time) <= current:
            failure = {
                "classification": "EVENT_ADVANCE_ERROR", "reason": "no strictly later event",
                "current_time": current, "ready_operations": ready, "blocked": blocked,
            }
            break
        current = int(next_time)

    schedule.sort(key=lambda row: (int(row["start_seconds"]), str(row["operation_key"])))
    runtime = time.perf_counter() - started
    return {
        "status": "COMPLETE" if not unscheduled else "INCOMPLETE",
        "policy": policy,
        "schedule": schedule,
        "runtime_seconds": runtime,
        "events_processed": len(event_log),
        "event_log": event_log,
        "allocation_profile": dict(allocation_profile),
        "unscheduled_operation_keys": sorted(unscheduled),
        "failure": failure,
        "database_writes": 0,
    }


def run_edd_schedule(workload: NeutralFactoryWorkload) -> dict:
    """Run EDD through the exact same physical baseline engine as FIFO."""
    return run_fifo_schedule(workload, policy="EDD")


def run_spt_schedule(workload: NeutralFactoryWorkload) -> dict:
    """Run SPT through the exact same physical baseline engine as FIFO/EDD."""
    return run_fifo_schedule(workload, policy="SPT")


def _boundary_facts(payload):
    facts = []
    for row in payload["resource_commitments"]["machine"]:
        facts.append({
            "machine_id": int(row["machine_id"]), "start_seconds": int(row["start_seconds"]),
            "end_seconds": int(row["end_seconds"]),
            "production_order_id": str(row["production_order_id"]), "operation_key": "fixed",
        })
    for row in payload["resource_commitments"]["worker"]:
        facts.append({
            "worker_id": str(row["employee_id"]), "start_seconds": int(row["start_seconds"]),
            "end_seconds": int(row["end_seconds"]), "operation_key": str(row.get("operation_key", "fixed")),
        })
    return tuple(facts)


def _export_rows(workload, schedule_rows):
    origin = workload.problem.origin
    exported = []
    for row in schedule_rows:
        cfg = row["resource_configuration"]
        exported.append({
            "production_order_id": str(row["order_id"]),
            "operation_id": str(row["operation_key"]).split("_", 1)[1],
            "assigned_line_id": int(cfg["line_id"]), "machines_used": int(cfg["k"]),
            "machine_ids_used": list(cfg["machine_ids"]), "operators_used": len(cfg["worker_ids"]),
            "employee_ids_used": list(cfg["worker_ids"]),
            "worker_segments_by_employee": {
                str(worker): [{
                    "start": (origin + timedelta(seconds=int(a))).isoformat(),
                    "end": (origin + timedelta(seconds=int(b))).isoformat(),
                } for a, b in row["worker_segments"]] for worker in cfg["worker_ids"]
            },
            "proposed_start_time": (origin + timedelta(seconds=int(row["start_seconds"]))).isoformat(),
            "base_end_time": (origin + timedelta(seconds=int(row["end_seconds"]))).isoformat(),
            "cushioned_end_time": (origin + timedelta(seconds=int(row["end_seconds"]))).isoformat(),
        })
    return exported


def validate_fifo_schedule(workload: NeutralFactoryWorkload, schedule_rows: Sequence[Mapping]) -> dict:
    """Independent identity, replay, physical, and complete-scope gates."""
    payload = workload.payload
    problem = workload.problem
    keys = tuple(str(x) for x in payload["accounting"]["benchmark_operation_keys"])
    scope_contract = ValidationScopeV1("ASSEMBLED_FACTORY", keys)
    window = WindowPhenotypeV1(tuple(dict(row) for row in schedule_rows), _boundary_facts(payload), (), scope_contract)
    scope = scope_completeness(scope_contract, [row["operation_key"] for row in schedule_rows])

    fixed_worker_res = defaultdict(list)
    for commitment in problem.resources.fixed_worker_commitments:
        fixed_worker_res[str(commitment.employee_id)].append((
            problem.origin + timedelta(seconds=int(commitment.start_seconds)),
            problem.origin + timedelta(seconds=int(commitment.end_seconds)),
        ))
    replay = replay_assembled_phenotype({
        "problem": problem, "operator_index": workload.operator_index,
        "_worker_res": {key: tuple(values) for key, values in fixed_worker_res.items()},
    }, window) if scope["complete_for_scope"] else {
        "replay_status": "NOT_RUN", "replay_mismatch_count": 0, "replay_mismatches": [],
    }

    expected = []
    rows_by_key = {str(row["operation_key"]): row for row in schedule_rows}
    for operation in problem.operations:
        row = rows_by_key.get(operation.operation_key)
        k = int((row or {}).get("resource_configuration", {}).get("k", operation.min_machine_count))
        order = problem.order_by_id[operation.order_id]
        expected.append({
            "production_order_id": operation.order_id, "operation_id": operation.operation_id,
            "operation_sequence_number": operation.sequence,
            "release_time": problem.origin + timedelta(seconds=int(order.release_seconds)),
            "deadline": None if order.deadline_seconds is None else problem.origin + timedelta(seconds=int(order.deadline_seconds)),
            "eligible_line_ids": operation.eligible_line_ids,
            "required_machine_type": operation.required_capability,
            "required_operation_type": operation.required_capability,
            "required_machine_count": k, "required_workers": k,
            "setup_minutes": operation.setup_seconds / 60.0,
            "productive_minutes": operation.serial_productive_seconds / float(k) / 60.0,
        })
    machines = {
        int(row["machine_id"]): {
            "line_id": int(row["line_id"]), "factory_id": int(row["factory_id"]),
            "capabilities": tuple(row.get("capabilities", ())),
        } for row in payload["machines"] if bool(row.get("is_usable"))
    }
    workers = {
        str(row["worker_id"]): {"factory_id": int(row["factory_id"]), "skills": tuple(row.get("skills", ()))}
        for row in payload["workers"]
    }
    committed_machine = [{
        "resource_id": row["machine_id"],
        "start": problem.origin + timedelta(seconds=int(row["start_seconds"])),
        "end": problem.origin + timedelta(seconds=int(row["end_seconds"])),
        "operation_key": "fixed",
    } for row in payload["resource_commitments"]["machine"]]
    committed_worker = [{
        "resource_id": row["employee_id"],
        "start": problem.origin + timedelta(seconds=int(row["start_seconds"])),
        "end": problem.origin + timedelta(seconds=int(row["end_seconds"])),
        "operation_key": row.get("operation_key", "fixed"),
    } for row in payload["resource_commitments"]["worker"]]
    context = ScheduleValidationContext(
        expected, {str(row["order_id"]): int(row["factory_id"]) for row in payload["orders"]},
        {int(row["line_id"]): int(row["factory_id"]) for row in payload["lines"]},
        machines, workers, committed_machine, committed_worker,
        lambda worker, when: operator_available_at(workload.operator_index, _worker_token(worker), when), True,
    )
    physical_report = validate_exported_schedule(_export_rows(workload, schedule_rows), context).as_dict()

    custom_errors = []
    operation_payload = {str(row["operation_key"]): row for row in payload["operations"]}
    machine_timeline = defaultdict(list)
    for row in payload["resource_commitments"]["machine"]:
        machine_timeline[int(row["machine_id"])].append((
            int(row["start_seconds"]), int(row["end_seconds"]), str(row["production_order_id"]), "fixed"))
    for row in schedule_rows:
        key = str(row["operation_key"])
        op = operation_payload[key]
        cfg = row["resource_configuration"]
        k = int(cfg["k"])
        if not int(op["min_machine_count"]) <= k <= int(op["max_machine_count"]):
            custom_errors.append({"code": "machine_count_out_of_range", "operation_key": key})
        if len(cfg["machine_ids"]) != k or len(cfg["worker_ids"]) != k:
            custom_errors.append({"code": "resource_count_mismatch", "operation_key": key})
        if not set(map(int, cfg["machine_ids"])).issubset(set(map(int, op["eligible_machine_ids"]))):
            custom_errors.append({"code": "ineligible_machine", "operation_key": key})
        if not set(map(str, cfg["worker_ids"])).issubset(set(map(str, op["eligible_worker_ids"]))):
            custom_errors.append({"code": "ineligible_worker", "operation_key": key})
        for machine in cfg["machine_ids"]:
            machine_timeline[int(machine)].append((
                int(row["start_seconds"]), int(row["end_seconds"]), str(row["order_id"]), key))
    for machine, values in machine_timeline.items():
        values.sort(key=lambda item: (item[0], item[1], item[3]))
        for left, right in zip(values, values[1:]):
            gap = authoritative_changeover_seconds(left[2], right[2])
            if right[0] < left[1] + int(gap):
                custom_errors.append({
                    "code": "machine_changeover_violation", "machine_id": machine,
                    "predecessor": left[3], "successor": right[3],
                })
    errors = list(physical_report["errors"]) + custom_errors
    physical_status = "VALID" if not errors else "INVALID"
    accepted = bool(scope["complete_for_scope"] and replay["replay_status"] == "PASS" and physical_status == "VALID")
    counts = Counter(error["code"] for error in errors)
    return {
        "scope_status": "SCOPE_VALID" if scope["complete_for_scope"] else "SCOPE_INVALID",
        "replay_status": replay["replay_status"], "physical_status": physical_status,
        "accepted": accepted, "scope": scope, "replay": replay,
        "physical": physical_report, "custom_errors": custom_errors,
        "violation_counts": dict(counts), "database_writes": 0,
    }
