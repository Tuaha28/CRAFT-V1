"""Independent source-ledger/physical check for the saved partial-chain candidate.

Does not invoke CRAFT preparation, CP-SAT, or its replay/validators. Source lots,
simulated holds, and the materialized candidate are checked separately.
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.machine_taxonomy import machine_type_to_operation_type
from utils.shift_rotation import batch_available_at

HERE = Path(__file__).resolve().parent
KEYS = ("665_2677", "665_2678", "665_2679")


def validate(source: dict, snapshot: dict, scenario: dict, candidate: dict) -> dict:
    errors: list[str] = []
    source_hash = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # Captured source fingerprint is based on original file bytes; caller also
    # checks that byte fingerprint. Here we bind the candidate to the scenario.
    if (candidate.get("classification") != "SIMULATION_ONLY_NOT_LIVE" or
            candidate.get("scenario_hash") != scenario.get("scenario_hash") or
            candidate.get("operation_keys") != list(KEYS)):
        errors.append("IDENTITY_OR_SIMULATION_GATE")
    by_key = {r["operation_key"]: r for r in snapshot["operations"]}
    routes = {f'{r["production_order_id"]}_{r["operation_id"]}': r for r in source["operations"]}
    partials = {r["operation_key"]: r for r in scenario["partial_operations"]}
    machines = {r["asset_id"]: r for r in source["resources"]["machines"]}
    workers = {r["employee_id"]: r for r in source["resources"]["workers"]}
    members = {r["employee_id"]: r["batch_id"] for r in source["resources"]["batch_members"]}
    batches = {r["batch_id"]: r for r in source["resources"]["batches"]}
    machine_caps = {(r["asset_id"], r["operation_type"].lower()) for r in source["resources"]["machine_capabilities"]}
    skills = {(r["employee_id"], r["operation_type"].lower()) for r in source["resources"]["skills"]}
    origin = datetime.fromisoformat(source["reference_time"])
    rows = candidate.get("selected_schedule", [])
    if len(rows) != len(KEYS) or {r.get("operation_key") for r in rows} != set(KEYS):
        errors.append("CANDIDATE_OPERATION_IDENTITY")
        return {"status": "FAIL", "errors": errors}
    selected = {r["operation_key"]: r for r in rows}
    machine_intervals: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    worker_intervals: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    for key in KEYS:
        op, route, row = by_key[key], routes[key], selected[key]
        completed = sum(l["quantity"] for l in op["lots"] if l["state"] == "COMPLETED")
        in_flight = sum(l["quantity"] for l in op["lots"] if l["state"] == "IN_FLIGHT")
        unstarted = sum(l["quantity"] for l in op["lots"] if l["state"] == "NOT_STARTED")
        if completed + in_flight + unstarted != op["quantity"]["target"]:
            errors.append(f"{key}:SOURCE_QUANTITY")
        if key in partials:
            p = partials[key]
            if (p["accepted_completed_quantity"] != completed or
                    p["in_flight_quantity_assumed_unproduced"] != in_flight or
                    p["unstarted_residual_quantity"] != unstarted):
                errors.append(f"{key}:SCENARIO_QUANTITY")
        expected_work = int((Decimal(str(route["standard_allowed_minutes"])) * unstarted * 60).to_integral_value(rounding=ROUND_CEILING))
        config = row["resource_configuration"]
        if (config["productive_seconds"] != expected_work or
                sum(b-a for a, b in row["worker_segments"]) != expected_work or
                config["setup_seconds"] != 900):
            errors.append(f"{key}:PRODUCTIVE_WORK")
        capability = machine_type_to_operation_type(route["required_machine_type"])
        if len(config["machine_ids"]) != 1 or len(config["worker_ids"]) != 1:
            errors.append(f"{key}:UNSUPPORTED_CREW")
            continue
        mid, wid, lid = int(config["machine_ids"][0]), int(config["worker_ids"][0]), int(config["line_id"])
        if (mid not in machines or wid not in workers or wid not in members or
                machines[mid]["line_id"] != lid or machines[mid]["current_status"] != "Active" or
                not workers[wid]["is_active"] or (mid, capability) not in machine_caps or
                (wid, capability) not in skills):
            errors.append(f"{key}:RESOURCE_ELIGIBILITY")
            continue
        start, end = int(row["start_seconds"]), int(row["end_seconds"])
        if start < 0 or end <= start:
            errors.append(f"{key}:INTERVAL")
        machine_intervals[mid].append((start, end, key))
        for a, b in row["worker_segments"]:
            if a < start or b > end or b <= a:
                errors.append(f"{key}:WORKER_SEGMENT")
            worker_intervals[wid].append((a, b, key))
            probe = origin + timedelta(seconds=a)
            finish = origin + timedelta(seconds=b)
            batch = batches[members[wid]]
            while probe < finish:
                if not batch_available_at(batch["rotation_offset"], batch["rest_day_offset"], probe):
                    errors.append(f"{key}:OFF_SHIFT_WORK")
                    break
                probe = min(finish, probe.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    holds = partials["665_2678"]["fixed_in_flight_holds"]
    lot_by_id = {lot["lot_id"]: lot for lot in by_key["665_2678"]["lots"]}
    if len(holds) != 2 or {h["lot_id"] for h in holds} != {l["lot_id"] for l in lot_by_id.values() if l["state"] == "IN_FLIGHT"}:
        errors.append("FIXED_HOLD_IDENTITY")
    latest_hold_end = 0
    for hold in holds:
        lot = lot_by_id[hold["lot_id"]]
        if (hold["quantity"] != lot["quantity"] or hold["machine_id"] != lot["machine_id"] or
                hold["worker_id"] != lot["worker_id"] or hold["line_id"] != lot["line_id"]):
            errors.append("FIXED_HOLD_SOURCE_MAPPING")
        a = int((datetime.fromisoformat(hold["machine_hold_from"]) - origin).total_seconds())
        b = int((datetime.fromisoformat(hold["machine_hold_until"]) - origin).total_seconds())
        latest_hold_end = max(latest_hold_end, b)
        machine_intervals[int(hold["machine_id"])].append((a, b, "fixed:"+hold["lot_id"]))
        for left, right in hold["worker_productive_segments"]:
            wa = int((datetime.fromisoformat(left) - origin).total_seconds())
            wb = int((datetime.fromisoformat(right) - origin).total_seconds())
            worker_intervals[int(hold["worker_id"])].append((wa, wb, "fixed:"+hold["lot_id"]))
    for kind, groups in (("MACHINE", machine_intervals), ("WORKER", worker_intervals)):
        for resource, intervals in groups.items():
            ordered = sorted(intervals)
            if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
                errors.append(f"{kind}_OVERLAP:{resource}")
    if (selected["665_2677"]["end_seconds"] > selected["665_2678"]["start_seconds"] or
            max(selected["665_2678"]["end_seconds"], latest_hold_end) > selected["665_2679"]["start_seconds"]):
        errors.append("FUTURE_WHOLE_OPERATION_PRECEDENCE")
    return {"status": "PASS" if not errors else "FAIL", "errors": sorted(set(errors)),
            "checked_operations": len(KEYS), "fixed_holds": len(holds),
            "machine_resources": len(machine_intervals), "worker_resources": len(worker_intervals),
            "source_semantic_hash": source_hash}


def main() -> int:
    source_path = ROOT / "web_v1/audits/execution_boundary_v1/factory1_source.json"
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    snapshot = json.loads((ROOT / "web_v1/audits/execution_boundary_v1/factory1_normalized_snapshot.json").read_text(encoding="utf-8"))
    scenario = json.loads((ROOT / "web_v1/audits/factory1_simulated_continuation_v1/scenario.json").read_text(encoding="utf-8"))
    candidate = json.loads((HERE / "partial_chain_candidate.json").read_text(encoding="utf-8"))
    if hashlib.sha256(source_bytes).hexdigest() != scenario["source_fingerprint"] or candidate["source_fingerprint"] != scenario["source_fingerprint"]:
        raise RuntimeError("SOURCE_FINGERPRINT_CHANGED")
    result = validate(source, snapshot, scenario, candidate)
    target = HERE / "partial_chain_independent_validation.json"
    if target.exists() and json.loads(target.read_text(encoding="utf-8")) != result:
        raise RuntimeError("EXISTING_INDEPENDENT_VALIDATION_DIFFERS")
    if not target.exists():
        target.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
