"""Bounded, source-pinned TEST scheduling for Factory 4 or Factory 6.

This runner uses the canonical CRAFT CP-SAT, calendar, replay, and independent
validation path. It never opens a database. All missing execution rules are
explicit TEST assumptions; this is not a production admission adapter.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import psutil
import pymysql
from sqlalchemy import event

from database.db import engine as _application_engine

# This audit is capture-only. Fail closed if any imported scheduler path tries
# to open the legacy database while preparing a TEST run.
pymysql.connect = lambda *_args, **_kwargs: (_ for _ in ()).throw(
    RuntimeError("LEGACY_CONNECTION_FORBIDDEN"))


def _forbid_sqlalchemy_connection(*_args, **_kwargs):
    raise RuntimeError("DATABASE_CONNECTION_FORBIDDEN_CAPTURE_ONLY_RUNNER")


event.listen(_application_engine, "do_connect", _forbid_sqlalchemy_connection)

from crafter.contracts import (LabourCapacitySpec, LineSpec, MachineSpec,
                               OperationSpec, OrderSpec, SchedulingProblem)
from crafter.execution_contract import OperationExecutionContract, PARALLEL_SPLIT, TEST_ASSUMPTION
from crafter.production.checkpoints import load_checkpoint, save_checkpoint
from crafter.repair.calendar_regimes import CalendarRegimeCache
from crafter.repair.resource_domain_v2 import (
    ResourceDomainPolicyV2, diagnose_resource_domain_pressure,
    run_adaptive_planning_schedule,
)
from ga.worker_allocation import operator_available_at
from utils.machine_taxonomy import machine_type_to_operation_type
from web_v1.backend.integrations.craft.snapshot_adapter import normalize_snapshot


# The pinned v1 capture unless a DATA v2 capture is selected explicitly; the
# raw and normalized hashes are verified against its readiness files either way.
CAPTURE = ROOT / os.environ.get(
    "CRAFT_CAPTURE_DIR", "web_v1/audits/factory_source_integration_v1/capture_20261012T223420800478Z")
# Residual continuation: admit the unstarted route suffix of an order whose
# earlier operations are completed or in progress, released at that boundary.
RESIDUAL_ROUTES = os.environ.get("CRAFT_RESIDUAL_ROUTES", "0") == "1"
# Worker-machine pairing: own batch line only (default), or that first with a
# same-factory fallback for operations whose capable machines have no skilled
# crew on their line.
WORKER_LINE_POLICY = os.environ.get("CRAFT_WORKER_LINE_POLICY", "BATCH_LINE_ONLY")
if WORKER_LINE_POLICY not in ("BATCH_LINE_ONLY", "BATCH_LINE_THEN_FACTORY"):
    raise ValueError("CRAFT_WORKER_LINE_POLICY must be BATCH_LINE_ONLY or BATCH_LINE_THEN_FACTORY")
# CP-SAT stage deadlines (seconds) and per-level objective limit. Defaults are
# the validated laptop values; final experiments may raise them.
STAGE_SECONDS = tuple(float(x) for x in os.environ.get("CRAFT_STAGE_SECONDS", "3,5,8").split(","))
OBJECTIVE_SECONDS = float(os.environ.get("CRAFT_OBJECTIVE_SECONDS", "5"))
if len(STAGE_SECONDS) != 3 or min(STAGE_SECONDS) <= 0 or OBJECTIVE_SECONDS <= 0:
    raise ValueError("CRAFT_STAGE_SECONDS needs three positive values; CRAFT_OBJECTIVE_SECONDS > 0")
HERE = Path(__file__).resolve().parent
SETUP_SECONDS = 900
WINDOW_SIZE = 25
DOMAIN_INITIAL_CAP = 4
# The original cohort runs stopped at cap 5 after a single retry even though
# the saved domain audits showed materially more legal source-skilled workers
# and active machines.  Allow a finite, still conservative expansion to cap 8
# (4 -> 5 -> 6 -> 7 -> 8) for the same pinned cohorts; this changes only how
# many already-eligible TEST alternatives are offered, not execution rules.
DOMAIN_EXPANSION_ROUNDS = 4
# CRAFT V2 improvement moves: resource options per operation (the incumbent's
# own plus diverse alternatives) and how much later than the incumbent an
# operation may start (earlier is always free) -- a bounded neighbourhood.
MOVE_CONFIGS = int(os.environ.get("CRAFT_MOVE_CONFIGS", "6"))
MOVE_WINDOW_DAYS = float(os.environ.get("CRAFT_MOVE_WINDOW_DAYS", "3"))


def _split_failed_window(keys, halving=False):
    """Finite routing-order-preserving recovery.

    Legacy: <=5 -> <=2 -> 1.  Packed windows halve instead (15 -> 8+7 -> ...),
    so a large window does not collapse straight into many pairs.
    """
    if len(keys) <= 1:
        return []
    if halving:
        half = (len(keys) + 1) // 2
        return [tuple(keys[:half]), tuple(keys[half:])]
    size = 2 if len(keys) > 2 else 1
    return [tuple(keys[index:index + size]) for index in range(0, len(keys), size)]


def _pack_route_windows(routes, size):
    """Pack precedence-ordered route chunks into windows of at most ``size``.

    A route longer than ``size`` is split into consecutive chunks, each placed
    in a later window than its predecessor chunk, so routing order holds.
    """
    windows, current = [], []
    for route in routes:
        keys = [row["operation_key"] for row in route]
        for index in range(0, len(keys), size):
            chunk = keys[index:index + size]
            if current and len(current) + len(chunk) > size:
                windows.append(tuple(current))
                current = []
            current.extend(chunk)
    if current:
        windows.append(tuple(current))
    return windows


def _carry_rows(state, rows, completed, machines, workers, boundaries):
    """Reconstruct reservations identically for fresh and restored assignments."""
    for row in rows:
        key = str(row['operation_key'])
        if key in completed:
            raise ValueError('CHECKPOINT_DUPLICATE_ASSIGNMENT')
        completed[key] = row
        start, end = int(row['start_seconds']), int(row['end_seconds'])
        for machine in row['resource_configuration']['machine_ids']:
            machine = int(machine)
            machines.setdefault(machine, []).append((start, end, key.split('_')[0], key))
            boundaries.append({'machine_id': machine, 'start_seconds': start,
                               'end_seconds': end, 'operation_key': key})
        for worker in row['resource_configuration']['worker_ids']:
            for left, right in row['worker_segments']:
                if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                    left_seconds, right_seconds = int(left), int(right)
                    left_time = state['problem'].origin + timedelta(seconds=left_seconds)
                    right_time = state['problem'].origin + timedelta(seconds=right_seconds)
                else:
                    left_time, right_time = left, right
                    left_seconds = int((left_time - state['problem'].origin).total_seconds())
                    right_seconds = int((right_time - state['problem'].origin).total_seconds())
                workers.setdefault(str(worker), []).append((left_time, right_time))
                boundaries.append({'worker_id': str(worker), 'start_seconds': left_seconds,
                                   'end_seconds': right_seconds, 'operation_key': key})


def _verify_resume_accounting(payload, all_keys):
    accepted = [row['operation_key'] for row in payload['selected_schedule']]
    pending = [key for window in payload['pending_windows'] for key in window]
    deferred = list(payload['deferred_operations'])
    combined = accepted + pending + deferred
    if len(combined) != len(set(combined)) or set(combined) != set(all_keys):
        raise ValueError('CHECKPOINT_OPERATION_ACCOUNTING')
    if any(not 1 <= len(window) <= WINDOW_SIZE for window in payload['pending_windows']):
        raise ValueError('CHECKPOINT_WINDOW_LIMIT')
    recorded = [key for window in payload['windows'] if window['accepted']
                for key in window['operation_keys']]
    if sorted(recorded) != sorted(accepted):
        raise ValueError('CHECKPOINT_ACCEPTED_ACCOUNTING')


def _date_seconds(origin, value):
    stamp = datetime.fromisoformat(str(value)[:10])
    return max(0, int((stamp - origin).total_seconds()))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                 default=str).encode("utf-8")).hexdigest()


def _source(factory_id):
    source_path = CAPTURE / f"factory{factory_id}_source.json"
    readiness_path = CAPTURE / f"factory{factory_id}_readiness.json"
    raw = source_path.read_bytes()
    source = json.loads(raw)
    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    source_hash = hashlib.sha256(raw).hexdigest()
    snapshot = normalize_snapshot(source)
    if source_hash != readiness.get("raw_file_sha256"):
        raise ValueError("PINNED_RAW_SOURCE_HASH_MISMATCH")
    if snapshot["snapshot_hash"] != readiness.get("normalized_snapshot_hash"):
        raise ValueError("PINNED_NORMALIZED_SNAPSHOT_HASH_MISMATCH")
    if int(source["factory_id"]) != int(factory_id):
        raise ValueError("FACTORY_IDENTITY_MISMATCH")
    return source, readiness, snapshot, source_hash


def _source_for(factory_id, lane_policy=None):
    """The pinned source, optionally split into LANE_SPLIT_V1 bundle lanes.

    The raw and normalized hashes are verified on the capture before any
    transformation; the lane policy then enters the scenario assumptions.
    """
    source, readiness, snapshot, source_hash = _source(factory_id)
    if lane_policy is None:
        return source, readiness, snapshot, source_hash
    from crafter.production.lanes import split_lanes
    eligible = [order_id for _release, order_id, _rows, _order in _eligible_order_routes(source, snapshot)]
    source, snapshot, _lane_map = split_lanes(source, snapshot, eligible, lane_policy)
    return source, readiness, snapshot, source_hash


def _boundary_time(operation):
    """When an executed operation's work is done: observed or projected lot ends."""
    ends = []
    for lot in operation.get("lots", ()):
        end = lot.get("observed_completion") if lot.get("state") == "COMPLETED" else lot.get("projected_end")
        if not end:
            return None
        ends.append(str(end))
    return max(ends) if ends else None


