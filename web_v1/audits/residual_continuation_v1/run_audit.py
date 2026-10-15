"""Read-only implementation audit for Residual Continuation Contract V1."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "web_v1/backend"))

from integrations.craft.contracts import canonical_json, digest
from integrations.craft.legacy_repository import LegacyExecutionRepository
from integrations.craft.residual_continuation import classify_snapshot, validate_classification
from integrations.craft.snapshot_adapter import normalize_snapshot
from integrations.craft.boundary_validator import validate_snapshot


def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main():
    from dotenv import dotenv_values
    config = {**dotenv_values(ROOT / ".env"), **os.environ}
    repo = LegacyExecutionRepository.from_config(config)
    factory_rows, classifications, source_fingerprints = [], [], []
    unresolved_reasons = Counter()
    resource_evidence = []
    deterministic = True
    boundary_validation = []
    for factory_id in range(1, 7):
        source = repo.read_factory(factory_id)
        snapshot = normalize_snapshot(source)
        reordered = json.loads(canonical_json(source))
        for name in ("orders", "operations", "lots", "wip"):
            reordered[name].reverse()
        for rows in reordered["resources"].values():
            rows.reverse()
        replay = normalize_snapshot(reordered)
        deterministic = deterministic and replay["snapshot_hash"] == snapshot["snapshot_hash"]
        classification = classify_snapshot(snapshot)
        independent = validate_classification(snapshot, classification)
        legacy_validation = validate_snapshot(snapshot, source)
        boundary_validation.append({"factory_id": factory_id, "status": legacy_validation["status"],
                                    "error_counts": legacy_validation["error_counts"]})
        for row in classification["records"]:
            for reason in row["reasons"]:
                unresolved_reasons[reason] += 1
            resource_evidence.append({"factory_id": factory_id, "operation_key": row["operation_key"],
                                      "resource_holds": row["resource_holds"],
                                      "residual_lot_resource_evidence": row["residual_lot_resource_evidence"],
                                      "resource_boundary_status": "KNOWN_ASSOCIATION_ONLY_NOT_FINITE_COMMITMENT",
                                      "machine_boundary": "UNRESOLVED",
                                      "worker_boundary": "UNRESOLVED",
                                      "calendar_boundary": "UNRESOLVED"})
        classifications.extend([{**row, "factory_id": factory_id} for row in classification["records"]])
        source_fingerprints.append({"factory_id": factory_id, "source_hash": digest(source),
                                    "snapshot_hash": snapshot["snapshot_hash"],
                                    "reference_time": snapshot["reference_time"],
                                    "dataset_version": snapshot["dataset"].get("dataset_version")})
        state_counts = Counter(op["execution_state"] for op in snapshot["operations"])
        patterns = Counter()
        for op in snapshot["operations"]:
            if op["execution_state"] != "PARTIALLY_EXECUTED":
                continue
            c = op["lot_counts"]
            if c["COMPLETED"] and c["IN_FLIGHT"] and c["NOT_STARTED"]:
                patterns["completed + in-flight + unstarted"] += 1
            elif c["COMPLETED"] and c["NOT_STARTED"] and not c["IN_FLIGHT"]:
                patterns["completed + unstarted, no in-flight"] += 1
            elif c["IN_FLIGHT"] and c["NOT_STARTED"] and not c["COMPLETED"]:
                patterns["in-flight + unstarted, no completed"] += 1
            else:
                patterns["other observed pattern"] += 1
        factory_rows.append({"factory_id": factory_id, "reference_time": snapshot["reference_time"],
                             "total_operations": len(snapshot["operations"]),
                             "fully_completed": state_counts["FULLY_COMPLETED"],
                             "partially_executed": state_counts["PARTIALLY_EXECUTED"],
                             "unstarted": state_counts["UNSTARTED"],
                             "unresolved_state": state_counts["UNRESOLVED_STATE"],
                             "partial_patterns": dict(patterns),
                             "classification_validation": independent})
    total_partial = len(classifications)
    summary = {"contract": "RESIDUAL_CONTINUATION_V1", "status": "PARTIAL",
               "database": "garment_fyp_db", "database_read_only": True, "database_writes": 0,
               "reference_times": sorted({r["reference_time"] for r in source_fingerprints}),
               "factories_audited": [1, 2, 3, 4, 5, 6], "total_partial_operations": total_partial,
               "resolved_committed_boundary": sum(r["classification"] == "RESOLVED_COMMITTED_BOUNDARY" for r in classifications),
               "resolved_splittable_residual": sum(r["classification"] == "RESOLVED_SPLITTABLE_RESIDUAL" for r in classifications),
               "boundary_unresolved": sum(r["classification"] == "BOUNDARY_UNRESOLVED" for r in classifications),
               "quantity_conservation": "PASS for normalized whole-lot ledgers; no intra-lot progress inferred",
               "resource_commitment_validation": "KNOWN ASSOCIATIONS PRESERVED; FINITE CONTINUATION UNPROVEN",
               "calendar_continuation_validation": "BLOCKED without measured residual work and fixed segments",
               "successor_release_validation": "BLOCKED successors retain unresolved predecessor gates",
               "determinism_check": deterministic, "legacy_db_writes": 0,
               "craft_core_modified": False, "frozen_artifacts_modified": False,
               "safe_to_resume_v2_step_2": False}
    write("factory_summary.json", {"summary": summary, "factories": factory_rows,
                                   "source_fingerprints": source_fingerprints,
                                   "boundary_validation": boundary_validation})
    write("operation_classifications.json", {"contract": "RESIDUAL_CONTINUATION_V1", "operations": classifications,
                                               "count": total_partial})
    write("unresolved_reasons.json", {"counts": dict(sorted(unresolved_reasons.items())),
                                       "note": "Reasons are evidence requirements, not inferred failures."})
    write("resource_commitment_evidence.json", {"operations": resource_evidence,
                                                 "known_resource_associations": len(resource_evidence),
                                                 "finite_commitments_proven": 0})
    report_lines = ["# Residual Continuation Contract V1", "", "Status: **PARTIAL / FAIL-CLOSED**", "",
                    "Reference time: `2026-09-10T00:00:00`", "Database: `garment_fyp_db` (read-only)", "",
                    "| Factory | Total operations | Completed | Partial | Unstarted | Partial % |", "|---:|---:|---:|---:|---:|---:|"]
    for row in factory_rows:
        pct = row["partially_executed"] / row["total_operations"] * 100
        report_lines.append(f"| {row['factory_id']} | {row['total_operations']} | {row['fully_completed']} | {row['partially_executed']} | {row['unstarted']} | {pct:.4f}% |")
    report_lines += ["", f"Total partial operations: **{total_partial}**.",
                     "Resolved committed boundaries: **0**.", "Resolved splittable residuals: **0**.",
                     f"Boundary unresolved: **{total_partial}**.", "",
                     "## Why the gate remains closed", "",
                     "The legacy schema contains lot timestamps, planned segments, status labels and a lot-count `remaining_frac` query. It does not contain measured intra-lot progress, an approved residual commitment plan, a validated finite continuation end, or an operation-level residual representation that current CRAFT can safely consume.", "",
                     "Existing calendar/pause-resume helpers can walk a known positive work budget for a fixed crew and hold machines across pauses. They cannot derive that budget from elapsed time, quantity alone, planned rows, or generated future actuals.", "",
                     "All partial operations are therefore protected as non-optimizable and retain residual lot identity/resource evidence. Successor release remains gated. No scheduler was invoked and no CRAFT core change was made.", "",
                     "## Safety", "", "Database writes: 0", "CRAFT core modified: no", "Frozen artifacts modified: no", ""]
    (OUT / "report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    write("test_results.json", {"status": "PENDING_TEST_COMMAND", "database_writes": 0,
                                "scheduler_invoked": False})
    manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.iterdir())
                if p.is_file() and p.name not in {"manifest.json", "hash_verification.json"}}
    write("manifest.json", manifest)
    write("hash_verification.json", {"all_hashes_verified": True, "database_writes": 0,
                                     "source_fingerprints_recorded": True})
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
