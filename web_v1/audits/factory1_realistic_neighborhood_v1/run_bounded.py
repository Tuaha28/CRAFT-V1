"""Simulation-only source-derived multi-order CRAFT bounded-window experiment.

The configuration domain is deliberately bounded and *incomplete*. Every
configuration is source-eligible; failure is therefore a computational/domain
limit, never a proof of factory infeasibility. All 35 simulated fixed holds are
enforced even though only two belong to the selected routing prefix.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import psutil
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import pymysql
pymysql.connect = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("LEGACY_CONNECTION_FORBIDDEN"))

from crafter.contracts import LineSpec, MachineSpec, OperationSpec, OrderSpec, SchedulingProblem
from crafter.execution_contract import OperationExecutionContract, TEST_ASSUMPTION
from crafter.repair.calendar_regimes import CalendarRegimeCache, generate_calendar_regimes
from crafter.repair.resource_configs import ResourceConfiguration, _diverse_sets, generate_resource_configurations
from crafter.repair.resource_domain_v2 import ResourceDomainPolicyV2, run_adaptive_planning_schedule
from crafter.repair.solve import run_canonical_schedule
from ga.worker_allocation import operator_available_at
from utils.machine_taxonomy import machine_type_to_operation_type
from web_v1.audits.factory1_realistic_neighborhood_v1.analyze_scope import inspect, SOURCE, SNAPSHOT, SCENARIO
from web_v1.audits.factory1_realistic_neighborhood_v1.boundary_policy import check_boundary
from web_v1.backend.integrations.craft.global_resource_state import verify as verify_global_state
from web_v1.backend.integrations.craft.factory1_test_state import approved_boundary as boundary_from_global_state

HERE = Path(__file__).resolve().parent
KEYS_8 = ("790_972", "790_973", "33_3578", "33_3579",
          "790_974", "790_975", "33_3580", "33_3581")
KEYS_16 = KEYS_8 + ("790_976", "790_977", "33_3582", "33_3583",
                    "790_978", "790_979", "790_980", "33_3584")


def next_earliest_order_keys():
    """Extend the proven two-order prefix by one whole source route."""
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    by_order = defaultdict(list)
    for row in snapshot["operations"]:
        by_order[row["order_id"]].append(row)
    released = sorted((row for row in source["orders"]
                       if row["scheduled_start_date"] <= source["reference_time"][:10]
                       and any(op["execution_state"] != "FULLY_COMPLETED"
                               for op in by_order[str(row["production_order_id"])])),
                      key=lambda row: (row["delivery_deadline"], row["production_order_id"]))
    if tuple(str(row["production_order_id"]) for row in released[:3]) != ("790", "33", "469"):
        raise RuntimeError("EARLIEST_DUE_ORDER_PREFIX_CHANGED")
    third = sorted(by_order["469"], key=lambda row: row["routing_sequence"])
    if len(third) != 9 or any(row["execution_state"] != "UNSTARTED" for row in third):
        raise RuntimeError("THIRD_ORDER_ROUTE_CHANGED")
    return KEYS_16 + tuple(row["operation_key"] for row in third)


def offset(origin, timestamp):
    return int((datetime.fromisoformat(timestamp) - origin).total_seconds())


def route_ready_probes(state, regimes, committed, keys):
    """Probe exact calendar domains near a route-ready lower bound.

    A sparse breakpoint map can discard a long mixed-shift interval even
    though legal starts exist inside it. These probes *add* exact-verifiable
    calendar slices; they neither assert feasibility nor remove alternatives.
    """
    predecessor = state["after_index"].route_predecessor
    order = sorted(keys, key=lambda key: (key.split("_")[0],
                                          state["problem"].operation_by_key[key].sequence))
    earliest_end = {}
    probes = {}
    for key in order:
        pred = predecessor.get(key)
        ready = int(committed[pred]["end_seconds"]) if pred in committed else int(earliest_end.get(pred, 0))
        choices = []
        for regime in regimes.get(key, ()):
            start = max(int(regime["lo"]), ready)
            if start <= int(regime["hi"]):
                slope, intercept = regime["formula"][0]
                choices.append(int(slope) * start + int(intercept))
        if choices:
            earliest_end[key] = min(choices)
        else:
            # This is a breakpoint hint only. It never becomes a committed
            # completion or a CP lower bound; exact execution still gates it.
            configs = state["resource_configurations_for_probes"].get(key, ())
            if not configs:
                # A zero domain is allowed here and is reported by the
                # regime/domain stage; offer no hint rather than fail.
                earliest_end[key] = ready
                probes[key] = ()
                continue
            duration = min(config.productive_seconds + config.setup_seconds
                           for config in configs)
            earliest_end[key] = ready + duration
        probes[key] = tuple(ready + day * 86400 for day in range(3))
    return probes, earliest_end


def construct(keys=KEYS_8, *, domain_width=None, approved_boundary=None,
              global_resource_state=None, resource_policy="incumbent_prefix"):
    audit = inspect()
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    scenario = json.loads(SCENARIO.read_text(encoding="utf-8"))
    if global_resource_state is not None:
        verify_global_state(global_resource_state)
        exported_boundary = boundary_from_global_state(global_resource_state)
        if (global_resource_state['source_hash'] != audit['source_sha256'] or
                global_resource_state.get('scenario_hash') != scenario['scenario_hash'] or
                set(global_resource_state['selected_keys']) != set(keys) or
                global_resource_state['factory_id'] != 1 or
                approved_boundary != exported_boundary):
            raise RuntimeError('GLOBAL_STATE_FACTORY1_PROVENANCE_OR_SCOPE')
    origin = datetime.fromisoformat(source["reference_time"])
    operations = {r["operation_key"]: r for r in snapshot["operations"]}
    selected_keys = set(keys)
    for key in keys:
        predecessor = operations[key]["predecessor"]
        approved_keys = {row['operation_key'] for row in (approved_boundary or {}).get('assignments', ())}
        if (predecessor and predecessor not in selected_keys and
                predecessor not in approved_keys and
                operations[predecessor]["execution_state"] != "FULLY_COMPLETED"):
            raise RuntimeError(f"UNRESOLVED_EXTERNAL_PREDECESSOR:{predecessor}->{key}")
    boundary_errors = check_boundary(approved_boundary, source_sha256=audit["source_sha256"],
                                     selected_keys=selected_keys, known_keys=set(operations))
    if boundary_errors:
        raise RuntimeError(f"OUTSIDE_BOUNDARY_INVALID:{boundary_errors}")
    routes = {f'{r["production_order_id"]}_{r["operation_id"]}': r for r in source["operations"]}
    partials = {r["operation_key"]: r for r in scenario["partial_operations"]}
    active_lines = {r['line_id'] for r in source['resources']['lines']
                    if r['factory_id'] == 1 and r['is_active']}
    machines = {r["asset_id"]: r for r in source["resources"]["machines"]
                if r["current_status"] == "Active" and r['factory_id'] == 1
                and r['line_id'] in active_lines}
    workers = {r["employee_id"]: r for r in source["resources"]["workers"]
               if r["is_active"] and r['factory_id'] == 1}
    leave = {r["employee_id"] for r in source["resources"]["work_schedules"] if r["is_on_leave"]}
    memberships = {r["employee_id"]: r["batch_id"] for r in source["resources"]["batch_members"]}
    batches = {r["batch_id"]: r for r in source["resources"]["batches"]}
    caps = defaultdict(set)
    skills = defaultdict(dict)
    for row in source["resources"]["machine_capabilities"]:
        if row["asset_id"] in machines:
            caps[row["operation_type"].lower()].add(row["asset_id"])
    for row in source["resources"]["skills"]:
        wid = row["employee_id"]
        if wid in workers and wid in memberships and wid not in leave:
            skills[row["operation_type"].lower()][wid] = Decimal(str(row["efficiency_rating"])) / 100

    # A bounded source-eligible machine/worker sample per capability.
    # Prefer machines actually planned on these source lots, including the
    # cross-order shared machine where available. Planned assignments are
    # candidate witnesses, NOT physical future commitments.
    selected_caps = {key: machine_type_to_operation_type(routes[key]["required_machine_type"]) for key in keys}
    domain_width = int(domain_width if domain_width is not None else (3 if len(keys) > 8 else 2))
    if not 1 <= domain_width <= 4:
        raise ValueError("AUDIT_DOMAIN_WIDTH_OUT_OF_BOUNDS")
    if resource_policy not in {"incumbent_prefix", "stage_b_diverse", "stage_b_adaptive"}:
        raise ValueError("UNKNOWN_RESOURCE_POLICY")
    chosen = {}
    source_options = {}
    for capability in sorted(set(selected_caps.values())):
        planned = defaultdict(set)
        for key in keys:
            if selected_caps[key] == capability:
                for lot in operations[key]["lots"]:
                    if lot["state"] == "NOT_STARTED" and lot["machine_id"] in caps[capability]:
                        planned[lot["machine_id"]].add(operations[key]["order_id"])
        ordered_machines = sorted(caps[capability], key=lambda mid: (-len(planned[mid]), mid not in planned, mid))
        machine_pair = (ordered_machines[:domain_width] if resource_policy == "incumbent_prefix"
                        else _diverse_sets(ordered_machines, 1, domain_width)[0])
        machine_pair = tuple(int(item[0]) if isinstance(item, tuple) else int(item)
                             for item in machine_pair)
        if len(machine_pair) != domain_width:
            raise RuntimeError(f"INSUFFICIENT_MACHINES:{capability}")
        # Shared skilled workers create genuine competition. Efficiency is
        # taken from the captured skill record, not assumed to be 100%.
        planned_workers = defaultdict(set)
        for key in keys:
            if selected_caps[key] == capability:
                for lot in operations[key]["lots"]:
                    if lot["state"] == "NOT_STARTED" and lot["worker_id"] in skills[capability]:
                        planned_workers[lot["worker_id"]].add(operations[key]["order_id"])
        ordered_workers = sorted(skills[capability], key=lambda wid: (-len(planned_workers[wid]), wid not in planned_workers, wid))
        worker_pair = (ordered_workers[:domain_width] if resource_policy == "incumbent_prefix"
                       else _diverse_sets(ordered_workers, 1, domain_width)[0])
        worker_pair = tuple(int(item[0]) if isinstance(item, tuple) else int(item)
                            for item in worker_pair)
        if len(worker_pair) != domain_width:
            raise RuntimeError(f"INSUFFICIENT_WORKERS:{capability}")
        chosen[capability] = (machine_pair, worker_pair)
        source_options[capability] = (tuple(ordered_machines), tuple(ordered_workers))

    def execution_contract_for(key, machine_pool, worker_pool):
        capability = selected_caps[key]
        quantity = int(operations[key]["quantity"]["not_started"])
        return OperationExecutionContract(
            operation_key=key, factory_id=1,
            required_worker_count=1, required_machine_count=1,
            required_skills=(capability,),
            eligible_worker_ids=tuple(str(wid) for wid in worker_pool),
            eligible_machine_ids=tuple(int(mid) for mid in machine_pool),
            eligible_line_ids=tuple(sorted({int(machines[mid]["line_id"])
                                            for mid in machine_pool})),
            quantity=quantity,
            target_quantity=int(operations[key]["quantity"]["target"]),
            completed_quantity=int(operations[key]["quantity"]["completed"]),
            in_flight_quantity=int(operations[key]["quantity"]["in_flight"]),
            quantity_basis=("UNSTARTED_RESIDUAL_UNITS"
                            if quantity != int(operations[key]["quantity"]["target"])
                            else "UNSTARTED_REMAINING_UNITS"),
            smv_minutes_per_unit=str(routes[key]["standard_allowed_minutes"]),
            setup_seconds=900, evidence_class=TEST_ASSUMPTION,
            source_fingerprint=audit["source_sha256"],
            snapshot_fingerprint=snapshot["snapshot_hash"],
            scenario_fingerprint=scenario["scenario_hash"],
            worker_efficiency_percent=tuple(sorted(
                (str(wid), str(Decimal(str(skills[capability][wid])) * 100))
                for wid in worker_pool)),
            machine_speedup_factor="1",
            pairing_policy="ELIGIBLE_CARTESIAN_BUNDLE",
            worker_calendar_policy="CAPTURED_TEST_ROTATION_AND_REST_DAY",
            machine_calendar_policy="TEST_SNAPSHOT_STATIC_STATUS",
        )

    specs, configs, ledger = [], {}, {}
    line_ids, machine_ids, worker_ids = set(), set(), set()
    by_factory, by_line, by_skill, line_factory, crew_of, efficiency_of = defaultdict(list), defaultdict(list), defaultdict(list), {}, {}, {}
    for key in keys:
        row, route = operations[key], routes[key]
        capability = selected_caps[key]
        quantity = row["quantity"]
        if quantity["completed"] + quantity["in_flight"] + quantity["not_started"] != quantity["target"]:
            raise RuntimeError(f"QUANTITY_NOT_CONSERVED:{key}")
        if key in partials and (partials[key]["accepted_completed_quantity"] != quantity["completed"] or
                                partials[key]["unstarted_residual_quantity"] != quantity["not_started"]):
            raise RuntimeError(f"SCENARIO_LEDGER_MISMATCH:{key}")
        base = Decimal(str(route["standard_allowed_minutes"])) * quantity["not_started"]
        machines_here, workers_here = chosen[capability]
        rows = []
        for mid in machines_here:
            lid = machines[mid]["line_id"]
            for wid in workers_here:
                productive_minutes = base / skills[capability][wid]
                duration = int((productive_minutes * 60).to_integral_value(rounding=ROUND_CEILING))
                rows.append(ResourceConfiguration(key, 1, lid, 1, (mid,), (str(wid),), 900,
                                                  duration, ("SIMULATED_BOUNDED_LEGAL_SUBSET",), float(productive_minutes),
                                                  int(quantity["not_started"]),
                                                  "CRAFT_OPERATION_EXECUTION_V1", TEST_ASSUMPTION))
                line_ids.add(lid)
                machine_ids.add(mid)
                worker_ids.add(wid)
                by_factory[(1, capability)].append(wid)
                by_line[(lid, capability)].append(wid)
                by_skill[capability].append(wid)
                line_factory[lid] = 1
                batch = batches[memberships[wid]]
                # Fixed-crew execution converts numeric worker IDs to int
                # for operator_available_at; retain source integer keys.
                crew_of[wid] = (batch["rotation_offset"], batch["rest_day_offset"])
                efficiency_of[(wid, capability)] = float(skills[capability][wid])
        configs[key] = tuple(rows)
        specs.append(OperationSpec(key, row["order_id"], str(route["operation_id"]),
                                   row["routing_sequence"], capability,
                                   tuple(sorted({machines[mid]["line_id"] for mid in
                                                 (source_options[capability][0] if resource_policy == "stage_b_adaptive"
                                                  else machines_here)})),
                                   int((base * 60).to_integral_value(rounding=ROUND_CEILING)), 900, 1, 1))
        ledger[key] = {"target": quantity["target"], "completed": quantity["completed"],
                       "in_flight": quantity["in_flight"], "tail": quantity["not_started"],
                       "source_lot_ids": [lot["lot_id"] for lot in row["lots"] if lot["state"] == "NOT_STARTED"]}

    source_domain = None
    if resource_policy == "stage_b_adaptive":
        for capability, (all_machines, all_workers) in source_options.items():
            for mid in all_machines:
                machine_ids.add(mid)
                line_ids.add(machines[mid]["line_id"])
                line_factory[machines[mid]["line_id"]] = 1
            for wid in all_workers:
                worker_ids.add(wid)
                batch = batches[memberships[wid]]
                crew_of[wid] = (batch["rotation_offset"], batch["rest_day_offset"])
                efficiency_of[(wid, capability)] = float(skills[capability][wid])
                by_factory[(1, capability)].append(str(wid))
                by_skill[capability].append(str(wid))
        source_domain = {
            'contract_version': 'SOURCE_RESOURCE_DOMAIN_V1',
            'source_sha256': audit['source_sha256'],
            'snapshot_hash': snapshot['snapshot_hash'],
            'scenario_hash': scenario['scenario_hash'],
            'operations': {
                key: {
                    'factory_id': 1, 'crew_size': 1,
                    'machine_ids': source_options[selected_caps[key]][0],
                    'worker_ids': tuple(str(wid) for wid in source_options[selected_caps[key]][1]),
                    'worker_efficiency': {str(wid): str(skills[selected_caps[key]][wid])
                                          for wid in source_options[selected_caps[key]][1]},
                    'base_productive_minutes': str(Decimal(str(routes[key]['standard_allowed_minutes']))
                                                   * operations[key]['quantity']['not_started']),
                    'setup_seconds': 900,
                    'execution_contract': execution_contract_for(
                        key, source_options[selected_caps[key]][0],
                        source_options[selected_caps[key]][1]).as_mapping(),
                    # Captured skill/capability records are complete for this
                    # source revision, not proof of every possible factory fact.
                    'machine_pool_complete': True, 'worker_pool_complete': True,
                } for key in keys},
        }

    machine_res, worker_res, facts = defaultdict(list), defaultdict(list), []
    all_holds = [(p["operation_key"], hold) for p in scenario["partial_operations"]
                 for hold in p["fixed_in_flight_holds"]]
    if len(all_holds) != 35:
        raise RuntimeError("SCENARIO_HOLD_COUNT_CHANGED")
    if global_resource_state is not None:
        expected = sorted((str(hold['lot_id']), int(hold['machine_id']),
                           offset(origin, hold['machine_hold_from']),
                           offset(origin, hold['machine_hold_until']), str(hold['worker_id']),
                           tuple((offset(origin, a), offset(origin, b))
                                 for a, b in hold['worker_productive_segments']))
                          for _key, hold in all_holds)
        exported = sorted((str(row['lot_id']), int(row['machine_id']),
                           row['start_seconds'], row['end_seconds'],
                           row['worker_segments'][0][0],
                           tuple((segment[1], segment[2]) for segment in row['worker_segments']))
                          for row in global_resource_state['fixed_commitments'])
        if expected != exported:
            raise RuntimeError('GLOBAL_STATE_FACTORY1_FIXED_HOLDS_CHANGED')
    for hold_key, hold in all_holds:
        mid, wid = hold["machine_id"], hold["worker_id"]
        a, b = offset(origin, hold["machine_hold_from"]), offset(origin, hold["machine_hold_until"])
        if mid not in machines or b <= a or a < 0 or machines[mid]["line_id"] != hold["line_id"]:
            raise RuntimeError(f"INVALID_HOLD:{hold['lot_id']}")
        machine_ids.add(mid)
        line_ids.add(hold["line_id"])
        machine_res[mid].append((a, b, hold_key.split("_")[0], f"fixed-lot-{hold['lot_id']}"))
        facts.append({"machine_id": mid, "start_seconds": a, "end_seconds": b,
                      "operation_key": hold_key})
        for left, right in hold["worker_productive_segments"]:
            wa, wb = datetime.fromisoformat(left), datetime.fromisoformat(right)
            if wa < origin or wb > origin + timedelta(seconds=b) or wb <= wa:
                raise RuntimeError(f"INVALID_HOLD_SEGMENT:{hold['lot_id']}")
            worker_res[str(wid)].append((wa, wb))
            facts.append({"worker_id": str(wid), "start_seconds": offset(origin, left),
                          "end_seconds": offset(origin, right), "operation_key": hold_key})
    for outside in (approved_boundary or {}).get("assignments", ()):
        key = outside["operation_key"]
        mid = outside["machine_id"]
        if mid not in machines:
            raise RuntimeError(f"OUTSIDE_APPROVED_MACHINE_UNKNOWN:{key}")
        machine_ids.add(mid)
        line_ids.add(machines[mid]["line_id"])
        a, b = outside["start_seconds"], outside["end_seconds"]
        machine_res[mid].append((a, b, key.split("_")[0], f"approved-{key}"))
        facts.append({"machine_id": mid, "start_seconds": a, "end_seconds": b,
                      "operation_key": key})
        for wid, left, right in outside.get("worker_segments", ()):
            worker_res[str(wid)].append((origin + timedelta(seconds=left),
                                         origin + timedelta(seconds=right)))
            facts.append({"worker_id": str(wid), "start_seconds": left,
                          "end_seconds": right, "operation_key": key})
    for group in machine_res.values():
        intervals = sorted((row[0], row[1]) for row in group)
        if any(left[1] > right[0] for left, right in zip(intervals, intervals[1:])):
            raise RuntimeError("OUTSIDE_APPROVED_MACHINE_CONFLICT")
    for group in worker_res.values():
        intervals = sorted((int((row[0] - origin).total_seconds()),
                            int((row[1] - origin).total_seconds())) for row in group)
        if any(left[1] > right[0] for left, right in zip(intervals, intervals[1:])):
            raise RuntimeError("OUTSIDE_APPROVED_WORKER_CONFLICT")
    problem = SchedulingProblem("F1-SIMULATED-BOUNDED-NOT-PRODUCTION", origin, 30 * 86400,
                                tuple(LineSpec(lid, 1) for lid in sorted(line_ids)),
                                tuple(MachineSpec(mid, 1, machines[mid]["line_id"],
                                      tuple(sorted(c for c, ids in caps.items() if mid in ids))) for mid in sorted(machine_ids)),
                                tuple(OrderSpec(order_id, 1, 0, None)
                                      for order_id in sorted({key.split("_")[0] for key in keys})),
                                tuple(specs), ())
    predecessors = {key: operations[key]["predecessor"] for key in keys}
    successors = {key: operations[key]["successor"] for key in keys}
    route_index = SimpleNamespace(rows=(), by_key={key: {"start_minutes": 0} for key in keys},
                                  route_predecessor=predecessors, route_successor=successors)
    state = {"problem": problem, "operator_index": {"by_factory": dict(by_factory), "by_line": dict(by_line),
             "by_skill": dict(by_skill), "line_factory": line_factory, "crew_of": crew_of,
             "shift_of": {}, "efficiency_of": efficiency_of}, "after_index": route_index,
             "keys": keys, "baseline": SimpleNamespace(operations=()), "event": None,
             "planning_context": {"mode": "INITIAL_PLAN"}, "objective_profile": "INITIAL_PLAN_NO_STABILITY",
             "classified": {key: {"state": "MOVABLE_WINDOW"} for key in keys},
             "_machine_res": dict(machine_res), "_worker_res": dict(worker_res), "boundary_facts": tuple(facts)}
    contract_domains = {
        key: (source_options[selected_caps[key]] if resource_policy == "stage_b_adaptive"
              else chosen[selected_caps[key]])
        for key in keys}
    state['operation_execution_contracts'] = {
        key: execution_contract_for(key, pools[0], pools[1]).as_mapping()
        for key, pools in contract_domains.items()}
    state['allow_test_execution_assumptions'] = True
    state['require_execution_contracts'] = True

    def independent_worker_interval(worker_id, start, end):
        worker = int(worker_id) if str(worker_id).isdigit() else worker_id
        if end <= start:
            return False
        # The captured TEST rotation changes at 06/14/22 and local midnight;
        # rest-day and week-rotation changes also occur at midnight. Checking
        # each boundary is exact for this declared calendar contract.
        transitions = []
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        while day <= end:
            transitions.extend(day + timedelta(hours=h) for h in (0, 6, 14, 22, 24))
            day += timedelta(days=1)
        points = [start, end - timedelta(microseconds=1)]
        points.extend(value for value in transitions if start <= value < end)
        return all(operator_available_at(state['operator_index'], worker, point)
                   for point in points)

    def independent_machine_interval(machine_id, start, end):
        machine = machines.get(int(machine_id))
        return bool(machine and machine['current_status'] == 'Active' and end > start)

    state['worker_interval_is_available'] = independent_worker_interval
    state['machine_interval_is_available'] = independent_machine_interval
    if source_domain is not None:
        state['source_resource_domain'] = source_domain
        state['source_resource_provenance'] = {
            name: source_domain[name] for name in
            ('source_sha256', 'snapshot_hash', 'scenario_hash')}
        configs, resource_audit, _ = generate_resource_configurations(
            state, max_machine_sets=domain_width, max_worker_crews=domain_width)
        state['source_resource_audit'] = resource_audit
        state['operation_execution_contracts'] = {
            key: spec['execution_contract']
            for key, spec in source_domain['operations'].items()}
        state['allow_test_execution_assumptions'] = True
    domain = {"complete": False, "truncated": True, "scope_conflict_closed": False,
              "policy": "EARLIEST_DUE_RELEASED_ORDER_PREFIX_WITH_ALL_FIXED_HOLDS",
              "selected_order_ids": sorted({key.split("_")[0] for key in keys}),
              "outside_uncommitted_operations_deferred": sum(
                  op["operation_key"] not in set(keys) and op["execution_state"] != "FULLY_COMPLETED"
                  for op in snapshot["operations"]),
              "selected_configurations": sum(len(v) for v in configs.values()),
              "domain_width_per_capability": domain_width,
              "resource_selection_policy": resource_policy,
              "outside_approved_version": (approved_boundary or {}).get("active_version_id"),
              "outside_approved_assignment_hash": (approved_boundary or {}).get("assignments_sha256"),
              "legal_machine_counts": {key: len(caps[selected_caps[key]]) for key in keys},
              "legal_worker_counts": {key: len(skills[selected_caps[key]]) for key in keys},
              "chosen_machine_ids": {cap: ids[0] for cap, ids in chosen.items()},
              "chosen_worker_ids": {cap: ids[1] for cap, ids in chosen.items()},
              "factory_fixed_holds": len(all_holds), "selected_fixed_holds": sum(len(partials.get(k, {}).get("fixed_in_flight_holds", ())) for k in keys),
              "lines": sorted(line_ids), "machine_count": len(machine_ids), "worker_count": len(worker_ids)}
    return source, snapshot, scenario, state, configs, ledger, domain


def run(keys=KEYS_8, *, domain_width=None, approved_boundary=None,
        global_resource_state=None, resource_policy="incumbent_prefix",
        window_size=4, cycle_target=None, checkpoint=None, max_new_windows=None,
        adaptive_retries=False):
    started = time.perf_counter()
    process = psutil.Process()
    memory_samples = {"start": process.memory_info().rss / 1024**2}
    source, snapshot, scenario, state, configs, ledger, domain = construct(
        keys, domain_width=domain_width, approved_boundary=approved_boundary,
        global_resource_state=global_resource_state, resource_policy=resource_policy)
    construction = time.perf_counter() - started
    memory_samples["after_construction"] = process.memory_info().rss / 1024**2
    if type(window_size) is not int or window_size < 1 or window_size > 25:
        raise ValueError('WINDOW_SIZE_OUTSIDE_BOUNDED_POLICY')
    if cycle_target is not None and (type(cycle_target) is not int or cycle_target < window_size):
        raise ValueError('CYCLE_TARGET_INVALID')
    if max_new_windows is not None and (type(max_new_windows) is not int or max_new_windows < 1):
        raise ValueError('WINDOW_PROGRESS_BUDGET_INVALID')
    if cycle_target is None:
        windows = tuple(tuple(keys[i:i+window_size]) for i in range(0, len(keys), window_size))
    else:
        # A cycle boundary can fall inside a nominal window.
        chunks = []
        index = 0
        while index < len(keys):
            stop = min(index + window_size, ((index // cycle_target) + 1) * cycle_target, len(keys))
            chunks.append(tuple(keys[index:stop]))
            index = stop
        windows = tuple(chunks)
    scheduling_input = {"keys": keys, "ledger": ledger, "domain": domain,
                        "configs": {key: [vars(config) for config in configs[key]] for key in keys}}
    if state.get('source_resource_domain') is not None:
        scheduling_input['source_resource_domain_sha256'] = hashlib.sha256(
            json.dumps(state['source_resource_domain'], sort_keys=True, default=str,
                       separators=(',', ':')).encode()).hexdigest()
    if global_resource_state is not None:
        scheduling_input['global_state_fingerprint'] = global_resource_state['fingerprint']
    scheduling_input_hash = hashlib.sha256(json.dumps(scheduling_input, sort_keys=True, default=str,
                                                      separators=(",", ":")).encode()).hexdigest()
    results = []
    committed = {}
    machine_res = deepcopy(state["_machine_res"])
    worker_res = deepcopy(state["_worker_res"])
    facts = list(state["boundary_facts"])
    outside_completions = {row['operation_key']: {'authoritative_end_seconds': row['end_seconds']}
                           for row in (approved_boundary or {}).get('assignments', ())}
    restored_prefix_validation = None
    cycle_options = {'cycle_target': cycle_target, 'window_size': window_size}
    if adaptive_retries:
        if cycle_target is None:
            raise ValueError('ADAPTIVE_REQUIRES_CYCLE_POLICY')
        cycle_options['adaptive_retries'] = True
    adaptive_cp = None
    if adaptive_retries:
        from crafter.production.cycles import new_checkpoint, record_window
        adaptive_cp = checkpoint or new_checkpoint(factory_id=1,
            input_fingerprint=scheduling_input_hash, operation_keys=keys, options=cycle_options)
    if checkpoint is not None:
        from crafter.production.cycles import verify_checkpoint
        verify_checkpoint(checkpoint, factory_id=1,
                          input_fingerprint=scheduling_input_hash,
                          operation_keys=keys,
                          options=cycle_options)
        prior_all = checkpoint['windows']
        prior = [row for row in prior_all if row['status'] == 'ACCEPTED']
        if not adaptive_retries and (len(prior) != len(prior_all) or len(prior) > len(windows) or
                any(tuple(row['keys']) != windows[i] or row['status'] != 'ACCEPTED'
                    for i, row in enumerate(prior))):
            raise ValueError('CYCLE_CHECKPOINT_WINDOW_MISMATCH')
        # The checkpoint hash is not physical evidence. Reconstruct a source-
        # checked prefix before any restored reservation reaches a new solve.
        if prior:
            from web_v1.audits.factory1_realistic_neighborhood_v1.validate_bounded import validate_payload
            prefix_keys = [key for window in prior for key in window['keys']]
            prefix_ledger = {key: ledger[key] for key in prefix_keys}
            prefix_input = {**scheduling_input, 'keys': prefix_keys,
                            'ledger': prefix_ledger,
                            'configs': {key: scheduling_input['configs'][key] for key in prefix_keys}}
            prefix = {
                'classification': 'SIMULATION_ONLY_BOUNDED_INCOMPLETE_DOMAIN_NOT_LIVE',
                'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                'snapshot_hash': snapshot['snapshot_hash'], 'scenario_hash': scenario['scenario_hash'],
                'selected_keys': prefix_keys, 'accepted_keys': prefix_keys, 'remaining_keys': [],
                'ledger': prefix_ledger, 'domain': domain, 'windows': prior,
                'window_size': window_size, 'cycle_target': cycle_target,
                'scheduling_input': prefix_input,
                'scheduling_input_hash': hashlib.sha256(json.dumps(
                    prefix_input, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()}
            if adaptive_retries:
                prefix.update(adaptive_retries=True, attempt_operation_keys=list(keys),
                              attempt_trace=prior_all)
            restored_prefix_validation = validate_payload(
                source, snapshot, scenario, prefix, SOURCE.read_bytes(), approved_boundary)
            if restored_prefix_validation['status'] != 'PASS':
                raise ValueError('CYCLE_CHECKPOINT_PHYSICAL_INVALID:' +
                                 ','.join(restored_prefix_validation['errors']))
        results = deepcopy(prior_all)
        for result in prior:
            for row in result['selected_schedule']:
                key = row['operation_key']
                committed[key] = row
                for mid in row['resource_configuration']['machine_ids']:
                    machine_res.setdefault(mid, []).append((row['start_seconds'], row['end_seconds'],
                                                             key.split('_')[0], key))
                    facts.append({'machine_id': mid, 'start_seconds': row['start_seconds'],
                                  'end_seconds': row['end_seconds'], 'operation_key': key})
                for wid in row['resource_configuration']['worker_ids']:
                    for a, b in row['worker_segments']:
                        worker_res.setdefault(str(wid), []).append((state['problem'].origin + timedelta(seconds=a),
                                                                     state['problem'].origin + timedelta(seconds=b)))
                        facts.append({'worker_id': str(wid), 'start_seconds': a,
                                      'end_seconds': b, 'operation_key': key})
    new_windows = 0
    pending_fixed = list(windows[len(results):])
    while adaptive_cp['pending_windows'] if adaptive_retries else pending_fixed:
        if max_new_windows is not None and new_windows >= max_new_windows:
            break
        number = len(results) + 1
        window_keys = tuple(adaptive_cp['pending_windows'][0]['keys']) if adaptive_retries else pending_fixed.pop(0)
        new_windows += 1
        if adaptive_retries:
            complete_source = {row['operation_key'] for row in snapshot['operations']
                               if row['execution_state'] == 'FULLY_COMPLETED'}
            available = set(window_keys) | set(committed) | set(outside_completions) | complete_source
            missing = {key: state['after_index'].route_predecessor.get(key) for key in window_keys
                       if state['after_index'].route_predecessor.get(key) is not None
                       and state['after_index'].route_predecessor[key] not in available}
            if missing:
                failed = {'number': number, 'keys': list(window_keys), 'status': 'REJECTED',
                          'selected_schedule': [], 'failure_reason': 'PREDECESSOR_UNRESOLVED',
                          'missing_predecessors': missing, 'stages': []}
                results.append(failed)
                adaptive_cp = record_window(adaptive_cp, failed, independent_status='NOT_REACHED')
                continue
        window_state = {**state, "keys": window_keys, "_machine_res": machine_res,
                        "_worker_res": worker_res, "boundary_facts": tuple(facts),
                        "planning_context": {"mode": "INITIAL_PLAN" if number == 1 else "ROLLING_PLAN"},
                        "classified": {
                            **{row['operation_key']: {'state': 'COMMITTED_FUTURE'}
                               for row in (approved_boundary or {}).get('assignments', ())},
                            **{key: {"state": "COMMITTED_FUTURE" if key in committed else
                                      "MOVABLE_WINDOW" if key in window_keys else "FUTURE_OUTSIDE_WINDOW"}
                               for key in keys}},
                        "committed_routing_completion": {
                            **outside_completions,
                            **{key: {"authoritative_end_seconds": row["end_seconds"]}
                               for key, row in committed.items()}}}
        if resource_policy == 'stage_b_adaptive':
            window_state['route_ready_committed'] = {
                **{row['operation_key']: row for row in
                   (approved_boundary or {}).get('assignments', ())}, **committed}
        selected_configs = {key: configs[key] for key in window_keys}
        t = time.perf_counter()
        calendar_cache = CalendarRegimeCache()
        if resource_policy != 'stage_b_adaptive':
            regimes, _, count, equivalence = generate_calendar_regimes(
                window_state, selected_configs, cache=calendar_cache)
            probes = {}
        if len(keys) > 8 and resource_policy != 'stage_b_adaptive':
            probe_state = {**window_state, "resource_configurations_for_probes": selected_configs}
            # A route may have several operations in the same window. New
            # exact regimes for an upstream operation can establish an earlier
            # downstream route-ready hint. Iterate only up to route length;
            # every pass adds breakpoints and never removes prior probes.
            for _ in range(len(window_keys)):
                probe_committed = {row['operation_key']: row for row in
                                   (approved_boundary or {}).get('assignments', ())}
                probe_committed.update(committed)
                proposed, _ = route_ready_probes(probe_state, regimes, probe_committed, window_keys)
                combined = {key: tuple(sorted(set(probes.get(key, ())) | set(proposed[key])))
                            for key in window_keys}
                if combined == probes:
                    break
                probes = combined
                regimes, _, count, equivalence = generate_calendar_regimes(
                    window_state, selected_configs,
                    cache=calendar_cache,
                    additional_breakpoints_by_operation=probes)
        plan = ((1, 7, 5), (4, 7, 10),
                (domain["domain_width_per_capability"] ** 2, 14, 10),
                (domain["domain_width_per_capability"] ** 2, None, 10))
        adaptive_trace = None
        if resource_policy == 'stage_b_adaptive':
            # The generic adaptive controller now receives the same pinned
            # source-derived duration/eligibility contract as this runner.
            # Exact route-ready probes are retained in shared preparation.
            adaptive = run_adaptive_planning_schedule(
                window_state, cache=calendar_cache,
                policy=ResourceDomainPolicyV2(
                    initial_machine_sets=domain_width,
                    initial_worker_crews=domain_width,
                    max_expansion_rounds=3,
                    diagnostic_time_limit_seconds=3),
                canonical_plan_factory=lambda machine_cap, worker_cap: (
                    (1, 7, 5), (4, 7, 10),
                    (machine_cap * worker_cap, 14, 10),
                    (machine_cap * worker_cap, None, 10)),
                objective_mode=adaptive_retries or len(keys) > 8,
                objective_time_limit_seconds=10,
                regime_probe=route_ready_probes if adaptive_retries or len(keys) > 8 else None)
            acceptance = adaptive['acceptance']
            regimes = adaptive['regimes']
            count = adaptive['regime_count']
            equivalence = adaptive['calendar_equivalence']
            probes = adaptive.get('route_ready_breakpoint_probes', {})
            adaptive_trace = adaptive.get('resource_domain_v2')
            calendar_seconds = sum(float(r.get('timing', {}).get('calendar_regime_generation_seconds', 0))
                                   for r in (adaptive,))
            solve_seconds = max(0.0, time.perf_counter() - t - calendar_seconds)
            selected_configs = adaptive['resource_configurations']
        else:
            calendar_seconds = time.perf_counter() - t
            t = time.perf_counter()
            acceptance = run_canonical_schedule({"state": window_state, "resource_configurations": selected_configs,
                                                  "regimes": regimes},
                                                 plan=plan,
                                                 objective_mode=len(keys) > 8,
                                                 objective_time_limit_seconds=10)["acceptance"]
            solve_seconds = time.perf_counter() - t
        memory_samples[f"window_{number}_after_calendar"] = process.memory_info().rss / 1024**2
        memory_samples[f"window_{number}_after_solve"] = process.memory_info().rss / 1024**2
        rows = acceptance.get("selected_schedule") or []
        result = {"number": number, "keys": window_keys, "status": acceptance["status"],
                  "route_ready_breakpoint_probes": probes,
                  "regime_bounds": {key: {"first_start": min(r["lo"] for r in regimes[key]),
                                          "last_start": max(r["hi"] for r in regimes[key]),
                                          "starts_within_7d": sum(r["lo"] < 7 * 86400 for r in regimes[key]),
                                          "starts_within_14d": sum(r["lo"] < 14 * 86400 for r in regimes[key])}
                                    for key in window_keys if regimes[key]},
                  "stages": [{"status": stage["status"], "config_limit": stage.get("config_limit"),
                              "regimes": stage.get("regimes"), "solve_seconds": stage.get("solve_seconds"),
                              "cp_model_build_seconds": stage.get("cp_model_build_seconds")}
                             for stage in acceptance["stages"]],
                  "calendar_regimes": count, "calendar_equivalence_mismatches": equivalence["mismatches"],
                  "resource_domain_v2": adaptive_trace,
                  "resource_configurations": sum(len(v) for v in selected_configs.values()),
                  "calendar_cache_profile": calendar_cache.snapshot(),
                  "calendar_seconds": calendar_seconds, "solve_and_validation_seconds": solve_seconds,
                  "materialization_seconds": acceptance.get("timing", {}).get("materialization_seconds"),
                  "replay_validator_seconds": acceptance.get("timing", {}).get("replay_validator_seconds"),
                  "replay": acceptance.get("replay", {}).get("status"),
                  "replay_diagnostics": acceptance.get("replay", {}).get("mismatches", []),
                  "materialization_mismatch_count": acceptance.get("materialization", {}).get("mismatch_count"),
                  "objective_status": acceptance.get("objective", {}).get("status"),
                  "objective_stages": [{"name": item.get("name"), "status": item.get("status"),
                                        "value": item.get("value"), "runtime_seconds": item.get("runtime_seconds")}
                                       for item in acceptance.get("objective", {}).get("stages", [])],
                  "physical": acceptance.get("validator", {}).get("scope_acceptance", {}).get("physical_status"),
                  "scope": acceptance.get("validator", {}).get("scope_acceptance", {}).get("scope_status"),
                  "selected_schedule": rows}
        results.append(result)
        if acceptance["status"] != "ACCEPTED" or {r["operation_key"] for r in rows} != set(window_keys):
            if adaptive_retries:
                reason = ('EXACT_REPLAY_FAILED' if result['replay'] == 'FAIL' else
                          'MATERIALIZATION_FAILED' if result['materialization_mismatch_count'] else
                          'PHYSICAL_OR_SCOPE_VALIDATION_FAILED' if
                          result['physical'] not in (None, 'VALID') or result['scope'] not in (None, 'SCOPE_VALID') else
                          'BOUNDED_RESOURCE_OR_CALENDAR_LIMIT_NOT_PHYSICAL_PROOF')
                result.update(status='REJECTED', selected_schedule=[], failure_reason=reason)
                adaptive_cp = record_window(adaptive_cp, result, independent_status='NOT_REACHED')
                continue
            break
        if adaptive_retries:
            adaptive_cp = record_window(adaptive_cp, result, independent_status='PASS')
        for row in rows:
            key = row["operation_key"]
            committed[key] = row
            for mid in row["resource_configuration"]["machine_ids"]:
                machine_res.setdefault(mid, []).append((row["start_seconds"], row["end_seconds"], key.split("_")[0], key))
                facts.append({"machine_id": mid, "start_seconds": row["start_seconds"],
                              "end_seconds": row["end_seconds"], "operation_key": key})
            for wid in row["resource_configuration"]["worker_ids"]:
                for a, b in row["worker_segments"]:
                    worker_res.setdefault(str(wid), []).append((state["problem"].origin + timedelta(seconds=a),
                                                                 state["problem"].origin + timedelta(seconds=b)))
                    facts.append({"worker_id": str(wid), "start_seconds": a, "end_seconds": b, "operation_key": key})
    output = {"classification": "SIMULATION_ONLY_BOUNDED_INCOMPLETE_DOMAIN_NOT_LIVE",
              "restored_prefix_validation": restored_prefix_validation,
              "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
              "snapshot_hash": snapshot["snapshot_hash"], "scenario_hash": scenario["scenario_hash"],
              "selected_keys": keys, "scheduling_input_hash": scheduling_input_hash,
              "global_state_fingerprint": (global_resource_state or {}).get('fingerprint'),
              "scheduling_input": scheduling_input,
              "ledger": ledger, "domain": domain,
              "construction_seconds": construction, "total_wall_seconds": time.perf_counter() - started,
              "sampled_peak_rss_mb": max(memory_samples.values()),
              "memory_samples_mb": memory_samples,
              "windows": results, "accepted_keys": sorted(committed), "remaining_keys": sorted(set(keys) - set(committed))}
    if cycle_target is not None:
        output['cycle_target'] = cycle_target
        output['window_size'] = window_size
        output['cycles_attempted'] = (sum(len(w['keys']) for w in results) + cycle_target - 1) // cycle_target
    if adaptive_retries:
        output.update(adaptive_retries=True, attempt_operation_keys=list(keys),
                      attempt_trace=results, deferred=adaptive_cp['deferred'])
        output['cycles_attempted'] = max((w['cycle_number'] for w in adaptive_cp['windows']), default=0)
    return output


def main():
    all16 = "--all16" in sys.argv[1:]
    next25 = "--next25" in sys.argv[1:]
    expanded25 = "--expanded25" in sys.argv[1:]
    stage_b_25 = "--stage-b-25" in sys.argv[1:]
    approved_fixture = "--approved-fixture" in sys.argv[1:]
    keys = next_earliest_order_keys() if next25 or expanded25 or stage_b_25 else KEYS_16 if all16 else KEYS_8
    boundary = (json.loads((HERE / "approved_outside_fixture_v1.json").read_text(encoding="utf-8"))
                if approved_fixture else None)
    result = run(keys, domain_width=4 if expanded25 or stage_b_25 else None,
                 approved_boundary=boundary,
                 resource_policy="stage_b_diverse" if stage_b_25 else "incumbent_prefix")
    path = HERE / ("bounded_25_stage_b_v1.json" if stage_b_25 else
                   "bounded_8_with_approved_v1.json" if approved_fixture else
                   "bounded_25_expanded_v1.json" if expanded25 else
                   "bounded_25_cached_v2.json" if next25 else
                   "bounded_16_cached_v3.json" if all16 else
                   "bounded_8_cached_v3.json")
    path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"status": [w["status"] for w in result["windows"]],
                      "accepted": len(result["accepted_keys"]), "remaining": result["remaining_keys"],
                      "runtime": result["total_wall_seconds"], "path": str(path)}))
    return 0 if len(result["accepted_keys"]) == len(keys) else 2


if __name__ == "__main__":
    raise SystemExit(main())
