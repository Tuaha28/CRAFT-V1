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
from crafter.execution_contract import OperationExecutionContract, TEST_ASSUMPTION
from crafter.production.checkpoints import load_checkpoint, save_checkpoint
from crafter.repair.calendar_regimes import CalendarRegimeCache
from crafter.repair.resource_domain_v2 import (
    ResourceDomainPolicyV2, diagnose_resource_domain_pressure,
    run_adaptive_planning_schedule,
)
from ga.worker_allocation import operator_available_at
from utils.machine_taxonomy import machine_type_to_operation_type
from web_v1.backend.integrations.craft.snapshot_adapter import normalize_snapshot


CAPTURE = ROOT / "web_v1/audits/factory_source_integration_v1/capture_20261012T223420800478Z"
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


def _split_failed_window(keys):
    """Finite routing-order-preserving recovery: <=5 -> <=2 -> 1."""
    if len(keys) <= 1:
        return []
    size = 2 if len(keys) > 2 else 1
    return [tuple(keys[index:index + size]) for index in range(0, len(keys), size)]


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


def _eligible_order_routes(source, snapshot):
    """Only whole orders whose selected route starts UNSTARTED are admitted."""
    groups = defaultdict(list)
    for operation in snapshot["operations"]:
        groups[str(operation["order_id"])].append(operation)
    wip_operations = {str(row["production_order_id"]) + "_" + str(row["operation_id"])
                      for row in source.get("wip", ()) if int(row.get("pieces_completed", 0)) > 0}
    routes = []
    for order_id, rows in groups.items():
        rows.sort(key=lambda row: (int(row["routing_sequence"]), str(row["operation_key"])))
        if not rows or any(row["execution_state"] != "UNSTARTED" for row in rows):
            continue
        if any(row["operation_key"] in wip_operations for row in rows):
            continue
        order = next(item for item in source["orders"]
                     if str(item["production_order_id"]) == order_id)
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
             *, complete_captured_test_domain=False):
    origin = datetime.fromisoformat(source["reference_time"])
    assumptions = {
        "contract": "CRAFT_OPERATION_EXECUTION_V1",
        "factory_id": factory_id,
        "required_workers": 1,
        "required_machines": 1,
        "setup_seconds": SETUP_SECONDS,
        "machine_speedup_factor": "1",
        "worker_line_policy": "BATCH_LINE_ONLY_TEST",
        "worker_calendar_policy": "CAPTURED_TEST_BATCH_ROTATION_AND_REST_DAY",
        "machine_calendar_policy": "CAPTURE_ACTIVE_STATUS_ONLY_TEST",
        "partial_execution_policy": "BLOCK_PARTIAL_OPERATIONS_AND_QUARANTINE_OPEN_HOLDS_TO_HORIZON",
        "precedence_policy": "WHOLE_OPERATION",
    }
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
            eligible_workers = tuple(sorted({int(wid) for wid, _mid in allowed_pairs}))
            if not eligible_machines or not eligible_workers or not allowed_pairs:
                raise ValueError(f"NO_CAPTURE_ELIGIBLE_LINE_RESOURCE_PAIR:{key}")
            smv = Decimal(str(raw_op["standard_allowed_minutes"]))
            base_seconds = int((Decimal(quantity) * smv * Decimal(60))
                               .to_integral_value(rounding=ROUND_CEILING))
            operation_specs.append(OperationSpec(
                key, order_id, str(raw_op["operation_id"]), int(raw_op["operation_sequence_number"]),
                capability, lines, max(1, base_seconds), SETUP_SECONDS, 1, 1))
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
                allowed_worker_machine_pairs=allowed_pairs)
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

    # Quarantine open-ended partial-operation holds through this TEST horizon.
    # This is deliberately conservative; it is not a claim about a real future
    # release time. No partial operation is admitted to the candidate set.
    machine_res, worker_res, boundary_facts = defaultdict(list), defaultdict(list), []
    partial_holds = []
    for operation in snapshot["operations"]:
        if operation["execution_state"] != "PARTIALLY_EXECUTED":
            continue
        for hold in operation.get("resource_holds", ()):
            machine_id = int(hold["machine_id"])
            worker_id = str(hold["worker_id"])
            machine_res[machine_id].append((0, int(horizon), operation["order_id"],
                                            "TEST-OPEN-HOLD-" + str(hold["lot_id"])))
            worker_res[worker_id].append((origin, origin + timedelta(seconds=horizon)))
            boundary_facts.append({"machine_id": machine_id, "start_seconds": 0,
                                   "end_seconds": int(horizon), "operation_key": operation["operation_key"]})
            boundary_facts.append({"worker_id": worker_id, "start_seconds": 0,
                                   "end_seconds": int(horizon), "operation_key": operation["operation_key"]})
            partial_holds.append({"operation_key": operation["operation_key"],
                                  "lot_id": hold["lot_id"], "machine_id": machine_id,
                                  "worker_id": worker_id, "release": "UNKNOWN_QUARANTINED_TO_TEST_HORIZON"})

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
         selected_keys=None, approved_rows=(), approval_fingerprint=None):
    # Reuse the already validated additive probes; no Factory 1 data is loaded.
    from web_v1.audits.factory1_realistic_neighborhood_v1.run_bounded import route_ready_probes
    started = time.perf_counter()
    memory = psutil.Process().memory_info().rss / 1024**2
    source, readiness, snapshot, source_hash = _source(factory_id)
    routes = _eligible_order_routes(source, snapshot)
    if type(factory_id) is not int or factory_id not in range(1, 7):
        raise ValueError('CAPTURE_FACTORY_NOT_SUPPORTED')
    from crafter.production.capture_boundaries import select_exact_routes
    if approved_rows and (selected_keys is None or not approval_fingerprint):
        raise ValueError('CAPTURE_APPROVAL_SNAPSHOT_REQUIRED')
    selected_routes = (_select_routes(routes, target) if selected_keys is None else
                       select_exact_routes(routes, selected_keys, approved_rows, factory_id))
    state = _prepare(factory_id, source, readiness, snapshot, source_hash, selected_routes,
                     complete_captured_test_domain=complete_captured_test_domain)
    approved_keys = {row['operation_key'] for row in approved_rows}
    all_keys = tuple(row["operation_key"] for route, _order in selected_routes for row in route
                     if row['operation_key'] not in approved_keys)
    if len(all_keys) != len(set(all_keys)):
        raise ValueError("DUPLICATE_SELECTED_OPERATION_IDENTITY")

    # Keep each selected order's precedence-closed route prefix together and
    # schedule no more than 25 operations in a CP-SAT model. The ALL gate uses
    # whole eligible routes (each source route is below the model cap).
    pending_routes = [[row for row in route if row['operation_key'] not in approved_keys]
                      for route, _order in selected_routes]
    windows = [tuple(row["operation_key"] for row in route[index:index + 5])
               for route in pending_routes for index in range(0, len(route), 5)]
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
                'recovery_policy': 'ORDERED_5_2_1_V1',
                'implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
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
    while windows:
        if max_new_attempts is not None and new_attempts >= max_new_attempts:
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
        window_state = {
            **state, "keys": window_keys,
            "route_ready_committed": dict(completed),
            "committed_routing_completion": {
                key: {"authoritative_end_seconds": row["end_seconds"]}
                for key, row in completed.items()},
            "_machine_res": commitments_m, "_worker_res": commitments_w,
            "boundary_facts": tuple(boundary_facts),
            "classified": {
                **{key: {"state": "COMMITTED_FUTURE"} for key in completed},
                **{key: {"state": "MOVABLE_WINDOW"} for key in window_keys},
            },
            "operation_execution_contracts": {key: state["operation_execution_contracts"][key]
                                               for key in window_keys},
            "source_resource_domain": {
                **state["source_resource_domain"],
                "operations": {key: state["source_resource_domain"]["operations"][key]
                               for key in window_keys}},
        }
        initial_cap = 30 if complete_captured_test_domain else DOMAIN_INITIAL_CAP
        worker_cap = 64 if complete_captured_test_domain else DOMAIN_INITIAL_CAP
        expansion_rounds = 0 if complete_captured_test_domain else DOMAIN_EXPANSION_ROUNDS
        adaptive = run_adaptive_planning_schedule(
            window_state, cache=cache,
            policy=ResourceDomainPolicyV2(initial_machine_sets=initial_cap,
                                          initial_worker_crews=worker_cap,
                                          max_expansion_rounds=expansion_rounds,
                                          diagnostic_time_limit_seconds=2.0),
            objective_mode=route_ready_objective, objective_time_limit_seconds=5,
            regime_probe=route_ready_probes if route_ready_objective else None,
            canonical_plan_factory=lambda m, w: ((1, 7, 3), (4, 14, 5),
                                                  (m * w, None, 8)))
        acceptance = adaptive.get("acceptance", {})
        selected = acceptance.get("selected_schedule", ())
        accepted = (acceptance.get("status") == "ACCEPTED"
                    and {row.get("operation_key") for row in selected} == set(window_keys)
                    and acceptance.get("replay", {}).get("status") == "PASS"
                    and acceptance.get("validator", {}).get("scope_acceptance", {}).get("physical_status") == "VALID"
                    and acceptance.get("validator", {}).get("scope_acceptance", {}).get("scope_status") == "SCOPE_VALID")
        diagnostic = None
        if (not accepted and complete_captured_test_domain
                and acceptance.get("stages")
                and acceptance["stages"][-1].get("status") == "INFEASIBLE"):
            # With every alternative exposed under this captured TEST
            # contract, ablate only constraint families to localize the
            # remaining contradiction. This is diagnostic evidence only;
            # the canonical candidate still has to pass all constraints.
            diagnostic = diagnose_resource_domain_pressure(
                window_state, adaptive, seconds=5.0)
        trial_validation = None
        if accepted:
            trial_rows = [*completed.values(), *selected]
            trial_validation = _validate_assembled_trial(
                state, trial_rows, state["boundary_facts"])
            accepted = bool(trial_validation["accepted"])
            if accepted:
                assembled_validation = trial_validation
        stage_rows = [row for stage in acceptance.get("stages", ()) for row in [stage]]
        window_rows.append({
            "window": number, "operation_keys": list(window_keys),
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
            smaller = _split_failed_window(window_keys)
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
