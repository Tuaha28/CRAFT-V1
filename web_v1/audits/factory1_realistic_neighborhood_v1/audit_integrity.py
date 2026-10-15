"""Independent pinned-input, domain and external-boundary accounting."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_CEILING
from hashlib import sha256
import json
from pathlib import Path

from web_v1.backend.integrations.craft.contracts import digest, ordered_source
from utils.machine_taxonomy import machine_type_to_operation_type
from .analyze_scope import SOURCE, SOURCE_SHA, SNAPSHOT, SCENARIO, inspect
from .validate_bounded import HERE, validate


def audit(path: Path) -> dict:
    raw = SOURCE.read_bytes()
    source = json.loads(raw)
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    scenario = json.loads(SCENARIO.read_text(encoding="utf-8"))
    result = json.loads(path.read_text(encoding="utf-8"))
    independent = validate(path)
    scenario_body = {k: v for k, v in scenario.items() if k != "scenario_hash"}
    source_sha = sha256(raw).hexdigest()
    normalized_input_hash = digest(ordered_source(source))
    normalized_content_hash = digest({k: v for k, v in snapshot.items() if k != "snapshot_hash"})
    scenario_hash = sha256(json.dumps(scenario_body, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
    input_packet = result.get("scheduling_input") or {}
    input_hash = sha256(json.dumps(input_packet, sort_keys=True, default=str,
                                   separators=(",", ":")).encode()).hexdigest()
    chain = {
        "raw_source_sha256": source_sha,
        "normalized_input_semantic_hash": normalized_input_hash,
        "normalized_snapshot_hash": normalized_content_hash,
        "scenario_hash": scenario_hash,
        "scheduling_input_hash": input_hash,
        "verified": (source_sha == SOURCE_SHA == result["source_sha256"] == scenario["source_fingerprint"]
                     and normalized_input_hash == snapshot["input_hash"]
                     and normalized_content_hash == snapshot["snapshot_hash"] == scenario["source_snapshot_hash"]
                     and scenario_hash == scenario["scenario_hash"] == result["scenario_hash"]
                     and input_hash == result.get("scheduling_input_hash")),
    }
    rows = {op["operation_key"]: op for op in snapshot["operations"]}
    routes = {f'{op["production_order_id"]}_{op["operation_id"]}': op for op in source["operations"]}
    machines = {m["asset_id"]: m for m in source["resources"]["machines"]}
    workers = {w["employee_id"]: w for w in source["resources"]["workers"]}
    lines = {line["line_id"]: line for line in source["resources"]["lines"]}
    leave = {r["employee_id"] for r in source["resources"]["work_schedules"] if r["is_on_leave"]}
    caps = {(r["asset_id"], r["operation_type"].lower()) for r in source["resources"]["machine_capabilities"]}
    skills = {(r["employee_id"], r["operation_type"].lower()): Decimal(str(r["efficiency_rating"])) / 100
              for r in source["resources"]["skills"]}
    keys = result["selected_keys"]
    generated_errors = []
    domain_rows = []
    selected_machines, selected_workers = set(), set()
    for key in keys:
        capability = machine_type_to_operation_type(routes[key]["required_machine_type"])
        tail = rows[key]["quantity"]["not_started"]
        generated = input_packet.get("configs", {}).get(key, [])
        signatures = set()
        for cfg in generated:
            mid, wid, lid = cfg["machine_ids"][0], int(cfg["worker_ids"][0]), cfg["line_id"]
            signature = (mid, wid, lid)
            if signature in signatures:
                generated_errors.append(f"DUPLICATE_CONFIG:{key}:{signature}")
            signatures.add(signature)
            selected_machines.add(mid)
            selected_workers.add(wid)
            expected = (int((Decimal(str(routes[key]["standard_allowed_minutes"])) * tail * 60 /
                             skills[(wid, capability)]).to_integral_value(rounding=ROUND_CEILING))
                        if (wid, capability) in skills else None)
            if (mid not in machines or wid not in workers or lid not in lines or
                    machines[mid]["current_status"] != "Active" or machines[mid]["factory_id"] != 1 or
                    machines[mid]["line_id"] != lid or lines[lid]["factory_id"] != 1 or
                    not lines[lid]["is_active"] or workers[wid]["factory_id"] != 1 or
                    not workers[wid]["is_active"] or wid in leave or
                    (mid, capability) not in caps or expected != cfg["productive_seconds"]):
                generated_errors.append(f"INVALID_GENERATED_CONFIG:{key}:{signature}")
        domain_rows.append({"operation_key": key, "capability": capability,
                            "required_crew_size": 1,
                            "source_capable_active_machines": result["domain"]["legal_machine_counts"][key],
                            "source_skilled_active_not_on_leave_workers": result["domain"]["legal_worker_counts"][key],
                            "source_capability_pair_upper_bound": (result["domain"]["legal_machine_counts"][key] *
                                                                    result["domain"]["legal_worker_counts"][key]),
                            "generated_configurations": len(generated),
                            "generated_machine_ids": sorted({cfg["machine_ids"][0] for cfg in generated}),
                            "generated_worker_ids": sorted({int(cfg["worker_ids"][0]) for cfg in generated}),
                            "excluded_machine_count_by_candidate_limit": max(
                                0, result["domain"]["legal_machine_counts"][key] -
                                len({cfg["machine_ids"][0] for cfg in generated})),
                            "excluded_worker_count_by_candidate_limit": max(
                                0, result["domain"]["legal_worker_counts"][key] -
                                len({cfg["worker_ids"][0] for cfg in generated})),
                            "expansion_available": True,
                            "truncation_reason": "BOUNDED_AUDIT_CANDIDATE_LIMIT",
                            "calendar_and_reservation_complete": False,
                            "legal_domain_complete": False})
    selected_set = set(keys)
    outside = [op for op in snapshot["operations"]
               if op["operation_key"] not in selected_set and op["execution_state"] != "FULLY_COMPLETED"]
    outside_planned_machine = set()
    outside_planned_worker = set()
    for op in outside:
        for lot in op["lots"]:
            if lot["state"] != "NOT_STARTED":
                continue
            if lot["machine_id"] in selected_machines:
                outside_planned_machine.add(op["operation_key"])
            if lot["worker_id"] in selected_workers:
                outside_planned_worker.add(op["operation_key"])
    selected_orders = {rows[key]["order_id"] for key in keys}
    route_external_predecessors = sorted({rows[key]["predecessor"] for key in keys
                                          if rows[key]["predecessor"] and rows[key]["predecessor"] not in selected_set})
    route_external_successors = sorted({rows[key]["successor"] for key in keys
                                        if rows[key]["successor"] and rows[key]["successor"] not in selected_set})
    complete_order_routes = all({op["operation_key"] for op in snapshot["operations"] if op["order_id"] == order}
                                <= selected_set for order in selected_orders)
    holds = [(partial["operation_key"], hold) for partial in scenario["partial_operations"]
             for hold in partial["fixed_in_flight_holds"]]
    return {
        "status": "PASS_PHYSICAL_PARTIAL_SCOPE" if chain["verified"] and independent["status"] == "PASS"
                  and not generated_errors else "FAIL",
        "fingerprint_chain": chain, "independent_candidate_validation": independent,
        "selected_operation_count": len(keys), "selected_order_ids": sorted(selected_orders),
        "complete_order_routes": complete_order_routes,
        "route_external_predecessors": route_external_predecessors,
        "route_external_successors": route_external_successors,
        "all_simulated_fixed_holds_enforced_in_runner": len(holds),
        "outside_simulated_fixed_holds": sum(key not in selected_set for key, _ in holds),
        "outside_uncommitted_or_partial_operations": len(outside),
        "outside_operations_with_source_planned_lots_on_selected_machines": len(outside_planned_machine),
        "outside_operations_with_source_planned_lots_on_selected_workers": len(outside_planned_worker),
        "source_planned_lots_are_not_fixed_commitments": True,
        "outside_future_conflict_closure_proven": False,
        "selected_machine_ids": sorted(selected_machines),
        "selected_worker_ids": sorted(selected_workers),
        "resource_domain": domain_rows, "generated_configuration_errors": generated_errors,
        "full_legal_resource_domain_proven": False,
        "selection_policy": inspect()["selection_policy"],
        "live_production_admission": False,
    }


if __name__ == "__main__":
    path = Path(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else HERE / "bounded_16_full_evidence_v2.json"
    result = audit(path)
    target = HERE / f"integrity_{path.stem}.json"
    target.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("status", "selected_operation_count", "complete_order_routes",
                                           "outside_uncommitted_or_partial_operations",
                                           "outside_operations_with_source_planned_lots_on_selected_machines",
                                           "outside_operations_with_source_planned_lots_on_selected_workers",
                                           "full_legal_resource_domain_proven")}))
    raise SystemExit(0 if result["status"] == "PASS_PHYSICAL_PARTIAL_SCOPE" else 2)
