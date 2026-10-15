"""Independent source-ledger and resource check for the hypothetical overlay."""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from utils.machine_taxonomy import machine_type_to_operation_type
from utils.shift_rotation import batch_available_at
from web_v1.backend.integrations.craft.boundary_validator import validate_snapshot

HERE = Path(__file__).resolve().parent
SOURCE = ROOT / "web_v1/audits/execution_boundary_v1/factory1_source.json"
SNAPSHOT = ROOT / "web_v1/audits/execution_boundary_v1/factory1_normalized_snapshot.json"


def validate(source: dict, snapshot: dict, scenario: dict) -> dict:
    errors = []
    content = {key: value for key, value in scenario.items() if key != "scenario_hash"}
    calculated_hash = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if scenario.get("scenario_hash") != calculated_hash:
        errors.append("SCENARIO_FINGERPRINT_MISMATCH")
    if scenario.get("simulation_only") is not True or scenario.get("production_admission_enabled") is not False or scenario.get("craft_input_allowed") is not False:
        errors.append("SIMULATION_ISOLATION_BROKEN")
    if (scenario.get("reference_time") != source["reference_time"] or
            scenario.get("source_snapshot_hash") != snapshot["snapshot_hash"]):
        errors.append("SOURCE_IDENTITY_MISMATCH")
    source_keys = {f"{r['production_order_id']}_{r['operation_id']}" for r in source["operations"]}
    snapshot_by_key = {r["operation_key"]: r for r in snapshot["operations"]}
    partial_keys = {k for k, r in snapshot_by_key.items() if r["execution_state"] == "PARTIALLY_EXECUTED"}
    records = scenario.get("partial_operations", [])
    actual_keys = [r["operation_key"] for r in records]
    if (len(source_keys) != 2879 or len(snapshot_by_key) != 2879 or
            len(records) != 82 or set(actual_keys) != partial_keys or len(set(actual_keys)) != len(actual_keys)):
        errors.append("OPERATION_IDENTITY_CONSERVATION")
    routes = {f"{r['production_order_id']}_{r['operation_id']}": r for r in source["operations"]}
    machines = {r["asset_id"]: r for r in source["resources"]["machines"]}
    workers = {r["employee_id"]: r for r in source["resources"]["workers"]}
    batches = {r["batch_id"]: r for r in source["resources"]["batches"]}
    members = {r["employee_id"]: r["batch_id"] for r in source["resources"]["batch_members"]}
    caps, skills = defaultdict(set), defaultdict(set)
    for row in source["resources"]["machine_capabilities"]:
        caps[row["asset_id"]].add(row["operation_type"].lower())
    for row in source["resources"]["skills"]:
        skills[row["employee_id"]].add(row["operation_type"].lower())
    reference = datetime.fromisoformat(source["reference_time"])
    seen_holds, machine_holds, worker_holds = set(), Counter(), Counter()
    for row in records:
        key = row["operation_key"]
        if key not in snapshot_by_key or key not in routes:
            errors.append(f"{key}:UNKNOWN_OPERATION")
            continue
        op, route = snapshot_by_key[key], routes[key]
        lots = op["lots"]
        completed = {r["lot_id"]: r for r in lots if r["state"] == "COMPLETED"}
        inflight = {r["lot_id"]: r for r in lots if r["state"] == "IN_FLIGHT"}
        unstarted = {r["lot_id"]: r for r in lots if r["state"] == "NOT_STARTED"}
        expected = {
            "original_required_quantity": sum(r["quantity"] for r in lots),
            "accepted_completed_quantity": sum(r["quantity"] for r in completed.values()),
            "in_flight_quantity_assumed_unproduced": sum(r["quantity"] for r in inflight.values()),
            "unstarted_residual_quantity": sum(r["quantity"] for r in unstarted.values()),
        }
        if any(row.get(field) != value for field, value in expected.items()):
            errors.append(f"{key}:QUANTITY_LEDGER")
        if (set(row.get("completed_lot_ids", [])) != set(completed) or
                set(row.get("unstarted_lot_ids", [])) != set(unstarted)):
            errors.append(f"{key}:LOT_IDENTITY")
        smv = Decimal(str(route["standard_allowed_minutes"]))
        residual_seconds = int((smv * sum(r["quantity"] for r in unstarted.values()) * 60).to_integral_value(rounding=ROUND_CEILING))
        if row.get("hypothetical_unstarted_serial_productive_seconds") != residual_seconds:
            errors.append(f"{key}:RESIDUAL_STANDARD_WORK")
        holds = row.get("fixed_in_flight_holds", [])
        if {h["lot_id"] for h in holds} != set(inflight) or len(holds) != len(inflight):
            errors.append(f"{key}:IN_FLIGHT_IDENTITY")
        for hold in holds:
            lot_id = hold["lot_id"]
            if lot_id not in inflight:
                continue
            lot = inflight[lot_id]
            mid, wid = hold["machine_id"], hold["worker_id"]
            capability = machine_type_to_operation_type(route["required_machine_type"])
            if ((key, lot_id) in seen_holds or mid != lot["machine_id"] or wid != lot["worker_id"] or
                    hold["line_id"] != lot["line_id"] or hold["quantity"] != lot["quantity"]):
                errors.append(f"{key}:{lot_id}:HOLD_IDENTITY")
            seen_holds.add((key, lot_id))
            machine_holds[mid] += 1
            worker_holds[wid] += 1
            if (mid not in machines or wid not in workers or wid not in members or
                    machines[mid]["line_id"] != lot["line_id"] or
                    machines[mid]["current_status"] != "Active" or not workers[wid]["is_active"] or
                    capability not in caps[mid] or capability not in skills[wid]):
                errors.append(f"{key}:{lot_id}:RESOURCE_ELIGIBILITY")
                continue
            batch = batches[members[wid]]
            segments = [(datetime.fromisoformat(a), datetime.fromisoformat(b))
                        for a, b in hold["worker_productive_segments"]]
            if not segments or any(a >= b or a < reference for a, b in segments):
                errors.append(f"{key}:{lot_id}:SEGMENT_BOUNDS")
                continue
            if any(left[1] > right[0] for left, right in zip(segments, segments[1:])):
                errors.append(f"{key}:{lot_id}:SEGMENT_OVERLAP")
            # Both segment endpoints and every included hour must belong to
            # the recorded batch calendar; this is independent of the builder.
            for a, b in segments:
                probe = a
                while probe < b:
                    if not batch_available_at(batch["rotation_offset"], batch["rest_day_offset"], probe):
                        errors.append(f"{key}:{lot_id}:OFF_SHIFT_WORK")
                        break
                    probe = min(b, probe.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
            work_seconds = int((smv * lot["quantity"] * 60).to_integral_value(rounding=ROUND_CEILING))
            if (sum(int((b-a).total_seconds()) for a, b in segments) != work_seconds or
                    hold["productive_seconds"] != work_seconds or
                    hold["machine_hold_from"] != reference.isoformat() or
                    hold["machine_hold_until"] != segments[-1][1].isoformat()):
                errors.append(f"{key}:{lot_id}:WORK_OR_OCCUPANCY")
        if holds and row.get("whole_operation_successor_release_not_before") != max(h["machine_hold_until"] for h in holds):
            errors.append(f"{key}:PRECEDENCE_BOUNDARY")
    if any(v > 1 for v in machine_holds.values()):
        errors.append("MACHINE_DOUBLE_BOOKING")
    if any(v > 1 for v in worker_holds.values()):
        errors.append("WORKER_DOUBLE_BOOKING")
    production = validate_snapshot(snapshot, source)
    if production["valid_for_craft"] or production["error_counts"] != {"BOUNDARY_UNRESOLVED": 82}:
        errors.append("PRODUCTION_ADMISSION_WEAKENED")
    return {"status": "PASS" if not errors else "FAIL", "errors": sorted(set(errors)),
            "operation_count": len(source_keys), "partial_count": len(records),
            "hold_count": len(seen_holds), "production_admission": production["status"],
            "production_blocked_count": production["error_counts"].get("BOUNDARY_UNRESOLVED", 0)}


def main() -> int:
    manifest = json.loads(SOURCE.with_name("artifact_hash_manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != manifest[SOURCE.name]:
        raise RuntimeError("SOURCE_FINGERPRINT_CHANGED")
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    scenario = json.loads((HERE / "scenario.json").read_text(encoding="utf-8"))
    result = validate(source, snapshot, scenario)
    target = HERE / "validation.json"
    if target.exists() and json.loads(target.read_text(encoding="utf-8")) != result:
        raise RuntimeError("EXISTING_VALIDATION_DIFFERS")
    if not target.exists():
        target.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
