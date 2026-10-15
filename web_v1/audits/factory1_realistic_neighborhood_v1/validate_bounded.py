"""Independent ledger, source eligibility, occupancy and routing validation.

This deliberately does not import the preparation adapter, CP model, replay,
or CRAFT's scoped validator. It checks the saved materialized result against
the pinned captured source and the separate simulated-hold scenario.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.machine_taxonomy import machine_type_to_operation_type
from utils.shift_rotation import batch_available_at
from web_v1.backend.integrations.craft.contracts import digest, ordered_source
from web_v1.audits.factory1_realistic_neighborhood_v1.analyze_scope import SOURCE, SOURCE_SHA, SNAPSHOT, SCENARIO

HERE = Path(__file__).resolve().parent


def validate_adaptive_trace(result, known_keys):
    """Independently reconstruct retry order, without the controller ledger."""
    keys = result.get('attempt_operation_keys', [])
    width, target = result.get('window_size'), result.get('cycle_target')
    if (not keys or len(keys) != len(set(keys)) or not set(keys) <= set(known_keys)
            or type(width) is not int or not 1 <= width <= 25
            or type(target) is not int or target < width):
        return [], ['ADAPTIVE_TRACE_POLICY_OR_IDENTITY']
    pending, accepted = [], []
    index = 0
    while index < len(keys):
        end = min(len(keys), index + width, (index // target + 1) * target)
        pending.append(tuple(keys[index:end]))
        index = end
    for row in result.get('attempt_trace', []):
        if not pending or tuple(row.get('keys', ())) != pending.pop(0):
            return [], ['ADAPTIVE_TRACE_ORDER']
        chunk = tuple(row['keys'])
        if row.get('status') == 'ACCEPTED':
            accepted.append(chunk)
        elif row.get('status') == 'REJECTED':
            if row.get('selected_schedule') or not row.get('failure_reason'):
                return [], ['ADAPTIVE_REJECTED_COMMITMENT_OR_REASON']
            if len(chunk) > 1:
                size = 2 if len(chunk) > 2 else 1
                pending[0:0] = [chunk[i:i + size] for i in range(0, len(chunk), size)]
        else:
            return [], ['ADAPTIVE_TRACE_STATUS']
    if [key for chunk in accepted for key in chunk] != list(result['selected_keys']):
        return [], ['ADAPTIVE_ACCEPTED_ACCOUNTING']
    return accepted, []


def validate_payload(source: dict, snapshot: dict, scenario: dict, result: dict,
                     source_bytes: bytes, approved_boundary: dict | None = None) -> dict:
    errors = []
    input_packet = result.get("scheduling_input")
    input_hash_verified = bool(input_packet and result.get("scheduling_input_hash") ==
        sha256(json.dumps(input_packet, sort_keys=True, default=str,
                          separators=(",", ":")).encode()).hexdigest())
    if input_packet is not None and not input_hash_verified:
        errors.append("SCHEDULING_INPUT_FINGERPRINT")
    if input_packet is not None and (input_packet.get("keys") != result.get("selected_keys") or
                                     input_packet.get("ledger") != result.get("ledger") or
                                     input_packet.get("domain") != result.get("domain")):
        errors.append("SCHEDULING_INPUT_CONTENT")
    scenario_content = {k: v for k, v in scenario.items() if k != "scenario_hash"}
    scenario_digest = sha256(json.dumps(scenario_content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if (sha256(source_bytes).hexdigest() != SOURCE_SHA or result["source_sha256"] != SOURCE_SHA or
            snapshot["input_hash"] != digest(ordered_source(source)) or
            snapshot["snapshot_hash"] != digest({k: v for k, v in snapshot.items() if k != "snapshot_hash"}) or
            result["snapshot_hash"] != snapshot["snapshot_hash"] or
            scenario["source_fingerprint"] != SOURCE_SHA or
            scenario["source_snapshot_hash"] != snapshot["snapshot_hash"] or
            scenario["scenario_hash"] != scenario_digest or
            result["scenario_hash"] != scenario["scenario_hash"] or
            result["classification"] != "SIMULATION_ONLY_BOUNDED_INCOMPLETE_DOMAIN_NOT_LIVE"):
        return {"status": "FAIL", "errors": ["SOURCE_OR_SIMULATION_IDENTITY"]}
    origin = datetime.fromisoformat(source["reference_time"])
    ops = {r["operation_key"]: r for r in snapshot["operations"]}
    routes = {f'{r["production_order_id"]}_{r["operation_id"]}': r for r in source["operations"]}
    partials = {r["operation_key"]: r for r in scenario["partial_operations"]}
    machines = {r["asset_id"]: r for r in source["resources"]["machines"]}
    lines = {r["line_id"]: r for r in source["resources"]["lines"]}
    workers = {r["employee_id"]: r for r in source["resources"]["workers"]}
    batches = {r["batch_id"]: r for r in source["resources"]["batches"]}
    memberships = {r["employee_id"]: r["batch_id"] for r in source["resources"]["batch_members"]}
    leave = {r["employee_id"] for r in source["resources"]["work_schedules"] if r["is_on_leave"]}
    machine_caps = {(r["asset_id"], r["operation_type"].lower()) for r in source["resources"]["machine_capabilities"]}
    efficiency = {(r["employee_id"], r["operation_type"].lower()): Decimal(str(r["efficiency_rating"])) / 100
                  for r in source["resources"]["skills"]}
    rows = [r for window in result["windows"] for r in window["selected_schedule"]]
    keys = result["selected_keys"]
    selected = {r["operation_key"]: r for r in rows}
    window_size = result.get('window_size', 4)
    cycle_target = result.get('cycle_target')
    if (type(window_size) is not int or window_size < 1 or window_size > 25 or
            (cycle_target is not None and
             (type(cycle_target) is not int or cycle_target < window_size))):
        errors.append('WINDOW_POLICY_INVALID')
        expected_windows = ()
    else:
        expected = []
        index = 0
        while index < len(keys):
            cycle_end = ((index // cycle_target) + 1) * cycle_target if cycle_target else len(keys)
            stop = min(index + window_size, cycle_end, len(keys))
            expected.append(tuple(keys[index:stop]))
            index = stop
        expected_windows = tuple(expected)
    if result.get('adaptive_retries'):
        expected_windows, trace_errors = validate_adaptive_trace(result, ops)
        errors.extend(trace_errors)
    if len(result["windows"]) != len(expected_windows):
        errors.append("WINDOW_COUNT")
    for index, window in enumerate(result["windows"]):
        if (index >= len(expected_windows) or tuple(window["keys"]) != expected_windows[index] or
                window["status"] != "ACCEPTED" or window["replay"] != "PASS" or
                window["physical"] != "VALID" or window["scope"] != "SCOPE_VALID" or
                {r["operation_key"] for r in window["selected_schedule"]} != set(window["keys"])):
            errors.append(f"WINDOW_ACCEPTANCE_OR_CARRY:{index+1}")
    if (len(rows) != len(set(r["operation_key"] for r in rows)) or set(selected) != set(keys) or
            set(result["accepted_keys"]) != set(keys) or result["remaining_keys"]):
        errors.append("OPERATION_IDENTITY_OR_WINDOW_ACCOUNTING")
    machine_intervals = defaultdict(list)
    worker_intervals = defaultdict(list)
    fixed_ends = defaultdict(int)
    fixed_count = 0
    approved_rows = {}
    boundary_version = result.get("domain", {}).get("outside_approved_version")
    boundary_hash = result.get("domain", {}).get("outside_approved_assignment_hash")
    if boundary_version and approved_boundary is None:
        errors.append("OUTSIDE_BOUNDARY_MISSING")
    if approved_boundary is not None:
        approved = approved_boundary.get("assignments", [])
        approved_hash = sha256(json.dumps(approved, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
        if (approved_boundary.get("classification") != "APPROVED_TEST_SCHEDULE_SNAPSHOT" or
                approved_boundary.get("status") != "ACTIVE" or
                not approved_boundary.get("active_version_id") or
                approved_boundary.get("source_sha256") != SOURCE_SHA or
                approved_boundary.get("assignments_sha256") != approved_hash or
                not approved):
            errors.append("OUTSIDE_APPROVAL_PROVENANCE")
        if (approved_boundary.get("active_version_id") != boundary_version or
                approved_boundary.get("assignments_sha256") != boundary_hash):
            errors.append("OUTSIDE_BOUNDARY_FINGERPRINT_MISMATCH")
        for outside in approved:
            outside_key = outside.get("operation_key")
            if outside_key in approved_rows or outside_key in keys or outside_key not in ops:
                errors.append(f"OUTSIDE_APPROVAL_IDENTITY:{outside_key}")
                continue
            approved_rows[outside_key] = outside
            a, b = outside.get("start_seconds"), outside.get("end_seconds")
            mid = outside.get("machine_id")
            if not all(isinstance(v, int) for v in (a, b, mid)) or a < 0 or b <= a:
                errors.append(f"OUTSIDE_APPROVAL_INTERVAL:{outside_key}")
                continue
            outside_route = routes[outside_key]
            outside_capability = machine_type_to_operation_type(
                outside_route["required_machine_type"])
            if (mid not in machines or machines[mid]["current_status"] != "Active" or
                    machines[mid]["factory_id"] != 1 or
                    (mid, outside_capability) not in machine_caps):
                errors.append(f"OUTSIDE_APPROVAL_MACHINE_ELIGIBILITY:{outside_key}")
            machine_intervals[mid].append((a, b, f"approved:{outside_key}"))
            for segment in outside.get("worker_segments", []):
                if (len(segment) != 3 or not all(isinstance(v, int) for v in segment) or
                        segment[1] < a or segment[2] > b or segment[2] <= segment[1]):
                    errors.append(f"OUTSIDE_APPROVAL_WORKER_SEGMENT:{outside_key}")
                    continue
                wid = segment[0]
                if (wid not in workers or workers[wid]["factory_id"] != 1 or
                        not workers[wid]["is_active"] or wid in leave or
                        wid not in memberships or (wid, outside_capability) not in efficiency):
                    errors.append(f"OUTSIDE_APPROVAL_WORKER_ELIGIBILITY:{outside_key}")
                else:
                    batch = batches[memberships[wid]]
                    for second in range(segment[1], segment[2], 60):
                        if not batch_available_at(batch["rotation_offset"], batch["rest_day_offset"],
                                                  origin + timedelta(seconds=second)):
                            errors.append(f"OUTSIDE_APPROVAL_OFF_SHIFT:{outside_key}")
                            break
                worker_intervals[segment[0]].append((segment[1], segment[2],
                                                     f"approved:{outside_key}"))
    for partial in scenario["partial_operations"]:
        key = partial["operation_key"]
        source_lots = {lot["lot_id"]: lot for lot in ops[key]["lots"]}
        for hold in partial["fixed_in_flight_holds"]:
            fixed_count += 1
            lot = source_lots.get(hold["lot_id"])
            if (lot is None or lot["state"] != "IN_FLIGHT" or
                    any(hold[field] != lot[field] for field in ("quantity", "machine_id", "worker_id", "line_id"))):
                errors.append(f"FIXED_HOLD_LOT_MAPPING:{key}:{hold['lot_id']}")
            a = int((datetime.fromisoformat(hold["machine_hold_from"]) - origin).total_seconds())
            b = int((datetime.fromisoformat(hold["machine_hold_until"]) - origin).total_seconds())
            fixed_ends[key] = max(fixed_ends[key], b)
            if a < 0 or b <= a:
                errors.append(f"FIXED_HOLD_TIME:{key}")
            machine_intervals[int(hold["machine_id"])].append((a, b, f"fixed:{hold['lot_id']}"))
            for left, right in hold["worker_productive_segments"]:
                wa = int((datetime.fromisoformat(left) - origin).total_seconds())
                wb = int((datetime.fromisoformat(right) - origin).total_seconds())
                if wa < a or wb > b or wb <= wa:
                    errors.append(f"FIXED_WORKER_SEGMENT:{key}")
                worker_intervals[int(hold["worker_id"])].append((wa, wb, f"fixed:{hold['lot_id']}"))
    if fixed_count != 35:
        errors.append("FIXED_HOLD_COUNT")
    for key in keys:
        if key not in selected or key not in ops:
            continue
        op, route, row = ops[key], routes[key], selected[key]
        if op["order_id"] != key.split("_")[0] or str(route["production_order_id"]) != op["order_id"]:
            errors.append(f"ORDER_IDENTITY:{key}")
        completed = sum(lot["quantity"] for lot in op["lots"] if lot["state"] == "COMPLETED")
        inflight = sum(lot["quantity"] for lot in op["lots"] if lot["state"] == "IN_FLIGHT")
        tail_lots = [lot for lot in op["lots"] if lot["state"] == "NOT_STARTED"]
        tail = sum(lot["quantity"] for lot in tail_lots)
        if completed + inflight + tail != op["quantity"]["target"]:
            errors.append(f"SOURCE_QUANTITY:{key}")
        ledger = result.get("ledger", {}).get(key)
        if (ledger is None or ledger.get("target") != op["quantity"]["target"] or
                ledger.get("completed") != completed or ledger.get("in_flight") != inflight or
                ledger.get("tail") != tail or
                set(ledger.get("source_lot_ids", ())) != {lot["lot_id"] for lot in tail_lots}):
            errors.append(f"SCHEDULED_QUANTITY_LEDGER:{key}")
        if key in partials:
            partial = partials[key]
            if (partial["accepted_completed_quantity"] != completed or
                    partial["in_flight_quantity_assumed_unproduced"] != inflight or
                    partial["unstarted_residual_quantity"] != tail or
                    set(partial["unstarted_lot_ids"]) != {lot["lot_id"] for lot in tail_lots}):
                errors.append(f"SIMULATION_LEDGER:{key}")
        cfg = row["resource_configuration"]
        if len(cfg["machine_ids"]) != 1 or len(cfg["worker_ids"]) != 1 or cfg["k"] != 1:
            errors.append(f"UNSUPPORTED_CREW:{key}")
            continue
        mid, wid, lid = int(cfg["machine_ids"][0]), int(cfg["worker_ids"][0]), int(cfg["line_id"])
        capability = machine_type_to_operation_type(route["required_machine_type"])
        if (mid not in machines or wid not in workers or wid not in memberships or
                machines[mid]["current_status"] != "Active" or machines[mid]["factory_id"] != 1 or
                machines[mid]["line_id"] != lid or lid not in lines or
                lines[lid]["factory_id"] != 1 or not lines[lid]["is_active"] or
                workers[wid]["factory_id"] != 1 or
                not workers[wid]["is_active"] or wid in leave or
                (mid, capability) not in machine_caps or (wid, capability) not in efficiency):
            errors.append(f"SOURCE_RESOURCE_ELIGIBILITY:{key}")
            continue
        expected = int((Decimal(str(route["standard_allowed_minutes"])) * tail * 60 /
                        efficiency[(wid, capability)]).to_integral_value(rounding=ROUND_CEILING))
        segments = [(int(a), int(b)) for a, b in row["worker_segments"]]
        if (cfg["productive_seconds"] != expected or
                sum(b-a for a, b in segments) != expected or cfg["setup_seconds"] != 900):
            errors.append(f"PRODUCTIVE_DURATION:{key}")
        start, end = int(row["start_seconds"]), int(row["end_seconds"])
        if start < 0 or end > 30 * 86400 or end <= start:
            errors.append(f"INTERVAL:{key}")
        machine_intervals[mid].append((start, end, key))
        batch = batches[memberships[wid]]
        for a, b in segments:
            if a < start + 900 or b > end or b <= a:
                errors.append(f"WORKER_SEGMENT_BOUNDS:{key}")
            worker_intervals[wid].append((a, b, key))
            # One-minute independent source-calendar scan. This uses the
            # captured batch parameters, not CRAFT's generated regimes.
            for second in range(a, b, 60):
                if not batch_available_at(batch["rotation_offset"], batch["rest_day_offset"],
                                          origin + timedelta(seconds=second)):
                    errors.append(f"OFF_SHIFT:{key}")
                    break
    for label, groups in (("MACHINE", machine_intervals), ("WORKER", worker_intervals)):
        for resource, intervals in groups.items():
            ordered = sorted(intervals)
            if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
                errors.append(f"{label}_OVERLAP:{resource}")
    for key in keys:
        if key not in selected:
            continue  # Identity/accounting error above already rejects partial results.
        predecessor = ops[key]["predecessor"]
        if predecessor in selected and key in selected:
            release = max(int(selected[predecessor]["end_seconds"]), fixed_ends[predecessor])
            if int(selected[key]["start_seconds"]) < release:
                errors.append(f"WHOLE_OPERATION_PRECEDENCE:{predecessor}->{key}")
        elif predecessor and predecessor not in selected and ops[predecessor]["execution_state"] != "FULLY_COMPLETED":
            if predecessor not in approved_rows:
                errors.append(f"UNRESOLVED_EXTERNAL_PREDECESSOR:{key}")
            elif int(selected[key]["start_seconds"]) < int(approved_rows[predecessor]["end_seconds"]):
                errors.append(f"EXTERNAL_PREDECESSOR_RELEASE:{predecessor}->{key}")
        successor = ops[key]["successor"]
        if successor in approved_rows and key in selected and int(selected[key]["end_seconds"]) > int(approved_rows[successor]["start_seconds"]):
            errors.append(f"EXTERNAL_SUCCESSOR_RELEASE:{key}->{successor}")
    return {"status": "PASS" if not errors else "FAIL", "errors": sorted(set(errors)),
            "checked_operations": len(selected), "fixed_holds": fixed_count,
            "machine_resources_checked": len(machine_intervals),
            "worker_resources_checked": len(worker_intervals),
            "scheduling_input_fingerprint_verified": input_hash_verified,
            "outside_approved_assignments_checked": len(approved_rows),
            "full_legal_domain_proven": False, "outside_future_conflict_closure_proven": False,
            "live_admission": False}


def validate(path: Path | None = None, approved_boundary_path: Path | None = None) -> dict:
    path = path or HERE / "bounded_result.json"
    source_bytes = SOURCE.read_bytes()
    return validate_payload(json.loads(source_bytes),
                            json.loads(SNAPSHOT.read_text(encoding="utf-8")),
                            json.loads(SCENARIO.read_text(encoding="utf-8")),
                            json.loads(path.read_text(encoding="utf-8")),
                            source_bytes,
                            json.loads(approved_boundary_path.read_text(encoding="utf-8"))
                            if approved_boundary_path is not None else None)


if __name__ == "__main__":
    candidate_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "bounded_result.json"
    boundary_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    outcome = validate(candidate_path, boundary_path)
    target = HERE / f"independent_{candidate_path.stem}.json"
    target.write_text(
        json.dumps(outcome, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(outcome))
    raise SystemExit(0 if outcome["status"] == "PASS" else 2)