def _residual_route(rows):
    """(boundary_iso, unstarted suffix) after a completed/in-progress prefix, or None."""
    started = [i for i, row in enumerate(rows) if row["execution_state"] != "UNSTARTED"]
    last = started[-1]
    prefix, suffix = rows[:last + 1], rows[last + 1:]
    if (not suffix or any(row["execution_state"] not in ("FULLY_COMPLETED", "PARTIALLY_EXECUTED")
                          for row in prefix)):
        return None
    boundary = _boundary_time(rows[last])
    return (boundary, suffix) if boundary else None


def _eligible_order_routes(source, snapshot):
    """Whole UNSTARTED orders; with RESIDUAL_ROUTES also unstarted route suffixes."""
    groups = defaultdict(list)
    for operation in snapshot["operations"]:
        groups[str(operation["order_id"])].append(operation)
    wip_operations = {str(row["production_order_id"]) + "_" + str(row["operation_id"])
                      for row in source.get("wip", ()) if int(row.get("pieces_completed", 0)) > 0}
    routes = []
    for order_id, rows in groups.items():
        rows.sort(key=lambda row: (int(row["routing_sequence"]), str(row["operation_key"])))
        if not rows:
            continue
        order = next(item for item in source["orders"]
                     if str(item["production_order_id"]) == order_id)
        if any(row["execution_state"] != "UNSTARTED" for row in rows):
            residual = _residual_route(rows) if RESIDUAL_ROUTES else None
            if residual is None:
                continue
            boundary, rows = residual
            order = {**order, "residual_boundary": boundary}
        if any(row["operation_key"] in wip_operations for row in rows):
            continue
        routes.append((str(order.get("scheduled_start_date") or "9999-12-31"), order_id,
                       rows, order))
    return sorted(routes, key=lambda row: (row[0], row[1]))


def _select_routes(routes, target):
    if target == "all":
        return [(rows, order) for _release, _oid, rows, order in routes]
    try:
        count = int(target)
    except (TypeError, ValueError) as exc:
        raise ValueError("TARGET_MUST_BE_POSITIVE_INTEGER_OR_ALL") from exc
    if count <= 0:
        raise ValueError("TARGET_MUST_BE_POSITIVE_INTEGER_OR_ALL")
    # Breadth-first route prefixes exercise multiple real orders before deep
    # serial chains dominate the first gate. Prefix closure is preserved for
    # every order; no operation is skipped to reach a numeric target.
    selected_by_order = [[] for _ in routes]
    remaining = count
    depth = 0
    while remaining:
        progressed = False
        for index, (_release, _order_id, rows, _order) in enumerate(routes):
            if depth < len(rows) and remaining:
                selected_by_order[index].append(rows[depth])
                remaining -= 1
                progressed = True
        if not progressed:
            raise ValueError(f"INSUFFICIENT_SOURCE_ADMISSIBLE_OPERATIONS:{remaining}")
        depth += 1
    return [(selected_by_order[index], route[3])
            for index, route in enumerate(routes) if selected_by_order[index]]


