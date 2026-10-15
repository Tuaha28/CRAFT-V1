"""Build a reproducible *hypothetical* Factory-1 continuation overlay.

This never modifies the captured source or production admission policy.  A
standard-work estimate is not an observed finish, and this artifact is not a
CRAFT scheduling input until its separate solver-admission gate is proven.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from utils.machine_taxonomy import machine_type_to_operation_type
from utils.shift_rotation import batch_available_at
from utils.work_calendar import productive_segments

HERE = Path(__file__).resolve().parent
CAPTURE = ROOT / "web_v1/audits/execution_boundary_v1/factory1_source.json"
NORMALIZED = ROOT / "web_v1/audits/execution_boundary_v1/factory1_normalized_snapshot.json"
MANIFEST = CAPTURE.with_name("artifact_hash_manifest.json")
SCENARIO_ID = "F1_SIMULATED_CONTINUATION_STANDARD_WORK_V1"
SEED = 0  # No random draws are used; retained as part of scenario identity.


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seconds(minutes: str, quantity: int) -> int:
    return int((Decimal(str(minutes)) * Decimal(quantity) * 60).to_integral_value(rounding=ROUND_CEILING))


def _calendar(batch: dict):
    return lambda when: batch_available_at(batch["rotation_offset"], batch["rest_day_offset"], when)


def build(source: dict, snapshot: dict) -> dict:
    if source["factory_id"] != 1 or snapshot["factory_id"] != 1 or source["reference_time"] != snapshot["reference_time"]:
        raise ValueError("SOURCE_IDENTITY_MISMATCH")
    reference = datetime.fromisoformat(source["reference_time"])
    routes = {f"{r['production_order_id']}_{r['operation_id']}": r for r in source["operations"]}
    machines = {r["asset_id"]: r for r in source["resources"]["machines"]}
    workers = {r["employee_id"]: r for r in source["resources"]["workers"]}
    batches = {r["batch_id"]: r for r in source["resources"]["batches"]}
    membership = {r["employee_id"]: r["batch_id"] for r in source["resources"]["batch_members"]}
    capabilities = defaultdict(set)
    skills = defaultdict(set)
    for row in source["resources"]["machine_capabilities"]:
        capabilities[row["asset_id"]].add(row["operation_type"].strip().lower())
    for row in source["resources"]["skills"]:
        skills[row["employee_id"]].add(row["operation_type"].strip().lower())
    records = []
    violations = []
    all_keys = [r["operation_key"] for r in snapshot["operations"]]
    if len(all_keys) != 2879 or len(set(all_keys)) != 2879 or set(all_keys) != set(routes):
        raise ValueError("OPERATION_IDENTITY_MISMATCH")
    states = Counter(r["execution_state"] for r in snapshot["operations"])
    if states != {"FULLY_COMPLETED": 171, "PARTIALLY_EXECUTED": 82, "UNSTARTED": 2626}:
        raise ValueError("EXECUTION_PARTITION_CHANGED")
    for op in sorted((r for r in snapshot["operations"] if r["execution_state"] == "PARTIALLY_EXECUTED"),
                     key=lambda r: r["operation_key"]):
        key = op["operation_key"]
        route = routes[key]
        capability = machine_type_to_operation_type(route["required_machine_type"])
        if not capability:
            violations.append(f"{key}:UNKNOWN_CAPABILITY")
            continue
        quantities = op["quantity"]
        lots = sorted(op["lots"], key=lambda r: r["lot_id"])
        if (sum(r["quantity"] for r in lots) != quantities["total"] or
                quantities["completed"] + quantities["remaining"] != quantities["total"] or
                quantities["remaining"] != quantities["in_flight"] + quantities["not_started"]):
            violations.append(f"{key}:QUANTITY_MISMATCH")
            continue
        if len({r["lot_id"] for r in lots}) != len(lots):
            violations.append(f"{key}:DUPLICATE_LOT")
            continue
        fixed_holds = []
        for lot in (r for r in lots if r["state"] == "IN_FLIGHT"):
            mid, wid = lot["machine_id"], lot["worker_id"]
            machine, worker = machines.get(mid), workers.get(wid)
            batch = batches.get(membership.get(wid))
            if not machine or not worker or not batch or not machine["current_status"] == "Active" or not worker["is_active"]:
                violations.append(f"{key}:{lot['lot_id']}:RESOURCE_UNAVAILABLE")
                continue
            if machine["line_id"] != lot["line_id"] or capability not in capabilities[mid] or capability not in skills[wid]:
                violations.append(f"{key}:{lot['lot_id']}:RESOURCE_INELIGIBLE")
                continue
            # No intra-lot output is observed.  The hypothetical scenario
            # conservatively assumes all units in this in-flight lot remain.
            work = _seconds(route["standard_allowed_minutes"], lot["quantity"])
            try:
                start, end, segments = productive_segments(reference, work / 60, _calendar(batch))
            except ValueError:
                violations.append(f"{key}:{lot['lot_id']}:NO_VALID_CALENDAR_CONTINUATION")
                continue
            fixed_holds.append({
                "lot_id": lot["lot_id"], "machine_id": mid, "worker_id": wid,
                "line_id": lot["line_id"], "quantity": lot["quantity"],
                "productive_seconds": work, "machine_hold_from": reference.isoformat(),
                "machine_hold_until": end.isoformat(),
                "worker_productive_segments": [[a.isoformat(), b.isoformat()] for a, b in segments],
                "assumption": "HYPOTHETICAL_ZERO_INTRA_LOT_OUTPUT_AT_REFERENCE",
            })
        residual_lots = [r for r in lots if r["state"] == "NOT_STARTED"]
        residual_quantity = sum(r["quantity"] for r in residual_lots)
        record = {
            "operation_key": key, "order_id": op["order_id"],
            "classification": "SIMULATED_CONTINUATION_NOT_OBSERVED",
            "original_required_quantity": quantities["total"],
            "accepted_completed_quantity": quantities["completed"],
            "in_flight_quantity_assumed_unproduced": quantities["in_flight"],
            "unstarted_residual_quantity": residual_quantity,
            "completed_lot_ids": sorted(r["lot_id"] for r in lots if r["state"] == "COMPLETED"),
            "unstarted_lot_ids": sorted(r["lot_id"] for r in residual_lots),
            "fixed_in_flight_holds": fixed_holds,
            "hypothetical_unstarted_serial_productive_seconds": _seconds(route["standard_allowed_minutes"], residual_quantity),
            "predecessor": op["predecessor"], "successor": op["successor"],
            "whole_operation_successor_release_not_before": max(
                (r["machine_hold_until"] for r in fixed_holds), default=None),
            "standard_allowed_minutes_per_unit": str(route["standard_allowed_minutes"]),
            "remaining_work_source": "ROUTING_STANDARD_WORK_MODEL_NOT_MEASURED_DURATION",
            "simulation_assumptions": [
                "Nominal 100% standard-work efficiency, one serial productive work budget per lot.",
                "All in-flight lot units are counted as remaining; no intra-lot completion is asserted.",
                "In-flight worker follows recorded batch rotation; machine held across calendar pauses.",
                "Unstarted lots are hypothetically transferable only if later CRAFT admission proves it.",
            ],
            "craft_input_allowed": False,
        }
        records.append(record)
    held_machines = Counter(h["machine_id"] for r in records for h in r["fixed_in_flight_holds"])
    held_workers = Counter(h["worker_id"] for r in records for h in r["fixed_in_flight_holds"])
    for resource, counts in (("MACHINE", held_machines), ("WORKER", held_workers)):
        violations.extend(f"DUPLICATE_{resource}_HOLD:{key}" for key, count in counts.items() if count > 1)
    if len(records) != 82 or sum(len(r["fixed_in_flight_holds"]) for r in records) != 35:
        violations.append("PARTIAL_OR_IN_FLIGHT_IDENTITY_MISMATCH")
    return {
        "scenario_id": SCENARIO_ID, "seed": SEED, "simulation_only": True,
        "production_admission_enabled": False, "craft_input_allowed": False,
        "factory_id": 1, "reference_time": source["reference_time"],
        "source_snapshot_hash": snapshot["snapshot_hash"],
        "operation_state_counts": dict(sorted(states.items())),
        "identity_count": len(all_keys), "partial_operations": records,
        "consistency_errors": sorted(violations),
    }


def main() -> int:
    expected = json.loads(MANIFEST.read_text(encoding="utf-8"))[CAPTURE.name]
    if _sha(CAPTURE) != expected:
        raise RuntimeError("SOURCE_FINGERPRINT_CHANGED")
    source = json.loads(CAPTURE.read_text(encoding="utf-8"))
    snapshot = json.loads(NORMALIZED.read_text(encoding="utf-8"))
    scenario = build(source, snapshot)
    scenario["source_fingerprint"] = expected
    scenario["scenario_hash"] = hashlib.sha256(json.dumps(scenario, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    target = HERE / "scenario.json"
    if target.exists():
        if json.loads(target.read_text(encoding="utf-8")) != scenario:
            raise RuntimeError("EXISTING_SCENARIO_DIFFERS; refusing to overwrite")
    else:
        target.write_text(json.dumps(scenario, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    identity = {state: sorted(r["operation_key"] for r in snapshot["operations"]
                              if r["execution_state"] == state)
                for state in ("FULLY_COMPLETED", "PARTIALLY_EXECUTED", "UNSTARTED")}
    if sum(map(len, identity.values())) != 2879 or len(set().union(*map(set, identity.values()))) != 2879:
        raise RuntimeError("SCENARIO_IDENTITY_ACCOUNTING_FAILED")
    ledger = {"scenario_id": SCENARIO_ID, "source_snapshot_hash": snapshot["snapshot_hash"],
              "operation_keys_by_state": identity,
              "newly_scheduled_operation_keys": [], "validly_deferred_operation_keys": [],
              "unresolved_operation_keys": [],
              "note": "Source partition only; no factory-wide scheduling result is asserted."}
    ledger_path = HERE / "identity_ledger.json"
    if ledger_path.exists() and json.loads(ledger_path.read_text(encoding="utf-8")) != ledger:
        raise RuntimeError("EXISTING_IDENTITY_LEDGER_DIFFERS")
    if not ledger_path.exists():
        ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"scenario_hash": scenario["scenario_hash"], "partial_operations": len(scenario["partial_operations"]),
                      "fixed_lot_holds": sum(len(r["fixed_in_flight_holds"]) for r in scenario["partial_operations"]),
                      "consistency_errors": scenario["consistency_errors"]}))
    return 0 if not scenario["consistency_errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
