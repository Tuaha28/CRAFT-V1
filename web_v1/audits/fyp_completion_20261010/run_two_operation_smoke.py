"""Source-derived, restricted-domain two-operation CRAFT smoke; not full Factory 1.

Both operations are unstarted route roots from the same captured source. Their
source lots identify a shared eligible machine/worker pair. The pair is checked
against capabilities, skills and calendar metadata before it is used as a
restricted simulation choice. No source DB access or schedule activation occurs.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import pymysql


def reject_database_connection(*_args, **_kwargs):
    raise RuntimeError("SOURCE_CAPTURE_ONLY: legacy database connection forbidden")


pymysql.connect = reject_database_connection

from crafter.contracts import LineSpec, MachineSpec, OperationSpec, OrderSpec, SchedulingProblem
from crafter.repair.calendar_regimes import generate_calendar_regimes
from crafter.repair.resource_configs import ResourceConfiguration
from crafter.repair.solve import run_canonical_schedule
from utils.machine_taxonomy import machine_type_to_operation_type

HERE = Path(__file__).resolve().parent
SOURCE = ROOT / "web_v1/audits/execution_boundary_v1/factory1_source.json"
SNAPSHOT = ROOT / "web_v1/audits/execution_boundary_v1/factory1_normalized_snapshot.json"
KEYS = ("119_4023", "469_1744")
MACHINE_ID, WORKER_ID, LINE_ID = 1459, 7600, 49


def main() -> int:
    start = time.perf_counter()
    source_bytes = SOURCE.read_bytes()
    source = json.loads(source_bytes)
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    scenario = json.loads((ROOT / "web_v1/audits/factory1_simulated_continuation_v1/scenario.json").read_text(encoding="utf-8"))
    if hashlib.sha256(source_bytes).hexdigest() != scenario["source_fingerprint"]:
        raise RuntimeError("SOURCE_FINGERPRINT_CHANGED")
    origin = datetime.fromisoformat(source["reference_time"])
    machine = next(m for m in source["resources"]["machines"] if m["asset_id"] == MACHINE_ID)
    worker = next(w for w in source["resources"]["workers"] if w["employee_id"] == WORKER_ID)
    member = next(m for m in source["resources"]["batch_members"] if m["employee_id"] == WORKER_ID)
    batch = next(b for b in source["resources"]["batches"] if b["batch_id"] == member["batch_id"])
    if machine["line_id"] != LINE_ID or machine["current_status"] != "Active" or not worker["is_active"]:
        raise RuntimeError("SHARED_RESOURCE_UNAVAILABLE")
    machine_caps = {m["operation_type"].lower() for m in source["resources"]["machine_capabilities"] if m["asset_id"] == MACHINE_ID}
    worker_skills = {s["operation_type"].lower() for s in source["resources"]["skills"] if s["employee_id"] == WORKER_ID}
    operations, orders, configs, ledger = [], [], {}, {}
    for key in KEYS:
        row = next(r for r in snapshot["operations"] if r["operation_key"] == key)
        raw = next(r for r in source["operations"] if f'{r["production_order_id"]}_{r["operation_id"]}' == key)
        capability = machine_type_to_operation_type(raw["required_machine_type"])
        if (row["execution_state"] != "UNSTARTED" or row["predecessor"] is not None or
                row["precedence_boundary"]["unresolved_predecessors"] or
                row["release_time"] > source["reference_time"] or
                capability not in machine_caps or capability not in worker_skills or
                not any(lot["machine_id"] == MACHINE_ID and lot["worker_id"] == WORKER_ID and
                        lot["line_id"] == LINE_ID for lot in row["lots"])):
            raise RuntimeError(f"INELIGIBLE_SOURCE_OPERATION:{key}")
        quantity = row["quantity"]["target"]
        if row["quantity"]["completed"] or row["quantity"]["remaining"] != quantity:
            raise RuntimeError(f"QUANTITY_LEDGER_MISMATCH:{key}")
        # Explicit simulation assumption: 100% efficiency, SAM x all unstarted units.
        # This is not an observed productive duration or proof of live factory rates.
        duration = int((Decimal(str(raw["standard_allowed_minutes"])) * quantity * 60).to_integral_value())
        order_id = str(raw["production_order_id"])
        operations.append(OperationSpec(key, order_id, str(raw["operation_id"]), row["routing_sequence"],
                                        capability, (LINE_ID,), duration, 900, 1, 1))
        orders.append(OrderSpec(order_id, 1, 0, None))
        configs[key] = (ResourceConfiguration(key, 1, LINE_ID, 1, (MACHINE_ID,), (str(WORKER_ID),),
                                               900, duration, ("SIMULATION_ONLY_RESTRICTED_LEGAL_DOMAIN",), duration / 60),)
        ledger[key] = {"required_quantity": quantity, "completed_quantity": 0, "remaining_quantity": quantity,
                       "hypothetical_productive_seconds": duration, "out_of_scope_successor": row["successor"]}
    problem = SchedulingProblem("F1-SIMULATED-TWO-OP-NOT-PRODUCTION", origin, 15 * 86400,
                                (LineSpec(LINE_ID, 1),),
                                (MachineSpec(MACHINE_ID, 1, LINE_ID, (operations[0].required_capability,)),),
                                tuple(orders), tuple(operations), ())
    capability = operations[0].required_capability
    if any(op.required_capability != capability for op in operations):
        raise RuntimeError("MIXED_CAPABILITY")
    index = {"by_factory": {(1, capability): [WORKER_ID]}, "by_line": {(LINE_ID, capability): [WORKER_ID]},
             "by_skill": {capability: [WORKER_ID]}, "line_factory": {LINE_ID: 1},
             "crew_of": {WORKER_ID: (batch["rotation_offset"], batch["rest_day_offset"])},
             "shift_of": {}, "efficiency_of": {(WORKER_ID, capability): 1.0}}
    route_index = SimpleNamespace(rows=(), by_key={key: {"start_minutes": 0} for key in KEYS},
                                  route_predecessor={key: None for key in KEYS},
                                  route_successor={key: None for key in KEYS})
    state = {"problem": problem, "operator_index": index, "after_index": route_index, "keys": KEYS,
             "baseline": SimpleNamespace(operations=()), "event": None,
             "planning_context": {"mode": "INITIAL_PLAN"}, "objective_profile": "INITIAL_PLAN_NO_STABILITY",
             "classified": {key: {"state": "MOVABLE_WINDOW"} for key in KEYS},
             "_machine_res": {}, "_worker_res": {}, "boundary_facts": ()}
    prepared_at = time.perf_counter()
    regimes, _, regime_count, equivalence = generate_calendar_regimes(state, configs)
    prep_seconds = time.perf_counter() - prepared_at
    result = run_canonical_schedule({"state": state, "resource_configurations": configs, "regimes": regimes},
                                    plan=((1, None, 5),), objective_mode=False)
    acceptance = result["acceptance"]
    rows = acceptance.get("selected_schedule") or []
    by_key = {r["operation_key"]: r for r in rows}
    quantities_valid = set(by_key) == set(KEYS) and all(
        sum(end - begin for begin, end in by_key[key]["worker_segments"]) == ledger[key]["hypothetical_productive_seconds"]
        for key in KEYS)
    intervals = sorted((r["start_seconds"], r["end_seconds"]) for r in rows)
    exclusive = len(intervals) == 2 and intervals[0][1] <= intervals[1][0]
    evidence = {"classification": "SIMULATED_SOURCE_DERIVED_RESTRICTED_TWO_OPERATION_SMOKE_NOT_LIVE",
                "source_fingerprint": hashlib.sha256(source_bytes).hexdigest(),
                "snapshot_hash": snapshot["snapshot_hash"], "keys": KEYS, "quantity_ledger": ledger,
                "machine_id": MACHINE_ID, "worker_id": WORKER_ID, "line_id": LINE_ID,
                "legal_alternatives_exhausted": False, "factory_wide_schedule": False,
                "successors_in_scope": False, "fixed_partial_holds_in_scope": False,
                "calendar_regimes": regime_count, "calendar_equivalence": equivalence,
                "preparation_seconds": prep_seconds, "total_seconds": time.perf_counter() - start,
                "controller_status": acceptance["status"],
                "replay": acceptance.get("replay", {}).get("status"),
                "physical": acceptance.get("validator", {}).get("scope_acceptance", {}).get("physical_status"),
                "scope": acceptance.get("validator", {}).get("scope_acceptance", {}).get("scope_status"),
                "quantity_reconciled": quantities_valid, "shared_resource_exclusive": exclusive,
                "selected_intervals": {key: [row["start_seconds"], row["end_seconds"]] for key, row in by_key.items()}}
    target = HERE / "two_operation_smoke.json"
    if target.exists():
        prior = json.loads(target.read_text(encoding="utf-8"))
        stable = ("source_fingerprint", "snapshot_hash", "keys", "quantity_ledger",
                  "machine_id", "worker_id", "line_id", "calendar_regimes",
                  "controller_status", "replay", "physical", "scope",
                  "quantity_reconciled", "shared_resource_exclusive", "selected_intervals")
        differences = {field: {"prior": prior.get(field), "current": evidence.get(field)}
                       for field in stable if prior.get(field) != json.loads(json.dumps(evidence.get(field), default=str))}
        if differences:
            print(json.dumps({"determinism_mismatch": differences}, default=str))
            raise RuntimeError("EXISTING_SMOKE_SEMANTICS_DIFFER; refusing overwrite")
    else:
        target.write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({k: evidence[k] for k in ("keys", "calendar_regimes", "preparation_seconds",
                "total_seconds", "controller_status", "replay", "physical", "scope",
                "quantity_reconciled", "shared_resource_exclusive", "selected_intervals")}, default=str))
    return 0 if acceptance["status"] == "ACCEPTED" and quantities_valid and exclusive else 2


if __name__ == "__main__":
    raise SystemExit(main())
