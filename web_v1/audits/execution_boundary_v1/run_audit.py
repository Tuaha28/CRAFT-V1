"""Capture, normalize and independently validate Factory-1 boundary evidence.

No solver invocation. Output is isolated from every frozen research artifact.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "web_v1/backend"))

from integrations.craft.boundary_validator import validate_snapshot
from integrations.craft.contracts import BoundaryUnresolved, canonical_json, digest
from integrations.craft.legacy_repository import LegacyExecutionRepository
from integrations.craft.snapshot_adapter import craft_boundary_payload, normalize_snapshot
from integrations.craft.residual_tail import assess_residual_tails

AUDITED = ("40_902", "40_903", "40_905", "212_2584", "212_2586", "411_2598", "411_2599", "979_952", "212_2585")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(name, payload):
    (OUT / name).write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main():
    from dotenv import dotenv_values

    old_root = ROOT / "web_v1/audits/production_platform_v2"
    preflight = json.loads((old_root / "database_and_boundary_evidence.json").read_text())
    for name, expected in json.loads((old_root / "artifact_hash_manifest.json").read_text()).items():
        if sha(old_root / name) != expected:
            raise RuntimeError(f"Prior audit hash mismatch: {name}")
    protected = {name: sha(ROOT / name) for name in preflight["source_hashes_after"]}
    if protected != preflight["source_hashes_after"]:
        raise RuntimeError("Audited source changed since preflight; review before proceeding")
    frozen_root = ROOT / "outputs/crafter/factory1_craft_workload_v1"
    frozen_hashes = {str(p.relative_to(ROOT)): sha(p) for p in frozen_root.rglob("*") if p.is_file()}
    if "--from-capture" in sys.argv:
        recorded_manifest = json.loads((OUT / "artifact_hash_manifest.json").read_text(encoding="utf-8"))
        if sha(OUT / "factory1_source.json") != recorded_manifest.get("factory1_source.json"):
            raise RuntimeError("Captured source hash mismatch; refusing offline replay")
        source = json.loads((OUT / "factory1_source.json").read_text(encoding="utf-8"))
    else:
        source = LegacyExecutionRepository.from_config({**dotenv_values(ROOT / ".env"), **os.environ}).read_factory(1)
    if len(source["orders"]) != 336 or len(source["operations"]) != 2879:
        raise RuntimeError("Factory-1 historical identity differs from preflight")
    if "--from-capture" not in sys.argv:
        write("factory1_source.json", source)
    snapshot = normalize_snapshot(source)
    # Reverse every table to demonstrate that retrieval ordering is not identity.
    reordered = json.loads(canonical_json(source))
    for name in ("orders", "operations", "lots", "wip"):
        reordered[name].reverse()
    for rows in reordered["resources"].values():
        rows.reverse()
    deterministic = normalize_snapshot(reordered)["snapshot_hash"] == snapshot["snapshot_hash"]
    validation = validate_snapshot(snapshot, source)
    tails = assess_residual_tails(snapshot)
    repeated_tails = assess_residual_tails(normalize_snapshot(reordered))
    assert tails == repeated_tails, "Residual boundary assessment is not deterministic"
    write("residual_tail_assessment.json", tails)
    write("factory1_normalized_snapshot.json", snapshot)
    write("boundary_validation.json", validation)
    old_state = {r["operation_key"]: r for r in json.loads((frozen_root / "state_operations.json").read_text())["operations"]}
    old_opt = {r["operation_key"] for r in json.loads((frozen_root / "optimizable_operations.json").read_text())["operations"]}
    old_running = {r["operation_key"] for r in json.loads((frozen_root / "running_boundary_operations.json").read_text())["operations"]}
    comparisons = []
    by_key = {op["operation_key"]: op for op in snapshot["operations"]}
    for op in snapshot["operations"]:
        key = op["operation_key"]
        previous = ("RUNNING_FIXED" if key in old_running else "OPTIMIZABLE" if key in old_opt else
                    "EXCLUDED_ORDER_790" if op["order_id"] == "790" else "COMPLETED_HISTORICAL_OUTSIDE_STATE")
        differences = []
        if key in old_opt and not op["optimizable"]:
            differences.append("Started or partially completed work withheld from whole-operation optimization")
        if previous == "RUNNING_FIXED" and op["boundary_status"] == "BOUNDARY_UNRESOLVED":
            differences.append("Running-only reservations and aggregate projected end do not prove a complete committed tail")
        if op["order_id"] == "790":
            differences.append("Live full-factory snapshot retains order 790; no research-only exclusion applied")
        if previous == "COMPLETED_HISTORICAL_OUTSIDE_STATE":
            differences.append("Retained as immutable historical precedence evidence, never optimization work")
        comparisons.append({"operation_key": key, "previous_classification": previous,
                            "corrected_classification": op["execution_state"], "boundary_status": op["boundary_status"],
                            "previous_optimizable": key in old_opt, "optimizable": op["optimizable"], "differences": differences})
    comparison = {"frozen_state_count": len(old_state), "frozen_optimizable_count": len(old_opt),
                  "new_historical_operations": len(snapshot["operations"]),
                  "new_state_counts": dict(Counter(op["execution_state"] for op in snapshot["operations"])),
                  "new_optimizable_by_state_count": sum(op["optimizable"] for op in snapshot["operations"]),
                  "currently_safe_to_pass_to_craft": validation["valid_for_craft"],
                  "all_operation_comparisons": comparisons}
    comparison["same_frozen_membership_comparison"] = {
        "operations_present": sum(op["operation_key"] in old_state for op in snapshot["operations"]),
        "missing": sorted(set(old_state) - set(by_key)),
        "corrected_unstarted": sum(op["operation_key"] in old_state and op["execution_state"] == "UNSTARTED" for op in snapshot["operations"]),
        "corrected_partial": sum(op["operation_key"] in old_state and op["execution_state"] == "PARTIALLY_EXECUTED" for op in snapshot["operations"]),
        "note": "Comparison only: the live snapshot itself retains full historical membership including order 790",
    }
    write("frozen_workload_comparison.json", comparison)
    previous_by_key = {row["operation_key"]: row for row in comparisons}
    audited = [{**by_key[key], "previous_classification": previous_by_key[key]["previous_classification"],
                "successor_safety": "WITHHELD_UNTIL_COMPLETE_RESIDUAL_BOUNDARY_IS_PROVEN",
                "remaining_work_handling": "All residual lot IDs and quantities retained; no generated tail promoted to a commitment"}
               for key in AUDITED]
    write("audited_operations.json", audited)
    if validation["valid_for_craft"]:
        smoke = {"status": "BOUNDARY_PAYLOAD_READY_ONLY", "payload": craft_boundary_payload(snapshot, source)}
    else:
        try:
            craft_boundary_payload(snapshot, source)
            raise AssertionError("Invalid snapshot passed CRAFT admission")
        except BoundaryUnresolved:
            smoke = {"status": "NOT_RUN_BOUNDARY_UNRESOLVED", "handoff_rejection_verified": True,
                     "scheduler_imported": any(m.startswith("crafter") for m in sys.modules), "solver_invoked": False}
    write("craft_preparation_gate.json", smoke)
    after = {name: sha(ROOT / name) for name in protected}
    frozen_after = {name: sha(ROOT / name) for name in frozen_hashes}
    summary = {
        "execution_boundary_contract": "IMPLEMENTED_FAIL_CLOSED", "factory_snapshot_validation": validation["status"],
        "reference_time": snapshot["reference_time"], "dataset": snapshot["dataset"],
        "input_hash": snapshot["input_hash"], "snapshot_hash": snapshot["snapshot_hash"], "deterministic_snapshot": deterministic,
        "operation_state_counts": comparison["new_state_counts"], "lot_state_counts": dict(Counter(lot["state"] for op in snapshot["operations"] for lot in op["lots"])),
        "optimizable_by_execution_state": comparison["new_optimizable_by_state_count"],
        "formerly_optimizable_now_protected": [r["operation_key"] for r in comparisons if r["previous_optimizable"] and not r["optimizable"]],
        "boundary_error_counts": validation["error_counts"],
        "audited_nine_classified_as_partial": all(by_key[k]["execution_state"] == "PARTIALLY_EXECUTED" for k in AUDITED),
        "audited_nine_non_optimizable": all(not by_key[k]["optimizable"] for k in AUDITED),
        "audited_nine_committed_tails_resolved": all(by_key[k]["boundary_status"] != "BOUNDARY_UNRESOLVED" for k in AUDITED),
        "small_craft_prep_smoke": smoke["status"], "database_writes": 0,
        "audited_sources_unchanged": after == protected, "frozen_workload_unchanged": frozen_after == frozen_hashes,
        "safe_to_resume_v2_step_2": validation["valid_for_craft"],
        "residual_tail_counts": {k: tails[k] for k in ("partial_operations_total", "resolved_committed_boundary", "resolved_splittable_residual", "boundary_unresolved")},
        "residual_assessment_deterministic": tails == repeated_tails,
    }
    write("report.json", summary)
    write("protected_hashes.json", {"before": {**protected, **frozen_hashes}, "after": {**after, **frozen_after}})
    write("implementation_hashes.json", {str(p.relative_to(ROOT)): sha(p)
          for p in sorted((ROOT / "web_v1/backend/integrations/craft").rglob("*.py"))})
    manifest = {p.name: sha(p) for p in sorted(OUT.glob("*")) if p.is_file() and p.name not in {"artifact_hash_manifest.json", "hash_verification.json"}}
    write("artifact_hash_manifest.json", manifest)
    write("hash_verification.json", {"all_hashes_verified": all(sha(OUT / k) == v for k, v in manifest.items()),
                                     "audited_sources_unchanged": after == protected, "frozen_workload_unchanged": frozen_after == frozen_hashes})
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
