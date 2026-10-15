"""Read-only Factory-1 candidate-scope audit from pinned captured inputs.

This does not schedule, assert conflict closure, or turn future planned lot
assignments into commitments. Its output is a prerequisite for a bounded
simulation-only scheduling attempt.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.machine_taxonomy import machine_type_to_operation_type

SOURCE = ROOT / "web_v1/audits/execution_boundary_v1/factory1_source.json"
SNAPSHOT = ROOT / "web_v1/audits/execution_boundary_v1/factory1_normalized_snapshot.json"
SCENARIO = ROOT / "web_v1/audits/factory1_simulated_continuation_v1/scenario.json"
VALIDATION = ROOT / "web_v1/audits/factory1_simulated_continuation_v1/validation.json"
SOURCE_SHA = "010541c698ca6c46aee096d322e1c197598b4d6fbd8ec2bfa6bfbca0b8974438"
ORDER_IDS = ("790", "33")


def inspect() -> dict:
    source_bytes = SOURCE.read_bytes()
    if sha256(source_bytes).hexdigest() != SOURCE_SHA:
        raise RuntimeError("SOURCE_FINGERPRINT_CHANGED")
    source = json.loads(source_bytes)
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    scenario = json.loads(SCENARIO.read_text(encoding="utf-8"))
    validation = json.loads(VALIDATION.read_text(encoding="utf-8"))
    if (scenario["source_fingerprint"] != SOURCE_SHA
            or scenario["source_snapshot_hash"] != snapshot["snapshot_hash"]
            or validation["status"] != "PASS"):
        raise RuntimeError("INCONSISTENT_CAPTURE_OR_SCENARIO")
    orders = {str(row["production_order_id"]): row for row in source["orders"]}
    by_order: dict[str, list[dict]] = defaultdict(list)
    for row in snapshot["operations"]:
        by_order[row["order_id"]].append(row)
    released = sorted((order for order in orders.values()
                       if order["scheduled_start_date"] <= source["reference_time"][:10]
                       and any(row["execution_state"] != "FULLY_COMPLETED"
                               for row in by_order[str(order["production_order_id"])])),
                      key=lambda row: (row["delivery_deadline"], row["production_order_id"]))
    if tuple(str(row["production_order_id"]) for row in released[:2]) != ORDER_IDS:
        raise RuntimeError("EARLIEST_INCOMPLETE_ORDER_PREFIX_CHANGED")
    raw = {f'{row["production_order_id"]}_{row["operation_id"]}': row
           for row in source["operations"]}
    selected = sorted((row for order_id in ORDER_IDS for row in by_order[order_id]),
                      key=lambda row: (ORDER_IDS.index(row["order_id"]), row["routing_sequence"]))
    active_machines = {row["asset_id"]: row for row in source["resources"]["machines"]
                       if row["current_status"] == "Active"}
    active_workers = {row["employee_id"]: row for row in source["resources"]["workers"]
                      if row["is_active"]}
    leave = {row["employee_id"] for row in source["resources"]["work_schedules"]
             if row["is_on_leave"]}
    machine_caps: dict[str, set[int]] = defaultdict(set)
    worker_skills: dict[str, set[int]] = defaultdict(set)
    for row in source["resources"]["machine_capabilities"]:
        if row["asset_id"] in active_machines:
            machine_caps[row["operation_type"].lower()].add(row["asset_id"])
    for row in source["resources"]["skills"]:
        if row["employee_id"] in active_workers and row["employee_id"] not in leave:
            worker_skills[row["operation_type"].lower()].add(row["employee_id"])
    partial = {row["operation_key"]: row for row in scenario["partial_operations"]}
    selected_keys = {row["operation_key"] for row in selected}
    selected_caps = {row["operation_key"]: machine_type_to_operation_type(
        raw[row["operation_key"]]["required_machine_type"]) for row in selected}
    planned_machines: dict[int, set[str]] = defaultdict(set)
    planned_workers: dict[int, set[str]] = defaultdict(set)
    for row in selected:
        for lot in row["lots"]:
            if lot["state"] == "NOT_STARTED":
                planned_machines[lot["machine_id"]].add(row["order_id"])
                planned_workers[lot["worker_id"]].add(row["order_id"])
    holds = [hold for row in scenario["partial_operations"]
             for hold in row["fixed_in_flight_holds"]]
    fixed_by_selected = [hold for row in selected if row["operation_key"] in partial
                         for hold in partial[row["operation_key"]]["fixed_in_flight_holds"]]
    operation_rows = []
    for row in selected:
        key = row["operation_key"]
        cap = selected_caps[key]
        machines = machine_caps[cap]
        workers = worker_skills[cap]
        lines = {active_machines[mid]["line_id"] for mid in machines}
        operation_rows.append({
            "key": key, "order": row["order_id"], "sequence": row["routing_sequence"],
            "state": row["execution_state"], "predecessor": row["predecessor"],
            "capability": cap, "completed_quantity": row["quantity"]["completed"],
            "in_flight_quantity": row["quantity"]["in_flight"],
            "unstarted_quantity": row["quantity"]["not_started"],
            "active_eligible_machine_count": len(machines),
            "active_not_on_leave_skilled_worker_count": len(workers),
            "machine_line_count": len(lines),
            "simulated_fixed_hold_count": len(partial.get(key, {}).get("fixed_in_flight_holds", [])),
        })
    return {
        "classification": "SIMULATION_ONLY_SOURCE_DERIVED_SCOPE_AUDIT_NOT_SCHEDULE",
        "source_sha256": SOURCE_SHA, "snapshot_hash": snapshot["snapshot_hash"],
        "scenario_hash": scenario["scenario_hash"], "reference_time": source["reference_time"],
        "selection_policy": "EARLIEST_DUE_RELEASED_INCOMPLETE_ORDER_PREFIX",
        "selected_orders": list(ORDER_IDS), "selected_keys": sorted(selected_keys),
        "selected_operations": len(selected),
        "states": dict(Counter(row["execution_state"] for row in selected)),
        "selected_simulated_fixed_holds": len(fixed_by_selected),
        "factory_simulated_fixed_holds": len(holds),
        "outside_simulated_fixed_holds": len(holds) - len(fixed_by_selected),
        "shared_planned_machine_ids_across_orders": sorted(mid for mid, order_ids in planned_machines.items()
                                                     if len(order_ids) > 1),
        "shared_planned_worker_ids_across_orders": sorted(wid for wid, order_ids in planned_workers.items()
                                                    if len(order_ids) > 1),
        "outside_unstarted_or_partial_operations": sum(
            row["operation_key"] not in selected_keys and row["execution_state"] != "FULLY_COMPLETED"
            for row in snapshot["operations"]),
        "operation_rows": operation_rows,
        "future_actuals_treated_as_commitments": False,
        "resource_domain_complete": False,
        "conflict_closure_proven": False,
    }


if __name__ == "__main__":
    print(json.dumps(inspect(), indent=2, sort_keys=True))
