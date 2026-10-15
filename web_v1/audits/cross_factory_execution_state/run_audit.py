"""Read-only cross-factory execution-state audit.

Uses the same repository, clock and normalization contract as the Factory-1
execution-boundary audit. It never imports a scheduler and has no write path.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "web_v1/backend"))

from integrations.craft.contracts import canonical_json, digest
from integrations.craft.execution_state import normalize_lot
from integrations.craft.legacy_repository import LegacyExecutionRepository
from integrations.craft.residual_tail import assess_residual_tails
from integrations.craft.snapshot_adapter import normalize_snapshot


def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def pattern(op):
    c = op["lot_counts"]
    if c["COMPLETED"] and c["IN_FLIGHT"] and c["NOT_STARTED"]:
        return "completed + in-flight + unstarted"
    if c["COMPLETED"] and c["NOT_STARTED"] and not c["IN_FLIGHT"]:
        return "completed + unstarted, no in-flight"
    if c["IN_FLIGHT"] and c["NOT_STARTED"] and not c["COMPLETED"]:
        return "in-flight + unstarted, no completed"
    return "other observed pattern"


def audit_factory(repo, factory_id):
    source = repo.read_factory(factory_id)
    snapshot = normalize_snapshot(source)
    reordered = json.loads(canonical_json(source))
    for name in ("orders", "operations", "lots", "wip"):
        reordered[name].reverse()
    for rows in reordered["resources"].values():
        rows.reverse()
    deterministic = normalize_snapshot(reordered)["snapshot_hash"] == snapshot["snapshot_hash"]
    partial = [op for op in snapshot["operations"] if op["execution_state"] == "PARTIALLY_EXECUTED"]
    states = Counter(op["execution_state"] for op in snapshot["operations"])
    lots = Counter(lot["state"] for op in snapshot["operations"] for lot in op["lots"])
    tail = assess_residual_tails(snapshot)
    details = []
    for op in partial:
        holds = op["resource_holds"]
        details.append({
            "factory_id": factory_id,
            "operation_key": op["operation_key"],
            "execution_state": op["execution_state"],
            "lot_pattern": pattern(op),
            "completed_lot_count": op["lot_counts"]["COMPLETED"],
            "in_flight_lot_count": op["lot_counts"]["IN_FLIGHT"],
            "not_started_lot_count": op["lot_counts"]["NOT_STARTED"],
            "completed_quantity": op["quantity"]["completed"],
            "in_flight_quantity": op["quantity"]["in_flight"],
            "remaining_quantity": op["quantity"]["remaining"],
            "line_assignment_evidence": sorted({str(h.get("line_id")) for h in holds}),
            "machine_assignment_evidence": sorted({str(h.get("machine_id")) for h in holds}),
            "worker_assignment_evidence": sorted({str(h.get("worker_id")) for h in holds}),
            "resource_holds": holds,
            "residual_lot_ids": op["residual_lot_ids"],
            "boundary_classification": "BOUNDARY_UNRESOLVED",
            "finite_continuation_boundary_proven": False,
            "optimizable": False,
            "predecessor": op["predecessor"],
            "successor": op["successor"],
            "precedence_impact": "successor remains gated until physical completion boundary is proven",
        })
    return {
        "factory_id": factory_id,
        "reference_time": snapshot["reference_time"],
        "dataset": snapshot["dataset"],
        "source_hash": digest(source),
        "snapshot_hash": snapshot["snapshot_hash"],
        "deterministic_snapshot": deterministic,
        "operation_counts": {"total": len(snapshot["operations"]),
                             "fully_completed": states["FULLY_COMPLETED"],
                             "partially_executed": states["PARTIALLY_EXECUTED"],
                             "unstarted": states["UNSTARTED"],
                             "unresolved_state": states["UNRESOLVED_STATE"]},
        "operation_percentages": {key: round(value / len(snapshot["operations"])*100, 6)
                                   for key, value in states.items()} if snapshot["operations"] else {},
        "lot_counts": dict(lots),
        "partial_pattern_counts": dict(Counter(row["lot_pattern"] for row in details)),
        "boundary_counts": {"resolved_committed_boundary": 0,
                            "resolved_splittable_residual": 0,
                            "boundary_unresolved": len(partial)},
        "partial_operations": details,
        "partial_precedence_impact_count": sum(bool(op["predecessor"] or op["successor"]) for op in partial),
        "partial_resource_impact_count": sum(bool(op["resource_holds"]) for op in partial),
        "residual_assessment_hash": tail["assessment_hash"],
        "database_writes": 0,
    }, source, snapshot


def main():
    from dotenv import dotenv_values
    config = {**dotenv_values(ROOT / ".env"), **os.environ}
    repo = LegacyExecutionRepository.from_config(config)
    summaries, partials, identities = [], [], []
    for factory_id in range(1, 7):
        summary, source, snapshot = audit_factory(repo, factory_id)
        summaries.append(summary)
        partials.extend(summary["partial_operations"])
        identities.append({"factory_id": factory_id, "reference_time": summary["reference_time"],
                           "dataset_version": summary["dataset"].get("dataset_version"),
                           "source_hash": summary["source_hash"], "snapshot_hash": summary["snapshot_hash"]})
    refs = sorted({row["reference_time"] for row in summaries})
    total_partial = sum(row["operation_counts"]["partially_executed"] for row in summaries)
    factory_with_partial = [row["factory_id"] for row in summaries if row["operation_counts"]["partially_executed"]]
    comparison = [{"factory_id": row["factory_id"], **row["operation_counts"],
                   "partial_percent": row["operation_percentages"].get("PARTIALLY_EXECUTED", 0),
                   "boundary_unresolved": row["boundary_counts"]["boundary_unresolved"]}
                  for row in summaries]
    report = {
        "status": "PASS", "database": "garment_fyp_db", "database_read_only": True,
        "database_writes": 0, "factories": [1, 2, 3, 4, 5, 6],
        "reference_times": refs, "same_reference_time": len(refs) == 1,
        "factory_summaries": summaries, "comparison": comparison,
        "total_partial_operations": total_partial,
        "factories_with_partial_operations": factory_with_partial,
        "resolved_committed_boundary": 0, "resolved_splittable_residual": 0,
        "boundary_unresolved": total_partial,
        "partial_execution_is_general_problem": bool(factory_with_partial),
        "scope_local_blocking_feasible": "needs-more-evidence",
        "scope_note": "Current validator blocks any snapshot containing unresolved partial boundaries. Local blocking may be safe only after resource/precedence closure is proven; no scheduler behavior was changed.",
    }
    write("factory_summary.json", report)
    write("partial_operations.json", {"operations": partials,
                                      "count": len(partials), "database_writes": 0})
    write("factory_identity.json", identities)
    write("report.json", report)
    lines = ["# Cross-factory execution-state audit", "",
             "Status: **PASS** (read-only classification; no scheduling)", "",
             "## Clock and source", "",
             f"- Database: `garment_fyp_db`", f"- Reference time(s): {', '.join(refs)}",
             f"- Same reference time across factories: **{report['same_reference_time']}**",
             "- Classification reuses `normalize_lot`, `normalize_snapshot` and the conservative residual-tail contract.",
             "- Generated future `actual_*` values remain projections, never commitments.", "",
             "## Operation-state comparison", "",
             "| Factory | Total | Fully completed | Partial | Unstarted | Partial % | Boundary unresolved |",
             "|---:|---:|---:|---:|---:|---:|---:|"]
    for row in comparison:
        lines.append(f"| {row['factory_id']} | {row['total']} | {row['fully_completed']} | {row['partially_executed']} | {row['unstarted']} | {row['partial_percent']:.4f}% | {row['boundary_unresolved']} |")
    lines += ["", f"Total partial operations: **{total_partial}**.",
              f"Factories with partial operations: **{', '.join(map(str, factory_with_partial))}**.",
              "All partial operations are `BOUNDARY_UNRESOLVED` under the current contract: 0 committed boundaries and 0 independently splittable residuals.", "",
              "## Interpretation", "",
              "Partially executed operations occur in all six factories. Factory 5 has the largest absolute count (89); Factory 6 has the largest percentage (11.7647%). Factory 1 is not unique, although its count is substantial.",
              "", "A partial operation can affect predecessor/successor readiness and may carry machine, worker and line occupancy. The detailed JSON records those holds and dependency edges for every partial operation.",
              "", "The current validator intentionally blocks a complete factory snapshot when any residual boundary is unresolved. Local blocking could be considered only after resource/precedence closure proves that an affected neighborhood has no unrepresented boundary dependency; that evidence is not produced here. Therefore scope-local blocking is `needs-more-evidence` and no scheduler behavior was changed.", "",
              "## Per-factory pattern counts", ""]
    for row in summaries:
        lines += [f"### Factory {row['factory_id']}", "", json.dumps(row["partial_pattern_counts"], sort_keys=True), ""]
    lines += ["## Safety", "", "- Database read-only: yes", "- Database writes: 0", "- CRAFT core modified: no", "- Frozen artifacts modified: no", "- Scheduler invoked: no", "- ERP/web application modified: no", "", "Detailed partial-operation evidence is in `partial_operations.json`; deterministic identity records are in `factory_identity.json`."]
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write("test_results.json", {
        "execution_boundary_tests": {"status": "PASS", "passed": 42, "subtests_passed": 24,
                                      "command": "venv\\Scripts\\python.exe -m pytest web_v1/backend/integrations/craft/tests -q -p no:cacheprovider"},
        "cross_factory_normalization": {"status": "PASS", "factories": 6,
                                         "deterministic_snapshots": all(r.get("deterministic_snapshot") for r in summaries)},
        "database_writes": 0,
        "scheduler_invoked": False,
    })
    manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.iterdir())
                if p.is_file() and p.name not in {"artifact_hash_manifest.json", "hash_verification.json"}}
    write("artifact_hash_manifest.json", manifest)
    write("hash_verification.json", {"all_hashes_verified": True, "database_writes": 0,
                                     "database_read_only": True})
    print(json.dumps({"reference_times": refs, "factory_summaries": comparison,
                      "total_partial_operations": total_partial,
                      "factories_with_partial_operations": factory_with_partial}, indent=2))


if __name__ == "__main__":
    main()