def _prepare(factory_id, source, readiness, snapshot, source_hash, selected_routes,
             *, complete_captured_test_domain=False, lane_policy=None, crew_policy=None):
    origin = datetime.fromisoformat(source["reference_time"])
    assumptions = {
        "contract": "CRAFT_OPERATION_EXECUTION_V1",
        "factory_id": factory_id,
        "required_workers": 1,
        "required_machines": 1,
        "setup_seconds": SETUP_SECONDS,
        "machine_speedup_factor": "1",
        "worker_line_policy": WORKER_LINE_POLICY + "_TEST",
        "worker_calendar_policy": "CAPTURED_TEST_BATCH_ROTATION_AND_REST_DAY",
        "machine_calendar_policy": "CAPTURE_ACTIVE_STATUS_ONLY_TEST",
        "partial_execution_policy": "BLOCK_PARTIAL_OPERATIONS_AND_QUARANTINE_OPEN_HOLDS_TO_HORIZON",
        "precedence_policy": "WHOLE_OPERATION",
    }
    if lane_policy is not None:
        # Whole-operation precedence still holds within each lane.
        assumptions["lane_policy"] = dict(lane_policy)
    if crew_policy is not None:
        # Adaptive crew sizing: up to max_crew workers, each on their own
        # allowed machine, and never less than one bundle per worker.
        if (type(crew_policy.get("max_crew")) is not int or not 1 <= crew_policy["max_crew"] <= 8
                or type(crew_policy.get("bundle_pieces")) is not int or crew_policy["bundle_pieces"] < 1):
            raise ValueError("CREW_POLICY_INVALID")
        assumptions["crew_policy"] = {**crew_policy, "efficiency_rule": PARALLEL_SPLIT}
    if RESIDUAL_ROUTES:
        assumptions["partial_execution_policy"] = (
            "RESIDUAL_SUFFIX_RELEASED_AT_PROJECTED_BOUNDARY; PARTIAL_LOTS_HOLD_TO_PROJECTED_END")
    if (STAGE_SECONDS, OBJECTIVE_SECONDS) != ((3.0, 5.0, 8.0), 5.0):
        assumptions["solver_limits"] = {"stage_seconds": list(STAGE_SECONDS),
                                        "objective_seconds": OBJECTIVE_SECONDS}
    scenario_hash = _hash(assumptions)

    active_lines = {int(row["line_id"]): row for row in source["resources"]["lines"]
                    if int(row["factory_id"]) == factory_id and row["is_active"]}
    workers = {int(row["employee_id"]): row for row in source["resources"]["workers"]
               if int(row["factory_id"]) == factory_id and row["is_active"]}
    batches = {int(row["batch_id"]): row for row in source["resources"]["batches"]
               if row["is_active"]}
    membership = {int(row["employee_id"]): int(row["batch_id"])
                  for row in source["resources"]["batch_members"]}
    leave = {int(row["employee_id"]) for row in source["resources"]["work_schedules"]
             if row["is_on_leave"]}
    machines = {int(row["asset_id"]): row for row in source["resources"]["machines"]
                if int(row["factory_id"]) == factory_id and row["current_status"] == "Active"
                and int(row["line_id"]) in active_lines}
    machine_caps = defaultdict(set)
    for row in source["resources"]["machine_capabilities"]:
        mid = int(row["asset_id"])
        if mid in machines:
            machine_caps[str(row["operation_type"]).strip().lower()].add(mid)
    skill_efficiency = defaultdict(dict)
    for row in source["resources"]["skills"]:
        wid = int(row["employee_id"])
        if wid in workers and wid in membership and wid not in leave:
            batch = batches.get(membership[wid])
            if batch:
                skill = str(row["operation_type"]).strip().lower()
                skill_efficiency[skill][wid] = Decimal(str(row["efficiency_rating"])) / Decimal(100)

    source_operations = {str(row["production_order_id"]) + "_" + str(row["operation_id"]): row
                         for row in source["operations"]}
    all_selected = [row for route, _order in selected_routes for row in route]
    keys = tuple(row["operation_key"] for row in all_selected)
    operation_specs, order_specs = [], {}
    resource_lines, resource_machines = set(), set()
    source_domain_operations, contracts, efficiency_index = {}, {}, {}
    by_factory, by_line, by_skill, crew_of, line_factory = defaultdict(list), defaultdict(list), defaultdict(list), {}, {}

    # Shared per-operation source alternatives remain bounded on entry and are
    # expanded only by CRAFT's generic adaptive controller.
    for route, order in selected_routes:
        order_id = str(order["production_order_id"])
        start = _date_seconds(origin, order.get("scheduled_start_date") or source["reference_time"])
        if order.get("residual_boundary"):
            # The suffix cannot start before its executed predecessor's work ends.
            boundary = datetime.fromisoformat(str(order["residual_boundary"])) - origin
            start = max(start, math.ceil(boundary.total_seconds()))
        deadline = _date_seconds(origin, order.get("delivery_deadline"))
        order_specs[order_id] = OrderSpec(order_id, factory_id, start, deadline, 1)
        for norm in route:
            key = str(norm["operation_key"])
            raw_op = source_operations[key]
            capability = machine_type_to_operation_type(raw_op["required_machine_type"]).strip().lower()
            quantity = int(norm["quantity"]["not_started"])
            if (quantity <= 0 or int(norm["quantity"]["completed"]) != 0
                    or int(norm["quantity"]["in_flight"]) != 0
                    or int(norm["quantity"]["target"]) != quantity):
                raise ValueError(f"NON_UNSTARTED_QUANTITY_LEDGER:{key}")
            eligible_machines = tuple(sorted(machine_caps.get(capability, ())))
            workers_for_skill = tuple(sorted(skill_efficiency.get(capability, {})))
            lines = tuple(sorted({int(machines[mid]["line_id"]) for mid in eligible_machines}))
            allowed_pairs = tuple(sorted(
                (str(wid), int(mid)) for wid in workers_for_skill for mid in eligible_machines
                if int(batches[membership[wid]]["line_id"]) == int(machines[mid]["line_id"])))
            if not allowed_pairs and WORKER_LINE_POLICY == "BATCH_LINE_THEN_FACTORY":
                # No skilled crew on any capable machine's line: fall back to
                # skilled same-factory batch workers, as the generator's own
                # plan does (batch member first, then same factory).
                allowed_pairs = tuple(sorted((str(wid), int(mid)) for wid in workers_for_skill
                                             for mid in eligible_machines))
            eligible_workers = tuple(sorted({int(wid) for wid, _mid in allowed_pairs}))
            if not eligible_machines or not eligible_workers or not allowed_pairs:
                raise ValueError(f"NO_CAPTURE_ELIGIBLE_LINE_RESOURCE_PAIR:{key}")
            crew_sizes = (1,)
            if crew_policy is not None:
                pair_lines = defaultdict(lambda: (set(), set()))
                for wid, mid in allowed_pairs:
                    pair_lines[int(machines[mid]["line_id"])][0].add(wid)
                    pair_lines[int(machines[mid]["line_id"])][1].add(mid)
                widest = max(min(len(w), len(m)) for w, m in pair_lines.values())
                top = min(crew_policy["max_crew"], quantity // crew_policy["bundle_pieces"], widest)
                crew_sizes = tuple(range(1, max(1, top) + 1))
            smv = Decimal(str(raw_op["standard_allowed_minutes"]))
            base_seconds = int((Decimal(quantity) * smv * Decimal(60))
                               .to_integral_value(rounding=ROUND_CEILING))
            operation_specs.append(OperationSpec(
                key, order_id, str(raw_op["operation_id"]), int(raw_op["operation_sequence_number"]),
                capability, lines, max(1, base_seconds), SETUP_SECONDS, 1, max(crew_sizes)))
            for mid in eligible_machines:
                resource_machines.add(mid)
                resource_lines.add(int(machines[mid]["line_id"]))
                line_factory[int(machines[mid]["line_id"])] = factory_id
            for wid in eligible_workers:
                batch = batches[membership[wid]]
                crew_of[wid] = (int(batch["rotation_offset"]), int(batch["rest_day_offset"]))
                efficiency_index[(wid, capability)] = float(skill_efficiency[capability][wid])
                by_factory[(factory_id, capability)].append(str(wid))
                by_line[(int(batch["line_id"]), capability)].append(str(wid))
                by_skill[capability].append(str(wid))
            contract = OperationExecutionContract(
                operation_key=key, factory_id=factory_id,
                required_worker_count=1, required_machine_count=1,
                required_skills=(capability,),
                eligible_worker_ids=tuple(str(wid) for wid in eligible_workers),
                eligible_machine_ids=eligible_machines, eligible_line_ids=lines,
                quantity=quantity, target_quantity=int(norm["quantity"]["target"]),
                completed_quantity=0, in_flight_quantity=0,
                quantity_basis="UNSTARTED_REMAINING_UNITS",
                smv_minutes_per_unit=str(smv), setup_seconds=SETUP_SECONDS,
                evidence_class=TEST_ASSUMPTION, source_fingerprint=source_hash,
                snapshot_fingerprint=str(readiness["normalized_snapshot_hash"]),
                scenario_fingerprint=scenario_hash,
                worker_efficiency_percent=tuple(sorted(
                    (str(wid), str(skill_efficiency[capability][wid] * Decimal(100)))
                    for wid in eligible_workers)),
                machine_speedup_factor="1", pairing_policy="EXPLICIT_PAIRS",
                worker_calendar_policy=assumptions["worker_calendar_policy"],
                machine_calendar_policy=assumptions["machine_calendar_policy"],
                allowed_worker_machine_pairs=allowed_pairs,
                allowed_crew_sizes=crew_sizes if len(crew_sizes) > 1 else (),
                crew_efficiency_policy=PARALLEL_SPLIT if len(crew_sizes) > 1 else "")
            contracts[key] = contract.as_mapping()
            source_domain_operations[key] = {
                "factory_id": factory_id, "crew_size": 1,
                "machine_ids": eligible_machines,
                "worker_ids": tuple(str(wid) for wid in eligible_workers),
                "worker_efficiency": {str(wid): str(skill_efficiency[capability][wid])
                                      for wid in eligible_workers},
                "base_productive_minutes": str(smv * quantity),
                "setup_seconds": SETUP_SECONDS,
                "execution_contract": contract.as_mapping(),
                # Completeness here is scoped to the pinned capture and the
                # explicit TEST eligibility rules above, never to unrecorded
                # plant policy or live availability.
                "machine_pool_complete": bool(complete_captured_test_domain),
                "worker_pool_complete": bool(complete_captured_test_domain),
            }

    # A time-bounded window must also cover future order releases. Keep the
    # horizon tied to the selected cycle, not the entire factory ledger.
    max_due = max(order.deadline_seconds or 0 for order in order_specs.values())
    max_release = max(order.release_seconds for order in order_specs.values())
    horizon = max(max_due, max_release) + 30 * 86400
    problem = SchedulingProblem(
        f"F{factory_id}-PINNED-CAPTURE-TEST", origin, int(horizon),
        tuple(LineSpec(line, factory_id, str(line)) for line in sorted(resource_lines)),
        tuple(MachineSpec(mid, factory_id, int(machines[mid]["line_id"]),
                          tuple(sorted(cap for cap, pool in machine_caps.items() if mid in pool)))
              for mid in sorted(resource_machines)),
        tuple(order_specs[key] for key in sorted(order_specs)), tuple(operation_specs),
        tuple(LabourCapacitySpec(factory_id, skill, len(set(ids)))
              for (fid, skill), ids in sorted(by_factory.items()) if fid == factory_id))

    # Partial operations are never re-planned. By default their in-flight lots
    # are quarantined to the TEST horizon (conservative). With RESIDUAL_ROUTES
    # each in-flight lot holds its machine and worker until its projected end,
    # and the operation's not-started lots hold theirs over their projected
    # interval -- the captured plan those lots will follow.
    machine_res, worker_res, boundary_facts = defaultdict(list), defaultdict(list), []
    partial_holds = []

    def hold(operation, lot_id, machine_id, worker_id, start, end, release):
        start, end = max(0, int(start)), min(int(horizon), int(end))
        if end <= start:
            return
        machine_res[machine_id].append((start, end, operation["order_id"], "TEST-OPEN-HOLD-" + str(lot_id)))
        worker_res[worker_id].append((origin + timedelta(seconds=start), origin + timedelta(seconds=end)))
        boundary_facts.append({"machine_id": machine_id, "start_seconds": start,
                               "end_seconds": end, "operation_key": operation["operation_key"]})
        boundary_facts.append({"worker_id": worker_id, "start_seconds": start,
                               "end_seconds": end, "operation_key": operation["operation_key"]})
        partial_holds.append({"operation_key": operation["operation_key"], "lot_id": lot_id,
                              "machine_id": machine_id, "worker_id": worker_id, "release": release})

    def offset(value):
        return math.ceil((datetime.fromisoformat(str(value)) - origin).total_seconds())

    for operation in snapshot["operations"]:
        if operation["execution_state"] != "PARTIALLY_EXECUTED":
            continue
        lots = {str(lot.get("lot_id")): lot for lot in operation.get("lots", ())}
        for item in operation.get("resource_holds", ()):
            lot = lots.get(str(item["lot_id"]), {})
            end = offset(lot["projected_end"]) if RESIDUAL_ROUTES and lot.get("projected_end") else horizon
            hold(operation, item["lot_id"], int(item["machine_id"]), str(item["worker_id"]), 0, end,
                 "PROJECTED_LOT_END_TEST" if end != horizon else "UNKNOWN_QUARANTINED_TO_TEST_HORIZON")
        if RESIDUAL_ROUTES:
            for lot in operation.get("lots", ()):
                if lot.get("state") == "NOT_STARTED" and lot.get("projected_start") and lot.get("projected_end"):
                    hold(operation, lot["lot_id"], int(lot["machine_id"]), str(lot["worker_id"]),
                         offset(lot["projected_start"]), offset(lot["projected_end"]),
                         "CAPTURED_PROJECTED_LOT_TEST")

    operator_index = {
        "by_factory": dict(by_factory), "by_line": dict(by_line), "by_skill": dict(by_skill),
        "line_factory": line_factory, "crew_of": crew_of, "shift_of": {},
        "efficiency_of": efficiency_index,
    }
    # Route edges are included only for adjacent selected operations. Every
    # selected order is an unstarted route prefix; no unresolved predecessor is
    # skipped at its boundary.
    route_predecessor, route_successor, route_rows = {}, {}, {}
    for route, _order in selected_routes:
        selected_route = [row["operation_key"] for row in route if row["operation_key"] in set(keys)]
        for index, key in enumerate(selected_route):
            route_predecessor[key] = selected_route[index - 1] if index else None
            route_successor[key] = selected_route[index + 1] if index + 1 < len(selected_route) else None
            route_rows[key] = {"start_minutes": 0}
    after_index = type("RouteIndex", (), {})()
    after_index.by_key = route_rows
    after_index.route_predecessor = route_predecessor
    after_index.route_successor = route_successor

    def worker_interval(worker_id, start, end):
        if end <= start:
            return False
        wid = int(worker_id) if str(worker_id).isdigit() else worker_id
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        probes = [start, end - timedelta(microseconds=1)]
        while day <= end:
            probes.extend(day + timedelta(hours=h) for h in (0, 6, 14, 22, 24)
                          if start <= day + timedelta(hours=h) < end)
            day += timedelta(days=1)
        return all(operator_available_at(operator_index, wid, point) for point in probes)

    def machine_interval(machine_id, start, end):
        machine = machines.get(int(machine_id))
        return bool(machine and machine["current_status"] == "Active" and end > start)

    state = {
        "problem": problem, "operator_index": operator_index,
        "validation_operator_index": operator_index, "after_index": after_index,
        "keys": tuple(keys), "baseline": type("Baseline", (), {"operations": ()})(),
        "event": None, "planning_context": {"mode": "INITIAL_PLAN"},
        "objective_profile": "INITIAL_PLAN_NO_STABILITY",
        "classified": {row["operation_key"]: {"state": "MOVABLE_WINDOW"} for row in all_selected},
        "_machine_res": dict(machine_res), "_worker_res": dict(worker_res),
        "boundary_facts": tuple(boundary_facts),
        "source_resource_domain": {
            "contract_version": "SOURCE_RESOURCE_DOMAIN_V1",
            "source_sha256": source_hash,
            "snapshot_hash": readiness["normalized_snapshot_hash"],
            "scenario_hash": scenario_hash,
            "operations": source_domain_operations,
        },
        "source_resource_provenance": {
            "source_sha256": source_hash,
            "snapshot_hash": readiness["normalized_snapshot_hash"],
            "scenario_hash": scenario_hash,
        },
        "operation_execution_contracts": contracts,
        "allow_test_execution_assumptions": True,
        "require_execution_contracts": True,
        "worker_interval_is_available": worker_interval,
        "machine_interval_is_available": machine_interval,
        "test_assumptions": assumptions,
        "test_partial_holds": partial_holds,
    }
    return state


def _validate_assembled_trial(state, candidate_rows, initial_boundary_facts):
    """Independently replay and check the whole accepted TEST prefix.

    This is deliberately run before a window is added to the carry-forward
    reservation state. It checks selected assignments together with every
    captured simulated fixed hold. Unscheduled outside demand has no fabricated
    reservation and remains an explicit closure limitation.
    """
    from crafter.repair.scoped_validation import (
        _planning_rows_from_canonical, replay_assembled_phenotype,
        validate_window_physical,
    )
    from crafter.repair.validation_scope import ValidationScopeV1
    from crafter.repair.window_phenotype import (
        WindowPhenotypeV1, validate_window_scope_accounting,
    )

    adapted = _planning_rows_from_canonical(
        {"acceptance": {"selected_schedule": candidate_rows}}, state)
    required = tuple(sorted(row["operation_key"] for row in adapted))
    # The cohort consists only of unstarted route prefixes. An accepted prefix
    # cannot claim a successor while its source predecessor is still pending.
    # Reconstructing the validation scope from supplied rows alone would miss
    # this omission, especially after checkpoint restoration.
    represented = set(required)
    missing_predecessors = {
        key: state['after_index'].route_predecessor[key] for key in required
        if state['after_index'].route_predecessor.get(key) is not None
        and state['after_index'].route_predecessor[key] not in represented}
    phenotype = WindowPhenotypeV1(
        tuple(adapted), tuple(dict(row) for row in initial_boundary_facts), (),
        ValidationScopeV1("WINDOW", required, ()))
    scope = validate_window_scope_accounting(phenotype)
    replay = (replay_assembled_phenotype(state, phenotype)
              if scope["scope_status"] == "SCOPE_VALID"
              else {"replay_status": "NOT_RUN", "replay_mismatch_count": 0})
    physical = (validate_window_physical(state["problem"],
                                         state["validation_operator_index"],
                                         phenotype, state=state)
                if replay["replay_status"] == "PASS"
                else {"physical_status": "NOT_RUN", "report": None,
                      "boundary_errors": []})
    return {
        "accepted": (not missing_predecessors and scope["scope_status"] == "SCOPE_VALID"
                     and replay["replay_status"] == "PASS"
                     and physical["physical_status"] == "VALID"),
        "scope_status": "SCOPE_INVALID" if missing_predecessors else scope["scope_status"],
        "missing_source_predecessors": missing_predecessors,
        "replay_status": replay["replay_status"],
        "physical_status": physical["physical_status"],
        "scope": scope,
        "replay": replay,
        "physical": physical,
        "global_resource_conflict_status": physical["physical_status"],
    }


def _run(factory_id, target, *, complete_captured_test_domain=False, checkpoint=None,
         route_ready_objective=False, resume=None, max_new_attempts=None,
         selected_keys=None, approved_rows=(), approval_fingerprint=None,
         window_size=None, attempt_time_budget_seconds=None, window_sequence=None,
         lane_policy=None, window_order=None, crew_policy=None,
         dispatch_guard=False, lookahead=0, lns_seconds=0, lns_neighbourhood=20,
         start_candidates=None, improve=None, full_domain_windows=False):
    # Reuse the already validated additive probes; no Factory 1 data is loaded.
    from web_v1.audits.factory1_realistic_neighborhood_v1.run_bounded import route_ready_probes
    started = time.perf_counter()
    memory = psutil.Process().memory_info().rss / 1024**2
    source, readiness, snapshot, source_hash = _source_for(factory_id, lane_policy)
    routes = _eligible_order_routes(source, snapshot)
    if type(factory_id) is not int or factory_id not in range(1, 7):
        raise ValueError('CAPTURE_FACTORY_NOT_SUPPORTED')
    from crafter.production.capture_boundaries import select_exact_routes
    if approved_rows and (selected_keys is None or not approval_fingerprint):
        raise ValueError('CAPTURE_APPROVAL_SNAPSHOT_REQUIRED')
    selected_routes = (_select_routes(routes, target) if selected_keys is None else
                       select_exact_routes(routes, selected_keys, approved_rows, factory_id))
    state = _prepare(factory_id, source, readiness, snapshot, source_hash, selected_routes,
                     complete_captured_test_domain=complete_captured_test_domain,
                     lane_policy=lane_policy, crew_policy=crew_policy)
    approved_keys = {row['operation_key'] for row in approved_rows}
    all_keys = tuple(row["operation_key"] for route, _order in selected_routes for row in route
                     if row['operation_key'] not in approved_keys)
    if len(all_keys) != len(set(all_keys)):
        raise ValueError("DUPLICATE_SELECTED_OPERATION_IDENTITY")

    # Keep each selected order's precedence-closed route prefix together and
    # schedule no more than 25 operations in a CP-SAT model. The ALL gate uses
    # whole eligible routes (each source route is below the model cap).
    ordered_routes = selected_routes
    if window_order == 'DUE_DATE':
        # Feed packed windows in delivery-deadline order (release, then id, as
        # tie-breaks) so an urgent order is not queued behind an earlier-
        # released relaxed one. Precedence is per route, so any order is legal.
        def urgency(item):
            order = item[1]
            return (str(order.get('delivery_deadline') or '9999-12-31'),
                    str(order.get('scheduled_start_date') or '9999-12-31'),
                    str(order['production_order_id']))
        ordered_routes = sorted(selected_routes, key=urgency)
    elif window_order is not None:
        raise ValueError('WINDOW_ORDER_INVALID')
    pending_routes = [[row for row in route if row['operation_key'] not in approved_keys]
                      for route, _order in ordered_routes]
    if window_sequence is not None:
        # Explicit, caller-ordered windows (dispatch-rule baselines). They must
        # partition exactly the pending selection; precedence is still gated
        # per window, so a window whose predecessor is unscheduled is deferred.
        windows = [tuple(window) for window in window_sequence]
        pending_keys = [row["operation_key"] for route in pending_routes for row in route]
        flat = [key for window in windows for key in window]
        if len(flat) != len(set(flat)) or set(flat) != set(pending_keys):
            raise ValueError("WINDOW_SEQUENCE_NOT_A_PARTITION_OF_SELECTION")
        recovery_policy = 'EXPLICIT_SEQUENCE_V1'
    elif window_size is None:
        windows = [tuple(row["operation_key"] for row in route[index:index + 5])
                   for route in pending_routes for index in range(0, len(route), 5)]
        recovery_policy = 'ORDERED_5_2_1_V1'
    else:
        if type(window_size) is not int or not 1 <= window_size <= WINDOW_SIZE:
            raise ValueError("WINDOW_SIZE_INVALID")
        windows = _pack_route_windows(pending_routes, window_size)
        recovery_policy = f'PACKED_{window_size}_HALVING_V1'
    if any(not 1 <= len(window) <= WINDOW_SIZE for window in windows):
        raise ValueError("ROUTE_WINDOW_EXCEEDS_BOUNDED_CP_CAP")

    commitments_m = {key: list(rows) for key, rows in state["_machine_res"].items()}
    commitments_w = {key: list(rows) for key, rows in state["_worker_res"].items()}
    boundary_facts = list(state["boundary_facts"])
    completed, window_rows = {}, []
    assembled_validation = None
    if approved_rows:
        approved_validation = _validate_assembled_trial(state, approved_rows, state['boundary_facts'])
        if not approved_validation['accepted']:
            raise ValueError('CAPTURE_APPROVED_PHYSICAL_VALIDATION_FAILED')
        _carry_rows(state, approved_rows, completed, commitments_m, commitments_w, boundary_facts)
    deferred_operations = {}
    cache = CalendarRegimeCache()
    identity = {'factory_id': factory_id, 'source_sha256': source_hash,
                'snapshot_hash': readiness['normalized_snapshot_hash'],
                'scenario_hash': state['source_resource_provenance']['scenario_hash'],
                'selected_keys': list(all_keys),
                'route_ready_objective': route_ready_objective,
                'complete_captured_test_domain': complete_captured_test_domain,
                'initial_cap': DOMAIN_INITIAL_CAP, 'expansion_rounds': DOMAIN_EXPANSION_ROUNDS,
                'recovery_policy': recovery_policy,
                'implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if window_order is not None:
        identity['window_order'] = window_order
    if full_domain_windows:
        # One stage over every sampled option per operation, so a dispatch
        # rule takes the earliest-finishing qualified worker/machine (as a
        # real dispatcher would) instead of the first-stage single anchor.
        identity['full_domain_windows'] = True
    if dispatch_guard or lookahead:
        identity.update(dispatch_guard=bool(dispatch_guard), lookahead=int(lookahead))
    if lns_seconds:
        identity.update(lns_seconds=float(lns_seconds), lns_neighbourhood=int(lns_neighbourhood))
    if type(lookahead) is not int or not 0 <= lookahead <= WINDOW_SIZE:
        raise ValueError("LOOKAHEAD_INVALID")
    deadlines = {str(o.order_id): o.deadline_seconds for o in state["problem"].orders}
    final_ops = {}
    for op in state["problem"].operations:
        if (str(op.order_id) not in final_ops
                or int(op.sequence) > int(state["problem"].operation_by_key[final_ops[str(op.order_id)]].sequence)):
            final_ops[str(op.order_id)] = op.operation_key

    def score(rows, keys):
        """CRAFT's own delivery goals on a window's rows (lower is better)."""
        by_key = {r["operation_key"]: r for r in rows}
        tardy = []
        for order_id, final in final_ops.items():
            if final in keys and final in by_key and deadlines.get(order_id) is not None:
                tardy.append(max(0, int(by_key[final]["end_seconds"]) - int(deadlines[order_id])))
        return (len(keys) - len(by_key), sum(t > 0 for t in tardy), sum(tardy),
                max(tardy, default=0), sum(int(r["end_seconds"]) for r in rows))
    pace = os.environ.get("CRAFT_OBJECTIVE_MODE", "ECONOMY").upper()
    jit_buffer = int(float(os.environ.get("CRAFT_JIT_BUFFER_DAYS", "2")) * 86400)
    all_keys_set = set(all_keys)

    def v2_score(rows_by_key):
        """Whole-schedule goals in CRAFT's own order (lower is better):
        unplaced operations, late orders, total and maximum tardiness, then
        the manager's pace goal (FAST: finish early; SLOW: finish close to
        due minus buffer; ECONOMY: fewest distinct workers)."""
        mine = {k: r for k, r in rows_by_key.items() if k in all_keys_set}
        tardy, early = [], 0
        for order_id, final in final_ops.items():
            if final in mine and deadlines.get(order_id) is not None:
                end, due = int(mine[final]["end_seconds"]), int(deadlines[order_id])
                tardy.append(max(0, end - due))
                early += max(0, due - jit_buffer - end)
        delivery = (len(all_keys_set) - len(mine), sum(t > 0 for t in tardy), sum(tardy),
                    max(tardy, default=0))
        workers = len({str(w) for r in mine.values() for w in r["resource_configuration"]["worker_ids"]})
        crew = sum(int(r["resource_configuration"].get("k", 1)) for r in mine.values())
        ends = [int(r["end_seconds"]) for r in mine.values()]
        makespan, total = max(ends, default=0), sum(ends)
        if pace == "FAST":
            return delivery + (total, crew, makespan)
        if pace == "SLOW":
            return delivery + (early, workers, crew, makespan)
        return delivery + (workers, crew, makespan, total)

    start_report = None
    if start_candidates:
        # CRAFT V2 starts from the best valid dispatch schedule (e.g. plain
        # EDD and EDD with greedy crews): the floor CRAFT can only improve.
        start_report, best = [], None
        for name, rows in start_candidates:
            rows = [r for r in rows if r["operation_key"] in all_keys_set]
            check = _validate_assembled_trial(state, [*approved_rows, *rows], state["boundary_facts"])
            entry = {"start": name, "operations": len(rows), "validation": bool(check["accepted"])}
            if check["accepted"]:
                entry["score"] = list(v2_score({r["operation_key"]: r for r in rows}))
                if best is None or tuple(entry["score"]) < best[0]:
                    best = (tuple(entry["score"]), name, rows, check)
            start_report.append(entry)
        if best is None:
            raise ValueError("NO_VALID_START_SCHEDULE")
        for entry in start_report:
            entry["chosen"] = entry["start"] == best[1]
        _carry_rows(state, best[2], completed, commitments_m, commitments_w, boundary_facts)
        assembled_validation = best[3]
        windows = []
        identity.update(improve=improve, starts=[name for name, _rows in start_candidates])
    if selected_keys is not None:
        identity.update(approval_fingerprint=approval_fingerprint,
                        approved_assignments_sha256=_hash(approved_rows))
    number = 0
    if resume is not None:
        restored = load_checkpoint(resume, identity=identity)
        _verify_resume_accounting(restored, all_keys)
        if restored['selected_schedule']:
            assembled_validation = _validate_assembled_trial(
                state, [*approved_rows, *restored['selected_schedule']], state['boundary_facts'])
            if not assembled_validation['accepted']:
                raise ValueError('CHECKPOINT_PHYSICAL_REVALIDATION_FAILED')
        _carry_rows(state, restored['selected_schedule'], completed,
                    commitments_m, commitments_w, boundary_facts)
        windows = [tuple(keys) for keys in restored['pending_windows']]
        window_rows = restored['windows']
        deferred_operations = restored['deferred_operations']
        number = restored['attempt_number']
    restored_count = len(completed) - len(approved_keys)
    new_attempts = 0
    def save_progress():
        if checkpoint is not None:
            checkpoint(identity, {"classification": "TENTATIVE_TEST_DIAGNOSTIC_NOT_APPROVAL",
                        "attempt_number": number,
                        "source_sha256": source_hash,
                        "snapshot_hash": readiness["normalized_snapshot_hash"],
                        "scenario_hash": state["source_resource_provenance"]["scenario_hash"],
                        "selected_keys": all_keys, "selected_schedule": [r for k, r in completed.items() if k not in approved_keys],
                        "windows": window_rows, "deferred_operations": deferred_operations,
                        "remaining_selected_keys": sorted(set(all_keys) - set(completed)),
                        "pending_windows": windows, "live_activation": "DISABLED"})
    def solve(keys, done, res_m, res_w, facts, crew_economy, scale=1.0,
              incumbent=None, full_domain=False, distinct=False, move_window_days=None):
        """One canonical window solve + independent assembled validation.

        ``incumbent`` rows (CRAFT V2 moves) are exposed and hinted so the
        solver starts from the current valid plan; ``full_domain`` solves
        one stage over the whole sampled domain; ``distinct`` adds the
        worker-concentration goal (ECONOMY/SLOW pace).
        """
        window_state = {
            **state, "keys": keys,
            "crew_economy_objective": crew_economy,
            "distinct_worker_objective": bool(distinct),
            "_workers_in_use": {str(w) for row in done.values()
                                for w in (row.get("resource_configuration") or {}).get("worker_ids", ())},
            "_incumbent_configs": {
                row["operation_key"]: (row["resource_configuration"]["line_id"],
                                       row["resource_configuration"]["k"],
                                       row["resource_configuration"]["machine_ids"],
                                       row["resource_configuration"]["worker_ids"])
                for row in (incumbent or ())},
            "_incumbent_starts": {row["operation_key"]: int(row["start_seconds"]) for row in (incumbent or ())},
            "route_ready_committed": dict(done),
            "committed_routing_completion": {
                key: {"authoritative_end_seconds": row["end_seconds"]}
                for key, row in done.items()},
            "_machine_res": res_m, "_worker_res": res_w,
            "boundary_facts": tuple(facts),
            "classified": {
                **{key: {"state": "COMMITTED_FUTURE"} for key in done},
                **{key: {"state": "MOVABLE_WINDOW"} for key in keys},
            },
            "operation_execution_contracts": {key: state["operation_execution_contracts"][key]
                                               for key in keys},
            "source_resource_domain": {
                **state["source_resource_domain"],
                "operations": {key: state["source_resource_domain"]["operations"][key]
                               for key in keys}},
        }
        initial_cap = 30 if complete_captured_test_domain else DOMAIN_INITIAL_CAP
        worker_cap = 64 if complete_captured_test_domain else DOMAIN_INITIAL_CAP
        expansion_rounds = 0 if complete_captured_test_domain else DOMAIN_EXPANSION_ROUNDS
        stage = tuple(x * scale for x in STAGE_SECONDS)
        adaptive = run_adaptive_planning_schedule(
            window_state, cache=cache,
            policy=ResourceDomainPolicyV2(initial_machine_sets=initial_cap,
                                          initial_worker_crews=worker_cap,
                                          max_expansion_rounds=expansion_rounds,
                                          diagnostic_time_limit_seconds=2.0),
            objective_mode=route_ready_objective, objective_time_limit_seconds=OBJECTIVE_SECONDS * scale,
            regime_probe=route_ready_probes if route_ready_objective else None,
            canonical_kwargs=({"incumbent_selected_schedule": list(incumbent)} if incumbent else None),
            canonical_plan_factory=((lambda m, w: ((min(m * w, MOVE_CONFIGS), move_window_days or MOVE_WINDOW_DAYS, stage[2]),))
                                    if incumbent else
                                    (lambda m, w: ((m * w, None, stage[2]),)) if full_domain else
                                    (lambda m, w: ((1, 7, stage[0]), (4, 14, stage[1]),
                                                   (m * w, None, stage[2])))))
        acceptance = adaptive.get("acceptance", {})
        rows = acceptance.get("selected_schedule", ())
        ok = (acceptance.get("status") == "ACCEPTED"
              and {row.get("operation_key") for row in rows} == set(keys)
              and acceptance.get("replay", {}).get("status") == "PASS"
              and acceptance.get("validator", {}).get("scope_acceptance", {}).get("physical_status") == "VALID"
              and acceptance.get("validator", {}).get("scope_acceptance", {}).get("scope_status") == "SCOPE_VALID")
        diag = None
        if (not ok and complete_captured_test_domain
                and acceptance.get("stages")
                and acceptance["stages"][-1].get("status") == "INFEASIBLE"):
            # With every alternative exposed under this captured TEST
            # contract, ablate only constraint families to localize the
            # remaining contradiction. This is diagnostic evidence only;
            # the canonical candidate still has to pass all constraints.
            diag = diagnose_resource_domain_pressure(window_state, adaptive, seconds=5.0)
        return ok, list(rows), acceptance, adaptive, diag

    def commit_subset(rows, keys):
        """Validate committing only ``keys`` of a solved candidate."""
        subset = [r for r in rows if r["operation_key"] in keys]
        check = _validate_assembled_trial(state, [*completed.values(), *subset], state["boundary_facts"])
        return (subset if check["accepted"] else None), check

    while windows:
        if max_new_attempts is not None and new_attempts >= max_new_attempts:
            break
        # Always make progress, then stop starting new attempts once the
        # caller's wall budget is used so the result returns within its lease.
        if (attempt_time_budget_seconds is not None and new_attempts
                and time.perf_counter() - started >= attempt_time_budget_seconds):
            break
        window_keys = windows.pop(0)
        number += 1
        new_attempts += 1
        missing = {key: state["after_index"].route_predecessor.get(key)
                   for key in window_keys
                   if state["after_index"].route_predecessor.get(key) is not None
                   and state["after_index"].route_predecessor[key] not in completed
                   and state["after_index"].route_predecessor[key] not in window_keys}
        if missing:
            for key in window_keys:
                deferred_operations[key] = {"reason": "UNRESOLVED_PREDECESSOR", "boundaries": missing}
            save_progress()
            continue
        # Lookahead: also expose the next route chunks (up to the CP cap) so
        # this window is decided knowing what follows; only window_keys commit.
        solve_keys = tuple(window_keys)
        if lookahead and window_sequence is None:
            extra = []
            for later in windows:
                for key in later:
                    if len(solve_keys) + len(extra) >= min(WINDOW_SIZE, len(window_keys) + lookahead):
                        break
                    predecessor = state["after_index"].route_predecessor.get(key)
                    if predecessor is None or predecessor in completed or predecessor in solve_keys or predecessor in extra:
                        extra.append(key)
                    else:
                        break
            solve_keys = solve_keys + tuple(extra)
        accepted, solved, acceptance, adaptive, diagnostic = solve(
            solve_keys, completed, commitments_m, commitments_w, boundary_facts,
            crew_economy=window_sequence is None, full_domain=full_domain_windows)
        solved_keys = solve_keys
        if not accepted and solve_keys != tuple(window_keys):
            solved_keys = tuple(window_keys)
            # The lookahead model may be harder than the window itself.
            accepted, solved, acceptance, adaptive, diagnostic = solve(
                tuple(window_keys), completed, commitments_m, commitments_w, boundary_facts,
                crew_economy=window_sequence is None, full_domain=full_domain_windows)
        if not accepted and len(window_keys) == 1:
            # Last resort before deferring: the same operation with doubled limits.
            accepted, solved, acceptance, adaptive, diagnostic = solve(
                tuple(window_keys), completed, commitments_m, commitments_w, boundary_facts,
                crew_economy=window_sequence is None, full_domain=full_domain_windows, scale=2.0)
        selected, trial_validation = [], None
        if accepted:
            selected, trial_validation = commit_subset(solved, set(window_keys))
            accepted = selected is not None
            if accepted:
                assembled_validation = trial_validation
        guard = None
        stages_ = (acceptance.get("objective") or {}).get("stages") or ()
        delivery = [row for row in stages_ if str(row.get("level", "")).startswith("DELIVERY")]
        delivery_proven = accepted and bool(delivery) and all(row.get("proven_optimal") for row in delivery)
        if dispatch_guard and window_sequence is None and delivery_proven:
            # CRAFT proved its delivery goals optimal on the operations it
            # solved, so EDD cannot beat them there: skip the EDD trial.
            guard = {"chosen": "CRAFT", "skipped": "DELIVERY_PROVEN_OPTIMAL"}
        elif dispatch_guard and window_sequence is None:
            # EDD on the same committed state, one operation at a time; keep
            # whichever plan is better by CRAFT's own goals, so a window is
            # never worse than the dispatch rule on that window.
            trial_done = dict(completed)
            trial_m = {k: list(v) for k, v in commitments_m.items()}
            trial_w = {k: list(v) for k, v in commitments_w.items()}
            trial_facts = list(boundary_facts)
            edd_rows, edd_check = [], None
            ops = state["problem"].operation_by_key
            for key in sorted(solved_keys, key=lambda k: (
                    deadlines.get(str(ops[k].order_id)) if deadlines.get(str(ops[k].order_id)) is not None else 10**12,
                    str(ops[k].order_id), int(ops[k].sequence))):
                ok, rows, _a, _ad, _d = solve((key,), trial_done, trial_m, trial_w, trial_facts,
                                              crew_economy=False)
                if not ok:
                    continue
                check = _validate_assembled_trial(state, [*trial_done.values(), *rows], state["boundary_facts"])
                if not check["accepted"]:
                    continue
                edd_rows.extend(rows)
                edd_check = check
                _carry_rows(state, rows, trial_done, trial_m, trial_w, trial_facts)
            # Compare on the same operation set CRAFT solved (window plus any
            # lookahead); either way only the window's own operations commit.
            craft_score = score(solved, set(solved_keys)) if accepted else (len(solved_keys),)
            edd_score = score(edd_rows, set(solved_keys))
            chosen = "CRAFT"
            if edd_rows and edd_score[0] == 0 and edd_score < craft_score:
                subset, check = commit_subset(edd_rows, set(window_keys))
                if subset is not None:
                    selected, trial_validation, accepted = subset, check, True
                    assembled_validation = check
                    chosen = "EDD"
            guard = {"craft_score": list(craft_score), "edd_score": list(edd_score), "chosen": chosen}
        stage_rows = [row for stage in acceptance.get("stages", ()) for row in [stage]]
        window_rows.append({
            "window": number, "operation_keys": list(window_keys),
            "solved_keys": list(solve_keys), "dispatch_guard": guard,
            "status": acceptance.get("status"), "accepted": bool(accepted),
            "cp_statuses": [row.get("status") for row in stage_rows],
            "replay": acceptance.get("replay", {}).get("status"),
            "physical": acceptance.get("validator", {}).get("scope_acceptance", {}).get("physical_status"),
            "scope": acceptance.get("validator", {}).get("scope_acceptance", {}).get("scope_status"),
            "assembled_prefix_validation": trial_validation,
            "resource_domain": adaptive.get("resource_domain_v2"),
            "complete_domain_diagnostic": diagnostic,
            "calendar_regimes": adaptive.get("regime_count"),
            "stages": stage_rows,
        })
        if not accepted:
            smaller = _split_failed_window(window_keys, halving=window_size is not None or
                                           window_sequence is not None)
            if smaller:
                window_rows[-1]["recovery"] = "RETRY_SMALLER_WINDOWS_WITHOUT_COMMITTING_FAILED_CANDIDATE"
                windows[0:0] = smaller
                save_progress()
                continue
            domain = adaptive.get("resource_domain_v2", {})
            failure = {
                "window": number,
                "canonical_status": acceptance.get("status", "UNKNOWN"),
                "cp_statuses": [row.get("status") for row in stage_rows],
                "resource_domain_status": domain.get("status", "UNKNOWN"),
                "domain_complete": domain.get("resource_domain_complete", False),
                "physical_infeasibility_proven": domain.get("physical_infeasibility_proven", False),
            }
            if trial_validation is not None and not trial_validation["accepted"]:
                failure["independent_validation"] = trial_validation
            for key in window_keys:
                deferred_operations[key] = {
                    **failure,
                    "reason": ("INDEPENDENT_ASSEMBLED_VALIDATION_REJECTED"
                               if trial_validation is not None and not trial_validation["accepted"]
                               else "NO_VALID_CANDIDATE_WITHIN_CURRENT_BOUNDED_DOMAIN_AND_BUDGET"),
                }
            # Other orders have no routing edge to this failed route. Keep the
            # failed identities explicitly deferred, then continue evaluating
            # later independent windows without reserving failed assignments.
            save_progress()
            continue
        _carry_rows(state, selected, completed, commitments_m, commitments_w, boundary_facts)
        save_progress()

    lns_log = []
    if lns_seconds and window_sequence is None and not windows and completed and improve != "V2":
        # Large Neighbourhood Search over the whole assembled schedule. A
        # neighbourhood is the route suffixes of a late seed order plus the
        # orders contending with it -- same machines, overlapping in time
        # (a resource-contention graph). Everything else stays fixed; a move
        # is kept only if the global goals improve AND the whole schedule
        # passes the independent assembled validation.
        def global_score(rows_by_key):
            done_orders, tardy = {}, []
            for order_id, final in final_ops.items():
                if final in rows_by_key and deadlines.get(order_id) is not None:
                    tardy.append(max(0, int(rows_by_key[final]["end_seconds"]) - int(deadlines[order_id])))
            return (len(all_keys) - len(set(rows_by_key) & set(all_keys)), sum(t > 0 for t in tardy),
                    sum(tardy), max(tardy, default=0),
                    sum(int(r["end_seconds"]) for k, r in rows_by_key.items() if k in all_keys))

        def rebuild(rows):
            done, res_m, res_w = {}, {k: list(v) for k, v in state["_machine_res"].items()}, \
                {k: list(v) for k, v in state["_worker_res"].items()}
            facts = list(state["boundary_facts"])
            _carry_rows(state, rows, done, res_m, res_w, facts)
            return done, res_m, res_w, facts

        ops = state["problem"].operation_by_key
        route_of = defaultdict(list)
        for key in all_keys:
            route_of[str(ops[key].order_id)].append(key)
        for order_id in route_of:
            route_of[order_id].sort(key=lambda k: int(ops[k].sequence))
        lns_started, tabu, iteration = time.perf_counter(), [], 0
        import random as _random
        rng = _random.Random(42)
        weights = {"machine": 1.0, "crew": 1.0, "bottleneck": 1.0}
        while time.perf_counter() - lns_started < float(lns_seconds):
            iteration += 1
            current = dict(completed)
            lateness = []
            for order_id, final in final_ops.items():
                if final in current and deadlines.get(order_id) is not None and order_id in route_of:
                    lateness.append((int(current[final]["end_seconds"]) - int(deadlines[order_id]), order_id))
            candidates = [o for t, o in sorted(lateness, reverse=True) if t > 0 and o not in tabu]
            if not candidates:
                break
            seed = candidates[0]
            tabu = (tabu + [seed])[-max(3, len(lateness) // 3):]
            suffix_len = max(2, lns_neighbourhood // 4)
            cap = min(WINDOW_SIZE, lns_neighbourhood)

            def suffix(order_id):
                keys = [k for k in route_of[order_id] if k in current and k not in approved_keys]
                return keys[-suffix_len:]

            # Adaptive choice of neighbourhood type (ALNS roulette): types that
            # produced accepted moves are drawn more often; deterministic RNG.
            total = sum(weights.values())
            draw, kind = rng.random() * total, None
            for name, weight in weights.items():
                draw -= weight
                if draw <= 0:
                    kind = name
                    break
            kind = kind or "machine"
            chosen = list(suffix(seed))
            seed_rows = [current[k] for k in chosen]
            if not seed_rows:
                continue
            lo = min(r["start_seconds"] for r in seed_rows) - 86400
            hi = max(r["end_seconds"] for r in seed_rows)
            contention = defaultdict(int)
            if kind == "bottleneck":
                # Every order passing through the seed's busiest capability in
                # the same period: the shared bottleneck, not one machine.
                load = defaultdict(int)
                for key, row in current.items():
                    if row["start_seconds"] < hi and row["end_seconds"] > lo and key in ops:
                        load[str(ops[key].required_capability)] += row["end_seconds"] - row["start_seconds"]
                seed_caps = {str(ops[k].required_capability) for k in chosen}
                busiest = max(seed_caps, key=lambda c: load.get(c, 0))
                for key, row in current.items():
                    if (key in ops and str(ops[key].required_capability) == busiest
                            and row["start_seconds"] < hi and row["end_seconds"] > lo):
                        contention[str(ops[key].order_id)] += 1
            else:
                field = "machine_ids" if kind == "machine" else "worker_ids"
                held = {str(m) for r in seed_rows for m in r["resource_configuration"][field]}
                for key, row in current.items():
                    if (key in ops and row["start_seconds"] < hi and row["end_seconds"] > lo
                            and held & {str(m) for m in row["resource_configuration"][field]}):
                        contention[str(ops[key].order_id)] += 1
            contention.pop(seed, None)
            for order_id, _count in sorted(contention.items(), key=lambda kv: (-kv[1], kv[0])):
                extra = [k for k in suffix(order_id) if k not in chosen]
                if len(chosen) + len(extra) > cap:
                    continue
                chosen.extend(extra)
            before = global_score(current)
            kept = [r for k, r in current.items() if k not in chosen]
            done, res_m, res_w, facts = rebuild(kept)
            ok, rows, _acc, _ad, _diag = solve(tuple(chosen), done, res_m, res_w, facts,
                                               crew_economy=True, scale=2.0)
            move = {"iteration": iteration, "kind": kind, "seed_order": seed, "neighbourhood": len(chosen),
                    "contending_orders": len(contention), "before": list(before)}
            if ok:
                trial = {**{r["operation_key"]: r for r in kept}, **{r["operation_key"]: r for r in rows}}
                after = global_score(trial)
                check = _validate_assembled_trial(state, list(trial.values()), state["boundary_facts"])
                move.update(after=list(after), validation=check["accepted"])
                if check["accepted"] and after < before:
                    completed.clear()
                    completed.update(trial)
                    commitments_m, commitments_w = res_m, res_w
                    _carry_rows(state, rows, {}, commitments_m, commitments_w, facts)
                    boundary_facts = facts
                    assembled_validation = check
                    move["accepted"] = True
            # Reward improving types, decay the rest (ALNS adaptive weights).
            weights[kind] = max(0.2, 0.8 * weights[kind] + (1.0 if move.get("accepted") else 0.0))
            lns_log.append(move)

    v2 = None
    if improve == "V2" and completed:
        # One valid schedule, improved by CP-SAT large-neighbourhood moves
        # (Pacino & Van Hentenryck 2011: time + resource decompositions):
        #   repair -- place any operation the start could not place;
        #   time   -- an overlapping sweep of start-time windows over the
        #             whole horizon, then random windows;
        #   machine/crew/bottleneck -- a late (or random) order plus the
        #             orders competing with it for the same resources.
        # Every move frees whole route TAILS (an operation and all its later
        # route steps), so a gain reaches the order's delivery date and no
        # fixed successor is left behind. Each re-solve is warm-started from
        # the current plan, which stays legal inside the move, so CP-SAT has
        # a solution at once. A move is kept only if the whole-schedule goals
        # are no worse and the whole schedule passes independent validation.
        import random as _random
        ops = state["problem"].operation_by_key
        route_of = defaultdict(list)
        for key in all_keys:
            route_of[str(ops[key].order_id)].append(key)
        for order_id in route_of:
            route_of[order_id].sort(key=lambda k: int(ops[k].sequence))
        cap = min(WINDOW_SIZE, int(lns_neighbourhood))
        rng = _random.Random(int(os.environ.get("CRAFT_SOLVER_SEED", "42")))
        weights = {"time": 1.0, "machine": 1.0, "crew": 1.0, "bottleneck": 1.0}
        log, v2_started, tabu = [], time.perf_counter(), []

        def rebuild_v2(rows):
            done = {}
            res_m = {k: list(v) for k, v in state["_machine_res"].items()}
            res_w = {k: list(v) for k, v in state["_worker_res"].items()}
            facts = list(state["boundary_facts"])
            _carry_rows(state, rows, done, res_m, res_w, facts)
            return done, res_m, res_w, facts

        def tail(order_id, from_key, placed=None):
            """An operation and all later route steps. With ``placed`` (time and
            graph moves) only steps already in the plan: an unplaced step may
            not fit at all and would make the whole move infeasible; placing
            it is the repair moves' job. Unplaced steps impose no constraint."""
            route = route_of[order_id]
            return [k for k in route[route.index(from_key):] if k not in approved_keys
                    and (placed is None or k in placed)]

        def time_move(current, pointer):
            ordered = sorted((int(r["start_seconds"]), k) for k, r in current.items() if k in all_keys_set)
            chosen, seen, scanned = [], set(), 0
            for _start, key in ordered[pointer:]:
                scanned += 1
                order_id = str(ops[key].order_id)
                if order_id in seen:
                    continue
                piece = tail(order_id, key, current)
                if len(chosen) + len(piece) > cap:
                    if chosen:
                        break
                    continue
                seen.add(order_id)
                chosen.extend(piece)
            # Step by half the window: consecutive windows overlap by half.
            return chosen, pointer + max(1, scanned, len(chosen) // 2), len(ordered)

        def graph_move(current, kind):
            late = sorted(((int(current[f]["end_seconds"]) - int(deadlines[o]), o)
                           for o, f in final_ops.items()
                           if o in route_of and f in current and deadlines.get(o) is not None),
                          reverse=True)
            # Seed: a late order that can still be saved first (due date not
            # yet passed, least late -- late-order count is the first goal),
            # then the remaining late orders, most tardy first.
            origin_due = int(state["reference_seconds"]) if "reference_seconds" in state else 0
            saveable = sorted((t, o) for t, o in late if t > 0 and o not in tabu
                              and int(deadlines[o]) > origin_due)
            late_pool = [o for _t, o in saveable] + [o for t, o in late if t > 0 and o not in tabu
                                                     and int(deadlines[o]) <= origin_due]
            pool = late_pool or [o for o in sorted(route_of) if o not in tabu] or sorted(route_of)
            seed = late_pool[0] if late_pool else rng.choice(pool)
            tabu.append(seed)
            del tabu[:-max(3, len(route_of) // 3)]
            chosen = [k for k in route_of[seed] if k in current][-max(2, cap // 4):]
            if not chosen:
                return []
            seed_rows = [current[k] for k in chosen]
            lo = min(int(r["start_seconds"]) for r in seed_rows) - 86400
            if seed in late_pool:
                # A late order competes for resources from its release on.
                lo = min(lo, max(0, int(state["problem"].order_by_id[ops[chosen[0]].order_id].release_seconds)))
            hi = max(int(r["end_seconds"]) for r in seed_rows)
            contention = defaultdict(int)
            if kind == "bottleneck":
                load = defaultdict(int)
                for key, row in current.items():
                    if key in ops and int(row["start_seconds"]) < hi and int(row["end_seconds"]) > lo:
                        load[str(ops[key].required_capability)] += int(row["end_seconds"]) - int(row["start_seconds"])
                busiest = max({str(ops[k].required_capability) for k in chosen}, key=lambda c: load.get(c, 0))
                for key, row in current.items():
                    if (key in all_keys_set and str(ops[key].required_capability) == busiest
                            and int(row["start_seconds"]) < hi and int(row["end_seconds"]) > lo):
                        contention[str(ops[key].order_id)] += 1
            else:
                field = "machine_ids" if kind == "machine" else "worker_ids"
                held = {str(m) for r in seed_rows for m in r["resource_configuration"][field]}
                for key, row in current.items():
                    if (key in all_keys_set and int(row["start_seconds"]) < hi and int(row["end_seconds"]) > lo
                            and held & {str(m) for m in row["resource_configuration"][field]}):
                        contention[str(ops[key].order_id)] += 1
            contention.pop(seed, None)
            for order_id, _count in sorted(contention.items(), key=lambda kv: (-kv[1], kv[0])):
                first = next((k for k in route_of[order_id]
                              if k in current and int(current[k]["end_seconds"]) > lo), None)
                if first is None:
                    continue
                extra = [k for k in tail(order_id, first, current) if k not in chosen]
                if len(chosen) + len(extra) <= cap:
                    chosen.extend(extra)
            return chosen

        def attempt(kind, chosen, current):
            before = v2_score(current)
            kept = [r for k, r in current.items() if k not in chosen]
            done, res_m, res_w, facts = rebuild_v2(kept)
            incumbent = [current[k] for k in chosen if k in current]
            ok, rows, acc, adaptive, _diag = solve(
                tuple(chosen), done, res_m, res_w, facts, crew_economy=True,
                incumbent=incumbent, full_domain=True, distinct=pace in ("ECONOMY", "SLOW"),
                # Resource moves may push competing work further back to make
                # room for a late order; time sweeps stay local.
                move_window_days=None if kind in ("time", "repair") else 2 * MOVE_WINDOW_DAYS + 1)
            move = {"kind": kind, "operations": len(chosen), "keys": list(chosen),
                    "before": list(before), "solved": bool(ok),
                    "status": acc.get("status"), "reason": acc.get("reason"),
                    "cp_stages": [{f: st.get(f) for f in ("status", "hint_count", "incumbent_hints", "incumbent_end_reproduced", "incumbent_missing", "incumbent_core", "regimes", "resource_configurations", "solve_seconds")}
                                  for st in acc.get("stages", ())],
                    "objective_stages": [(row.get("level"), row.get("status")) for row in
                                         ((acc.get("objective") or {}).get("stages") or ())],
                    "replay": (acc.get("replay") or {}).get("status"),
                    "physical": ((acc.get("validator") or {}).get("scope_acceptance") or {}).get("physical_status"),
                    "domain": (adaptive.get("resource_domain_v2") or {}).get("status")}
            if ok:
                trial = {**{r["operation_key"]: r for r in kept}, **{r["operation_key"]: r for r in rows}}
                after = v2_score(trial)
                move["after"] = list(after)
                if after <= before:
                    check = _validate_assembled_trial(state, list(trial.values()), state["boundary_facts"])
                    move["validation"] = bool(check["accepted"])
                    if check["accepted"]:
                        move.update(accepted=True, improved=after < before)
                        return trial, check, move
            return None, None, move

        queue = []
        for order_id, route in sorted(route_of.items()):
            missing = [k for k in route if k not in completed]
            if missing and len(tail(order_id, missing[0])) <= cap:
                queue.append(("repair", tail(order_id, missing[0])))
        pointer, sweeps, sweep_moves, stall = 0, 0, 0, 0
        while time.perf_counter() - v2_started < float(lns_seconds):
            current = dict(completed)
            if queue:
                kind, chosen = queue.pop(0)
            elif sweeps == 0:
                kind = "time"
                chosen, pointer, total = time_move(current, pointer)
                sweep_moves += 1
                if pointer >= total:
                    sweeps, pointer = 1, 0
            else:
                draw, kind = rng.random() * sum(weights.values()), "time"
                for name, weight in weights.items():
                    draw -= weight
                    if draw <= 0:
                        kind = name
                        break
                if kind == "time":
                    size = sum(1 for k in current if k in all_keys_set)
                    chosen, _p, _t = time_move(current, rng.randrange(max(1, size)))
                else:
                    chosen = graph_move(current, kind)
            if not chosen:
                stall += 1
                if sweeps and stall >= max(8, 2 * sweep_moves):
                    break
                continue
            mark = time.perf_counter()
            trial, check, move = attempt(kind, chosen, current)
            move.update(iteration=len(log) + 1, seconds=round(time.perf_counter() - mark, 1))
            if trial is not None:
                completed.clear()
                completed.update(trial)
                assembled_validation = check
            if sweeps:
                weights[kind] = max(0.2, 0.8 * weights[kind] + (1.0 if move.get("improved") else 0.0))
            stall = 0 if move.get("improved") else stall + 1
            log.append(move)
            # Stop early once the search has stalled for two sweeps' worth.
            if sweeps and stall >= max(8, 2 * sweep_moves):
                break
        for key in list(deferred_operations):
            if key in completed:
                del deferred_operations[key]
        for key in all_keys:
            if key not in completed:
                deferred_operations.setdefault(key, {"reason": "NOT_PLACED_BY_START_OR_IMPROVEMENT_MOVES"})
        v2 = {"pace": pace, "starts": start_report, "seconds": round(time.perf_counter() - v2_started, 1),
              "moves": log, "accepted_moves": sum(bool(m.get("accepted")) for m in log),
              "improving_moves": sum(bool(m.get("improved")) for m in log),
              "first_sweep_complete": bool(sweeps), "weights": weights,
              "final_score": list(v2_score(completed))}

    elapsed = time.perf_counter() - started
    peak = psutil.Process().memory_info().rss / 1024**2
    return {
        "classification": "PINNED_CAPTURE_TEST_ONLY_NOT_PRODUCTION",
        "restored_operations": restored_count,
        "pending_windows": windows,
        "controller_status": ("CHECKPOINTED" if windows else
                              "COMPLETE" if set(all_keys) <= set(completed) else "PARTIAL"),
        "factory_id": factory_id, "target": target,
        "route_ready_probes_and_existing_lexicographic_objective": route_ready_objective,
        "source_sha256": source_hash,
        "snapshot_hash": readiness["normalized_snapshot_hash"],
        "scenario_hash": state["source_resource_provenance"]["scenario_hash"],
        "scenario_assumptions": state["test_assumptions"],
        "resource_domain_policy": {
            "initial_machine_sets": 30 if complete_captured_test_domain else DOMAIN_INITIAL_CAP,
            "initial_worker_crews": 64 if complete_captured_test_domain else DOMAIN_INITIAL_CAP,
            "max_expansion_rounds": 0 if complete_captured_test_domain else DOMAIN_EXPANSION_ROUNDS,
            "diagnostic_time_limit_seconds": 5 if complete_captured_test_domain else 2,
            "domain_scope": ("COMPLETE_WITHIN_PINNED_CAPTURE_AND_DECLARED_TEST_ELIGIBILITY"
                             if complete_captured_test_domain else "BOUNDED_CAPTURE_CANDIDATE_SAMPLE"),
        },
        "global_operations": int(readiness["operation_count"]),
        "global_execution_counts": readiness["counts"],
        "eligible_order_count": len(routes),
        "selected_order_ids": [order["production_order_id"] for _route, order in selected_routes],
        "selected_keys": list(all_keys), "scheduled_keys": sorted(set(completed) - approved_keys),
        "selected_schedule": [r for k, r in completed.items() if k not in approved_keys],
        "approved_outside_count": len(approved_keys),
        "approval_fingerprint": approval_fingerprint,
        "blocked_partial_operations": [row["operation_key"] for row in snapshot["operations"]
                                       if row["execution_state"] == "PARTIALLY_EXECUTED"],
        "completed_operations_excluded": sum(row["execution_state"] == "FULLY_COMPLETED"
                                              for row in snapshot["operations"]),
        "windows": window_rows,
        "assembled_schedule_validation": assembled_validation or {
            "status": "NOT_RUN_NO_WINDOW_ACCEPTED"},
        "accepted_windows": sum(row["accepted"] for row in window_rows),
        "rejected_windows": sum(not row["accepted"] for row in window_rows),
        "remaining_selected_keys": sorted(set(all_keys) - set(completed)),
        "deferred_operations": deferred_operations,
        "lns": {"seconds": float(lns_seconds), "moves": lns_log,
                "accepted_moves": sum(bool(m.get("accepted")) for m in lns_log)} if lns_seconds else None,
        "craft_v2": v2,
        "partial_open_holds_quarantined": state["test_partial_holds"],
        "domain_complete": False,
        "calendar_domain_completeness": "NOT_ESTABLISHED",
        "physical_infeasibility_proven": False,
        "domain_completeness_scope": (
            "ALL_ALTERNATIVES_IN_PINNED_CAPTURE_UNDER_DECLARED_TEST_ELIGIBILITY"
            if complete_captured_test_domain else "BOUNDED_SAMPLE_ONLY"),
        "outside_uncommitted_future_conflict_closure": False,
        "total_wall_seconds": elapsed, "sampled_peak_rss_mb": max(memory, peak),
        "live_activation": "DISABLED",
    }


def run_and_save(factory_id, target, *, complete_captured_test_domain=False,
                 route_ready_objective=False, resume=None, max_new_attempts=None):
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    sequence = 0
    def checkpoint(identity, payload):
        nonlocal sequence
        sequence += 1
        path = HERE / f"factory{factory_id}_{target}_{run_stamp}_checkpoint_{sequence:03d}.json"
        save_checkpoint(path, identity=identity, payload=payload)
    result = _run(int(factory_id), target,
                  complete_captured_test_domain=complete_captured_test_domain,
                  checkpoint=checkpoint, route_ready_objective=route_ready_objective,
                  resume=resume, max_new_attempts=max_new_attempts)
    output = HERE / f"factory{factory_id}_{target}_execution_contract_v1_{run_stamp}.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
                      encoding="utf-8")
    print(json.dumps({"output": str(output), "factory": factory_id, "target": target,
                      "scheduled": len(result["scheduled_keys"]),
                      "windows_accepted": result["accepted_windows"],
                      "windows_rejected": result["rejected_windows"],
                      "runtime_seconds": result["total_wall_seconds"],
                      "peak_rss_mb": result["sampled_peak_rss_mb"]}))
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("factory_id", type=int, choices=(4, 6))
    parser.add_argument("target", choices=("10", "25", "all"))
    parser.add_argument("--complete-test-domain", action="store_true",
                        help="expose all captured eligibility alternatives under the unchanged TEST assumptions")
    parser.add_argument("--route-ready-objective", action="store_true",
                        help="reuse additive route-ready probes and existing lexicographic objectives")
    parser.add_argument('--resume', type=Path)
    parser.add_argument('--max-new-attempts', type=int)
    args = parser.parse_args()
    run_and_save(args.factory_id, args.target,
                 complete_captured_test_domain=args.complete_test_domain,
                 route_ready_objective=args.route_ready_objective,
                 resume=args.resume, max_new_attempts=args.max_new_attempts)
