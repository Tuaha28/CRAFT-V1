"""Simulation-only CRAFT proof for a partial/fixed-hold/whole-successor route.

This is a restricted legal resource domain for order 665, NOT full-factory
admission. Completed history is accounted in the ledger but never scheduled.
Existing in-flight holds are hypothetical scenario commitments; new residual
execution must follow the upstream residual and release the unstarted successor.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from copy import deepcopy

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import pymysql
pymysql.connect = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("LEGACY_CONNECTION_FORBIDDEN"))

from crafter.contracts import LineSpec, MachineSpec, OperationSpec, OrderSpec, SchedulingProblem
from crafter.repair.calendar_regimes import generate_calendar_regimes
from crafter.repair.resource_configs import ResourceConfiguration
from crafter.repair.solve import run_canonical_schedule
from utils.machine_taxonomy import machine_type_to_operation_type
from web_v1.audits.fyp_completion_20261010.validate_partial_chain import validate as validate_assembled

HERE = Path(__file__).resolve().parent
KEYS = ("665_2677", "665_2678", "665_2679")
SELECTED = {"665_2677": (450, 7369, 53), "665_2678": (415, 1112, 124),
            "665_2679": (2808, 7929, 8)}


def seconds(origin: datetime, value: str) -> int:
    return int((datetime.fromisoformat(value) - origin).total_seconds())


def run_two_windows(state, configs, source, snapshot, scenario):
    """Carry accepted first-window resource and routing boundaries into second."""
    first_state = {**state, "keys": (KEYS[0],),
                   "classified": {**state["classified"], KEYS[1]: {"state": "FUTURE_OUTSIDE_WINDOW"},
                                  KEYS[2]: {"state": "FUTURE_OUTSIDE_WINDOW"}}}
    first_regimes, _, first_count, _ = generate_calendar_regimes(first_state, {KEYS[0]: configs[KEYS[0]]})
    first = run_canonical_schedule({"state": first_state, "resource_configurations": {KEYS[0]: configs[KEYS[0]]},
                                    "regimes": first_regimes}, plan=((1, None, 5),), objective_mode=False)["acceptance"]
    report = {"classification": "SIMULATED_RESTRICTED_TWO_WINDOW_NOT_LIVE",
              "source_fingerprint": scenario["source_fingerprint"], "scenario_hash": scenario["scenario_hash"],
              "windows_attempted": 1, "window_1": {"status": first["status"], "calendar_regimes": first_count,
              "replay": first.get("replay", {}).get("status"),
              "physical": first.get("validator", {}).get("scope_acceptance", {}).get("physical_status"),
              "scope": first.get("validator", {}).get("scope_acceptance", {}).get("scope_status")}}
    if first["status"] != "ACCEPTED" or len(first.get("selected_schedule") or []) != 1:
        return report
    prior = first["selected_schedule"][0]
    if prior["operation_key"] != KEYS[0]:
        report["error"] = "FIRST_WINDOW_IDENTITY"
        return report
    machine_res = deepcopy(state["_machine_res"])
    worker_res = deepcopy(state["_worker_res"])
    facts = list(state["boundary_facts"])
    for mid in prior["resource_configuration"]["machine_ids"]:
        machine_res.setdefault(mid, []).append((prior["start_seconds"], prior["end_seconds"], "665", KEYS[0]))
        facts.append({"machine_id": mid, "start_seconds": prior["start_seconds"],
                      "end_seconds": prior["end_seconds"], "operation_key": KEYS[0]})
    for wid in prior["resource_configuration"]["worker_ids"]:
        for a, b in prior["worker_segments"]:
            worker_res.setdefault(str(wid), []).append((state["problem"].origin + timedelta(seconds=a),
                                                         state["problem"].origin + timedelta(seconds=b)))
            facts.append({"worker_id": str(wid), "start_seconds": a, "end_seconds": b,
                          "operation_key": KEYS[0]})
    second_state = {**state, "keys": KEYS[1:], "_machine_res": machine_res,
                    "_worker_res": worker_res, "boundary_facts": tuple(facts),
                    "planning_context": {"mode": "ROLLING_PLAN"},
                    "classified": {**state["classified"], KEYS[0]: {"state": "COMMITTED_FUTURE"}},
                    "committed_routing_completion": {KEYS[0]: {"authoritative_end_seconds": prior["end_seconds"]}}}
    second_configs = {key: configs[key] for key in KEYS[1:]}
    second_regimes, _, second_count, _ = generate_calendar_regimes(second_state, second_configs)
    second = run_canonical_schedule({"state": second_state, "resource_configurations": second_configs,
                                     "regimes": second_regimes}, plan=((1, None, 5),), objective_mode=False)["acceptance"]
    report["windows_attempted"] = 2
    report["window_2"] = {"status": second["status"], "calendar_regimes": second_count,
                           "replay": second.get("replay", {}).get("status"),
                           "physical": second.get("validator", {}).get("scope_acceptance", {}).get("physical_status"),
                           "scope": second.get("validator", {}).get("scope_acceptance", {}).get("scope_status")}
    if second["status"] == "ACCEPTED":
        assembled = [prior] + list(second.get("selected_schedule") or [])
        candidate = {"classification": "SIMULATION_ONLY_NOT_LIVE", "source_fingerprint": scenario["source_fingerprint"],
                     "scenario_hash": scenario["scenario_hash"], "operation_keys": list(KEYS),
                     "selected_schedule": assembled}
        report["assembled_independent_validation"] = validate_assembled(source, snapshot, scenario, candidate)
        report["assembled_selected_intervals"] = {r["operation_key"]: [r["start_seconds"], r["end_seconds"]] for r in assembled}
        report["assembled_source_quantity_conserved"] = report["assembled_independent_validation"]["status"] == "PASS"
    return report


def main() -> int:
    started = time.perf_counter()
    source_path = ROOT / "web_v1/audits/execution_boundary_v1/factory1_source.json"
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    snapshot = json.loads((ROOT / "web_v1/audits/execution_boundary_v1/factory1_normalized_snapshot.json").read_text(encoding="utf-8"))
    scenario = json.loads((ROOT / "web_v1/audits/factory1_simulated_continuation_v1/scenario.json").read_text(encoding="utf-8"))
    validation = json.loads((ROOT / "web_v1/audits/factory1_simulated_continuation_v1/validation.json").read_text(encoding="utf-8"))
    if (hashlib.sha256(source_bytes).hexdigest() != scenario["source_fingerprint"] or
            validation.get("status") != "PASS"):
        raise RuntimeError("SIMULATION_SOURCE_OR_VALIDATION_CHANGED")
    origin = datetime.fromisoformat(source["reference_time"])
    by_key = {r["operation_key"]: r for r in snapshot["operations"]}
    by_partial = {r["operation_key"]: r for r in scenario["partial_operations"]}
    machines = {r["asset_id"]: r for r in source["resources"]["machines"]}
    workers = {r["employee_id"]: r for r in source["resources"]["workers"]}
    memberships = {r["employee_id"]: r for r in source["resources"]["batch_members"]}
    batches = {r["batch_id"]: r for r in source["resources"]["batches"]}
    machine_caps = {(r["asset_id"], r["operation_type"].lower()) for r in source["resources"]["machine_capabilities"]}
    worker_skills = {(r["employee_id"], r["operation_type"].lower()) for r in source["resources"]["skills"]}
    lines = {r["line_id"]: r for r in source["resources"]["lines"]}
    if by_key["665_2676"]["execution_state"] != "FULLY_COMPLETED":
        raise RuntimeError("PREDECESSOR_NOT_COMPLETED")
    operations, configs, ledger = [], {}, {}
    by_factory, by_line, by_skill, line_factory, crew_of, efficiency_of = {}, {}, {}, {}, {}, {}
    for key in KEYS:
        row = by_key[key]
        route = next(r for r in source["operations"] if f'{r["production_order_id"]}_{r["operation_id"]}' == key)
        capability = machine_type_to_operation_type(route["required_machine_type"])
        mid, wid, lid = SELECTED[key]
        if (row["release_time"] > source["reference_time"] or row["predecessor"] not in ("665_2676", *KEYS) or
                (mid, capability) not in machine_caps or (wid, capability) not in worker_skills or
                machines[mid]["current_status"] != "Active" or machines[mid]["line_id"] != lid or
                not workers[wid]["is_active"] or lines[lid]["factory_id"] != 1 or
                not any(lot["state"] == "NOT_STARTED" and (lot["machine_id"], lot["worker_id"], lot["line_id"]) == (mid, wid, lid)
                        for lot in row["lots"])):
            raise RuntimeError(f"UNSUPPORTED_RESOURCE_OR_ROUTE:{key}")
        partial = by_partial.get(key)
        if key == "665_2678" and len(partial["fixed_in_flight_holds"]) != 2:
            raise RuntimeError("EXPECTED_TWO_FIXED_HOLDS")
        if partial:
            duration = partial["hypothetical_unstarted_serial_productive_seconds"]
            units = partial["unstarted_residual_quantity"]
        else:
            units = row["quantity"]["not_started"]
            duration = int((Decimal(str(route["standard_allowed_minutes"])) * units * 60).to_integral_value())
        if row["quantity"]["completed"] + row["quantity"]["in_flight"] + units != row["quantity"]["target"]:
            raise RuntimeError(f"QUANTITY_ACCOUNTING:{key}")
        operations.append(OperationSpec(key, "665", str(route["operation_id"]), row["routing_sequence"],
                                        capability, (lid,), duration, 900, 1, 1))
        configs[key] = (ResourceConfiguration(key, 1, lid, 1, (mid,), (str(wid),), 900, duration,
                                              ("SIMULATION_ONLY_RESTRICTED_LEGAL_DOMAIN",), duration / 60),)
        ledger[key] = {"required": row["quantity"]["target"], "completed": row["quantity"]["completed"],
                       "fixed_in_flight": row["quantity"]["in_flight"], "scheduled_tail": units,
                       "scheduled_productive_seconds": duration}
        by_factory.setdefault((1, capability), []).append(wid)
        by_line.setdefault((lid, capability), []).append(wid)
        by_skill.setdefault(capability, []).append(wid)
        line_factory[lid] = 1
        batch = batches[memberships[wid]["batch_id"]]
        crew_of[wid] = (batch["rotation_offset"], batch["rest_day_offset"])
        efficiency_of[(wid, capability)] = 1.0
    hold_rows = by_partial["665_2678"]["fixed_in_flight_holds"]
    machine_res, worker_res, facts = {}, {}, []
    for hold in hold_rows:
        a, b = seconds(origin, hold["machine_hold_from"]), seconds(origin, hold["machine_hold_until"])
        if a < 0 or b <= a or hold["line_id"] != machines[hold["machine_id"]]["line_id"]:
            raise RuntimeError("INVALID_FIXED_HOLD")
        machine_res.setdefault(hold["machine_id"], []).append((a, b, "665", f'fixed-lot-{hold["lot_id"]}'))
        facts.append({"machine_id": hold["machine_id"], "start_seconds": a, "end_seconds": b,
                      "operation_key": "665_2678"})
        for left, right in hold["worker_productive_segments"]:
            wa, wb = datetime.fromisoformat(left), datetime.fromisoformat(right)
            if wa < origin or wb > datetime.fromisoformat(hold["machine_hold_until"]) or wb <= wa:
                raise RuntimeError("INVALID_FIXED_WORKER_SEGMENT")
            worker_res.setdefault(str(hold["worker_id"]), []).append((wa, wb))
            facts.append({"worker_id": str(hold["worker_id"]), "start_seconds": seconds(origin, left),
                          "end_seconds": seconds(origin, right), "operation_key": "665_2678"})
    machine_ids = set(mid for mid, _, _ in SELECTED.values()) | {h["machine_id"] for h in hold_rows}
    line_ids = set(lid for _, _, lid in SELECTED.values()) | {h["line_id"] for h in hold_rows}
    problem = SchedulingProblem("F1-SIMULATED-PARTIAL-CHAIN-NOT-PRODUCTION", origin, 30 * 86400,
                                tuple(LineSpec(lid, 1) for lid in sorted(line_ids)),
                                tuple(MachineSpec(mid, 1, machines[mid]["line_id"],
                                    tuple(sorted(cap for asset, cap in machine_caps if asset == mid))) for mid in sorted(machine_ids)),
                                (OrderSpec("665", 1, 0, None),), tuple(operations), ())
    predecessors = {"665_2677": None, "665_2678": "665_2677", "665_2679": "665_2678"}
    successors = {"665_2677": "665_2678", "665_2678": "665_2679", "665_2679": None}
    route_index = SimpleNamespace(rows=(), by_key={key: {"start_minutes": 0} for key in KEYS},
                                  route_predecessor=predecessors, route_successor=successors)
    state = {"problem": problem, "operator_index": {"by_factory": by_factory, "by_line": by_line,
             "by_skill": by_skill, "line_factory": line_factory, "crew_of": crew_of,
             "shift_of": {}, "efficiency_of": efficiency_of}, "after_index": route_index,
             "keys": KEYS, "baseline": SimpleNamespace(operations=()), "event": None,
             "planning_context": {"mode": "INITIAL_PLAN"}, "objective_profile": "INITIAL_PLAN_NO_STABILITY",
             "classified": {key: {"state": "MOVABLE_WINDOW"} for key in KEYS},
             "_machine_res": machine_res, "_worker_res": worker_res, "boundary_facts": tuple(facts)}
    prepared_at = time.perf_counter()
    regimes, _, count, equivalence = generate_calendar_regimes(state, configs)
    prep_seconds = time.perf_counter() - prepared_at
    result = run_canonical_schedule({"state": state, "resource_configurations": configs, "regimes": regimes},
                                    plan=((1, None, 5),), objective_mode=False)
    acceptance = result["acceptance"]
    rows = {r["operation_key"]: r for r in acceptance.get("selected_schedule") or []}
    work_ok = set(rows) == set(KEYS) and all(
        sum(b-a for a, b in rows[key]["worker_segments"]) == ledger[key]["scheduled_productive_seconds"] for key in KEYS)
    precedence_ok = bool(work_ok and rows["665_2677"]["end_seconds"] <= rows["665_2678"]["start_seconds"] and
                         rows["665_2678"]["end_seconds"] <= rows["665_2679"]["start_seconds"] and
                         rows["665_2679"]["start_seconds"] >= max(seconds(origin, h["machine_hold_until"]) for h in hold_rows))
    evidence = {"source_fingerprint": hashlib.sha256(source_bytes).hexdigest(),
                "snapshot_hash": snapshot["snapshot_hash"], "scenario_hash": scenario["scenario_hash"],
                "classification": "SIMULATION_ONLY_RESTRICTED_PARTIAL_CHAIN_NOT_LIVE",
                "operation_keys": KEYS, "quantity_ledger": ledger, "fixed_hold_count": len(hold_rows),
                "fixed_boundary_facts": facts, "candidate_resources": SELECTED,
                "calendar_regimes": count, "calendar_equivalence_mismatches": equivalence["mismatches"],
                "preparation_seconds": prep_seconds, "total_seconds": time.perf_counter() - started,
                "controller_status": acceptance["status"], "replay": acceptance.get("replay", {}).get("status"),
                "physical": acceptance.get("validator", {}).get("scope_acceptance", {}).get("physical_status"),
                "scope": acceptance.get("validator", {}).get("scope_acceptance", {}).get("scope_status"),
                "scheduled_work_reconciled": work_ok, "future_precedence_valid": precedence_ok,
                "selected_intervals": {key: [r["start_seconds"], r["end_seconds"]] for key, r in rows.items()},
                "successor_665_2680_outside_scope": True, "full_factory_admission": False}
    # Application invocations own an isolated output directory. A fresh solve
    # must pass physical validation, not byte-match an older evidence envelope.
    # Standalone historical reproduction retains its fail-closed comparisons.
    artifact_dir = Path(sys.argv[1]).resolve().parent if len(sys.argv) > 1 else HERE
    candidate_path = artifact_dir / "partial_chain_candidate.json"
    candidate_payload = {"classification": "SIMULATION_ONLY_NOT_LIVE", "source_fingerprint": evidence["source_fingerprint"],
                         "scenario_hash": evidence["scenario_hash"], "operation_keys": KEYS,
                         "selected_schedule": list(acceptance.get("selected_schedule") or [])}
    normalized_candidate = json.loads(json.dumps(candidate_payload, sort_keys=True, default=str))
    if candidate_path.exists():
        if json.loads(candidate_path.read_text(encoding="utf-8")) != normalized_candidate:
            raise RuntimeError("EXISTING_PARTIAL_CHAIN_CANDIDATE_DIFFERS")
    else:
        candidate_path.write_text(json.dumps(normalized_candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    two_window = run_two_windows(state, configs, source, snapshot, scenario)
    # Keep the first diagnostic artifact intact; V2 corrects only the
    # in-memory tuple/list evidence-envelope mismatch before independent check.
    two_window_path = artifact_dir / "two_window_partial_chain_v2.json"
    if two_window_path.exists() and json.loads(two_window_path.read_text(encoding="utf-8")) != json.loads(json.dumps(two_window, default=str)):
        raise RuntimeError("EXISTING_TWO_WINDOW_EVIDENCE_DIFFERS")
    if not two_window_path.exists():
        two_window_path.write_text(json.dumps(two_window, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    target = artifact_dir / "partial_chain_smoke.json"
    if target.exists():
        prior = json.loads(target.read_text(encoding="utf-8"))
        stable = ("source_fingerprint", "snapshot_hash", "scenario_hash", "operation_keys", "quantity_ledger",
                  "fixed_hold_count", "fixed_boundary_facts", "candidate_resources", "calendar_regimes",
                  "controller_status", "replay", "physical", "scope", "scheduled_work_reconciled",
                  "future_precedence_valid", "selected_intervals")
        if any(prior.get(k) != json.loads(json.dumps(evidence.get(k), default=str)) for k in stable):
            raise RuntimeError("EXISTING_PARTIAL_CHAIN_EVIDENCE_DIFFERS")
    else:
        target.write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    if len(sys.argv) > 1:
        independent = validate_assembled(source, snapshot, scenario, normalized_candidate)
        worker_result = {
            "source": "SIMULATED_FACTORY1_PARTIAL_CHAIN",
            "controller_status": acceptance["status"], "acceptance": acceptance,
            "quantity": {"status": independent["status"], "checked_operations": independent.get("checked_operations"),
                         "fixed_holds": independent.get("fixed_holds"), "errors": independent.get("errors", [])},
            "multi_window": two_window,
            "ledger": {"source_evidence": {"as_of": source["reference_time"],
                                             "source_fingerprint": evidence["source_fingerprint"],
                                             "scenario_hash": scenario["scenario_hash"],
                                             "classification": "SIMULATED_CONTINUATION_NOT_LIVE"}},
        }
        Path(sys.argv[1]).write_text(json.dumps(worker_result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({k: evidence[k] for k in ("operation_keys", "fixed_hold_count", "calendar_regimes",
         "preparation_seconds", "total_seconds", "controller_status", "replay", "physical", "scope",
         "scheduled_work_reconciled", "future_precedence_valid", "selected_intervals")}, default=str))
    print(json.dumps({"two_window": {k: two_window.get(k) for k in ("windows_attempted", "window_1", "window_2",
          "assembled_independent_validation", "assembled_selected_intervals")}}, default=str))
    windows_valid = (two_window.get("window_1", {}).get("status") == "ACCEPTED" and
                     two_window.get("window_2", {}).get("status") == "ACCEPTED" and
                     two_window.get("assembled_independent_validation", {}).get("status") == "PASS" and
                     all(two_window.get(f"window_{i}", {}).get("replay") == "PASS" and
                         two_window.get(f"window_{i}", {}).get("physical") == "VALID" and
                         two_window.get(f"window_{i}", {}).get("scope") == "SCOPE_VALID" for i in (1, 2)))
    return 0 if acceptance["status"] == "ACCEPTED" and work_ok and precedence_ok and windows_valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
