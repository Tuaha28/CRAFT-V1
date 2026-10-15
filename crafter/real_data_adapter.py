"""Read-only adapter from a frozen development snapshot to Crafter contracts.

This module changes only the process-local database selection environment.
It performs SELECTs through existing repository readers and has no write API.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from agent.decision_engine import AIDecisionEngine
from database.ga_snapshot import activate_snapshot, get_snapshot_origin, list_snapshots
from database.db import get_engine
from database.queries import (
    RESERVATION_FIXED,
    fetch_active_orders_and_routings,
    fetch_assignable_operators,
    fetch_concurrent_operator_capacity,
    fetch_line_factory_map,
    fetch_machine_reservation_records,
    fetch_machine_state,
    fetch_operator_reservation_records,
)
from ga.fitness import CHANGEOVER_MINUTES
from ga.scheduler import GAScheduler, make_key
from utils.factory_ownership import resolve_ownership
from utils.machine_taxonomy import machine_type_to_operation_type

from .contracts import (
    FixedMachineCommitment, FixedWorkerCommitment, LabourCapacitySpec,
    LineSpec, MachineSpec, OperationSpec, OrderSpec, ResourceState,
    SchedulingProblem,
)


SNAPSHOT_NAME = "parity_fix_20260916_s42"
SOURCE_MANIFEST = Path(
    "outputs/scheduler_parity_fix_preflight_20260916_s42/"
    "scheduler_snapshot_manifest.json"
)


@dataclass(frozen=True)
class RealWindowFixture:
    problem: SchedulingProblem
    operator_index: dict
    selected_order_ids: tuple[str, ...]
    source_metadata: dict
    source_fingerprint: str
    scheduler: object
    initial_machine_free: dict
    initial_machine_last_order: dict
    initial_operator_reservations: dict
    df_routings: object
    df_machines: object
    df_lines: object


def _seconds(origin, value, *, floor_zero=False):
    if value is None or pd.isna(value):
        return None
    seconds = int(math.ceil((pd.Timestamp(value).to_pydatetime() - origin).total_seconds()))
    return max(0, seconds) if floor_zero else seconds


def _canonical_frame(frame, sort_columns):
    if frame is None or frame.empty:
        return []
    ordered = frame.sort_values(sort_columns, kind="mergesort").copy()
    rows = []
    for record in ordered.to_dict(orient="records"):
        clean = {}
        for key, value in sorted(record.items()):
            if value is None or (isinstance(value, float) and pd.isna(value)):
                clean[key] = None
            elif hasattr(value, "isoformat"):
                clean[key] = value.isoformat()
            elif hasattr(value, "total_seconds"):
                clean[key] = value.total_seconds()
            elif hasattr(value, "item"):
                clean[key] = value.item()
            else:
                clean[key] = value
        rows.append(clean)
    return rows


def _fingerprint(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_real_window_fixture(order_limit: int = 50,
                              snapshot_name: str = SNAPSHOT_NAME) -> RealWindowFixture:
    if order_limit <= 0:
        raise ValueError("order_limit must be positive")
    available = {row["snapshot_name"]: row for row in list_snapshots()}
    if snapshot_name not in available:
        raise RuntimeError(f"frozen snapshot {snapshot_name!r} is not available")
    activate_snapshot(snapshot_name)
    origin = get_snapshot_origin(snapshot_name)

    routings = fetch_active_orders_and_routings()
    machines = fetch_machine_state()
    lines = pd.read_sql(
        "SELECT pl.line_id, pl.department_id, pl.line_number, "
        "pl.target_efficiency_percentage, pl.is_active, d.factory_id "
        "FROM Production_Lines pl JOIN Departments d "
        "ON d.department_id=pl.department_id WHERE pl.is_active=1",
        get_engine(),
    )
    workers = fetch_assignable_operators()
    line_factory = fetch_line_factory_map()
    operator_index = AIDecisionEngine._build_operator_index(
        workers, lines, line_factory)
    ownership = resolve_ownership(routings["production_order_id"])

    release_table = (
        routings.groupby("production_order_id", as_index=False)["release_date"].min()
        .sort_values(["release_date", "production_order_id"], kind="mergesort")
    )
    selected_native = tuple(
        release_table["production_order_id"].head(order_limit).tolist())
    selected = tuple(str(value) for value in selected_native)
    selected_set = set(selected_native)
    selected_routings = routings[
        routings["production_order_id"].isin(selected_set)
    ].copy()
    if selected_routings["production_order_id"].nunique() != order_limit:
        raise RuntimeError("earliest-release selection did not produce requested order count")

    keys = tuple(
        make_key(row.production_order_id, row.operation_id)
        for row in selected_routings.itertuples(index=False)
    )
    neutral_predictions = {
        key: {"delay": 0.0, "priority": 0.5} for key in keys
    }
    capacity = fetch_concurrent_operator_capacity()

    machine_records = [
        row for row in fetch_machine_reservation_records(origin)
        if row.classification == RESERVATION_FIXED
    ]
    worker_records = [
        row for row in fetch_operator_reservation_records(origin)
        if row.classification == RESERVATION_FIXED
    ]
    initial_machine_free = {}
    initial_machine_last_order = {}
    latest_machine_end = {}
    for row in machine_records:
        if row.end > latest_machine_end.get(row.asset_id, origin):
            latest_machine_end[row.asset_id] = row.end
            initial_machine_free[row.asset_id] = max(
                0.0, (row.end - origin).total_seconds() / 60.0)
            initial_machine_last_order[row.asset_id] = row.production_order_id
    initial_operator_reservations = {}
    for row in worker_records:
        initial_operator_reservations.setdefault(str(row.employee_id), []).extend(row.segments)

    scheduler = GAScheduler(
        selected_routings, machines, lines, neutral_predictions,
        current_time=origin,
        present_operator_count=capacity,
        initial_machine_free=initial_machine_free,
        initial_machine_last_order=initial_machine_last_order,
        scored_order_ids=set(selected),
        ownership_snapshot=ownership,
        operator_index=operator_index,
        initial_operator_reservations=initial_operator_reservations,
    )

    represented_factories = {
        int(scheduler.factory.order_factory[order_id]) for order_id in selected
    }
    line_specs = tuple(
        LineSpec(
            line_id=int(row.line_id), factory_id=int(row.factory_id),
            name=str(row.line_number),
        )
        for row in lines.itertuples(index=False)
        if int(row.factory_id) in represented_factories
    )

    usable_ids = set(scheduler.factory.usable_asset_ids)
    grouped_machines = machines[
        machines["factory_id"].isin(represented_factories)
    ].groupby("asset_id", sort=True)
    machine_specs = []
    for asset_id, group in grouped_machines:
        first = group.iloc[0]
        capabilities = tuple(sorted({
            str(value).strip().lower()
            for value in group["operation_type"].dropna().tolist()
        }))
        machine_specs.append(MachineSpec(
            machine_id=int(asset_id), factory_id=int(first["factory_id"]),
            line_id=int(first["line_id"]), capabilities=capabilities,
            is_usable=asset_id in usable_ids,
        ))

    order_specs = []
    for native_id in selected_native:
        rows = selected_routings[
            selected_routings["production_order_id"] == native_id]
        first = rows.iloc[0]
        order_id = str(native_id)
        order_specs.append(OrderSpec(
            order_id=order_id,
            factory_id=int(scheduler.factory.order_factory[order_id]),
            release_seconds=_seconds(origin, first["release_date"], floor_zero=True) or 0,
            deadline_seconds=_seconds(origin, first["order_deadline"]),
            # No explicit manager-priority field is present in this snapshot.
            priority_weight=1,
        ))

    operation_specs = []
    for row in selected_routings.itertuples(index=False):
        key = make_key(row.production_order_id, row.operation_id)
        details = scheduler.op_details[key]
        eligible_lines = tuple(sorted(
            int(line_id) for line_id in scheduler.factory.eligibility_map[key]))
        max_count = max(
            scheduler.factory._max_machines_for(key, line_id)
            for line_id in eligible_lines
        )
        operation_specs.append(OperationSpec(
            operation_key=key,
            order_id=str(row.production_order_id),
            operation_id=str(row.operation_id),
            sequence=int(row.operation_sequence_number),
            required_capability=str(details["required_capability"]).strip().lower(),
            eligible_line_ids=eligible_lines,
            serial_productive_seconds=max(
                1, int(math.ceil(float(details["serial_processing_mins"]) * 60.0))),
            setup_seconds=max(
                0, int(math.ceil(float(details["setup_mins"]) * 60.0))),
            min_machine_count=1,
            max_machine_count=int(max_count),
        ))

    skills_needed = {row.required_capability for row in operation_specs}
    labour_specs = []
    for factory_id in sorted(represented_factories):
        for skill in sorted(skills_needed):
            employees = {
                str(employee) for employee in
                operator_index["by_factory"].get((factory_id, skill), ())
            }
            if employees:
                labour_specs.append(LabourCapacitySpec(
                    factory_id=factory_id, skill=skill, capacity=len(employees)))

    routing_skill = {
        (str(row.production_order_id), str(row.operation_id)):
            machine_type_to_operation_type(row.required_machine_type)
        for row in routings.itertuples(index=False)
    }
    machine_commitments = tuple(
        FixedMachineCommitment(
            machine_id=int(row.asset_id),
            start_seconds=max(0, _seconds(origin, row.start) or 0),
            end_seconds=max(1, _seconds(origin, row.end) or 1),
            production_order_id=str(row.production_order_id),
        )
        for row in machine_records
        if int(row.asset_id) in {machine.machine_id for machine in machine_specs}
    )

    worker_commitments = []
    employee_factory = {
        str(row.employee_id): int(row.factory_id)
        for row in workers.dropna(subset=["factory_id"]).itertuples(index=False)
    }
    for row in worker_records:
        skill = routing_skill.get((str(row.production_order_id), str(row.operation_id)))
        if not skill:
            raise RuntimeError(
                f"cannot map fixed worker commitment {row.schedule_item_id} to a skill")
        for start, end in row.segments:
            worker_commitments.append(FixedWorkerCommitment(
                employee_id=str(row.employee_id),
                factory_id=employee_factory[str(row.employee_id)],
                skill=str(skill),
                start_seconds=max(0, _seconds(origin, start) or 0),
                end_seconds=max(1, _seconds(origin, end) or 1),
                operation_key=f"fixed:{row.production_order_id}_{row.operation_id}",
            ))

    max_release = max(order.release_seconds for order in order_specs)
    max_deadline = max(
        (order.deadline_seconds or 0) for order in order_specs)
    sequential_upper = sum(
        operation.setup_seconds + operation.serial_productive_seconds
        + int(round(CHANGEOVER_MINUTES * 60.0))
        for operation in operation_specs
    )
    fixed_upper = max(
        (row.end_seconds for row in machine_commitments), default=0)
    horizon = max(max_deadline, fixed_upper, max_release) + sequential_upper + 86_400

    problem = SchedulingProblem(
        problem_id=f"crafter-{snapshot_name}-first-{order_limit}-orders",
        origin=origin,
        horizon_seconds=int(horizon),
        lines=line_specs,
        machines=tuple(machine_specs),
        orders=tuple(order_specs),
        operations=tuple(operation_specs),
        labour_capacities=tuple(labour_specs),
        resources=ResourceState(
            fixed_machine_commitments=machine_commitments,
            fixed_worker_commitments=tuple(worker_commitments),
        ),
        changeover_seconds=int(round(CHANGEOVER_MINUTES * 60.0)),
    )

    manifest_bytes = SOURCE_MANIFEST.read_bytes()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    source_payload = {
        "snapshot": available[snapshot_name],
        "source_manifest_sha256": manifest_sha,
        "selected_order_ids": selected,
        "routings": _canonical_frame(
            selected_routings,
            ["production_order_id", "operation_sequence_number", "operation_id"],
        ),
        "machines": _canonical_frame(
            machines[machines["factory_id"].isin(represented_factories)],
            ["asset_id", "operation_type"],
        ),
        "workers": _canonical_frame(
            workers[workers["factory_id"].isin(represented_factories)],
            ["employee_id", "operation_type"],
        ),
        "problem": asdict(problem),
    }
    source_fingerprint = _fingerprint(source_payload)
    source_metadata = {
        "snapshot_name": snapshot_name,
        "snapshot_database": available[snapshot_name]["snapshot_db"],
        "source_database": available[snapshot_name]["source_db"],
        "origin": origin.isoformat(),
        "source_manifest": str(SOURCE_MANIFEST),
        "source_manifest_sha256": manifest_sha,
        "selection_rule": "first 50 orders by (minimum release_date, production_order_id)",
        "neutral_signals": "delay=0.0, priority=0.5; no GNN inference",
        "manager_priority": "no explicit manager-priority field; weight=1",
        "reservation_policy": "fixed/in-flight retained; movable future released",
        "calendar_semantics": (
            "validated repository rotating 06:00-14:00, 14:00-22:00, "
            "22:00-06:00 crews with staggered rest days; not replaced by 07:00-16:00"
        ),
        "calendar_realism_debt": True,
        "assignable_worker_rows": len(workers),
        "assignable_workers": int(workers["employee_id"].nunique()),
        "concurrent_operator_capacity": int(capacity),
        "standby_reserved_assets": len(scheduler.factory.reserved_asset_ids),
        "usable_assets": len(scheduler.factory.usable_asset_ids),
        "fixed_machine_source_records": len(machine_records),
        "fixed_worker_source_records": len(worker_records),
        "fixed_worker_segments": len(worker_commitments),
    }
    return RealWindowFixture(
        problem=problem,
        operator_index=operator_index,
        selected_order_ids=selected,
        source_metadata=source_metadata,
        source_fingerprint=source_fingerprint,
        scheduler=scheduler,
        initial_machine_free=initial_machine_free,
        initial_machine_last_order=initial_machine_last_order,
        initial_operator_reservations=initial_operator_reservations,
        df_routings=selected_routings,
        df_machines=machines,
        df_lines=lines,
    )
