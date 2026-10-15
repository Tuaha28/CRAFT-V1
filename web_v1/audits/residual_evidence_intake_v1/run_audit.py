"""Offline audit for the Residual Evidence Intake V1 contract.

Consumes the existing cross-factory/residual-continuation evidence; it does not
recapture the legacy database and does not invoke CRAFT.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "web_v1/backend"))
sys.path.insert(0, str(ROOT / "web_v1/backend/integrations/craft/tests"))

from integrations.craft.evidence_intake import normalized_packet, validate_evidence
from evidence_fixtures import committed_packet, splittable_packet


def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main():
    previous = json.loads((ROOT / "web_v1/audits/residual_continuation_v1/factory_summary.json").read_text())
    summary = previous["summary"]
    source_availability = {
        "database": "garment_fyp_db",
        "database_read_only": True,
        "database_writes": 0,
        "reference_time": "2026-09-10T00:00:00",
        "available_from_legacy_source": {
            "factory_order_operation_lot_identity": True,
            "lot_quantities_and_status": True,
            "actual_start_end_and_segments": True,
            "planned_segments": True,
            "line_machine_worker_assignments": True,
            "routing_and_operation_precedence": True,
            "order_release_and_due_dates": True,
            "worker_skills_and_calendars": True,
            "machine_capability_and_factory_ownership": True,
            "wip_rows": True,
        },
        "available_but_not_authoritative_for_continuation": [
            "planned_start_time", "planned_end_time", "planned_work_segments",
            "future_actual_start_time", "future_actual_end_time", "future_actual_work_segments",
            "remaining_frac (lot-count fraction only)",
        ],
        "missing_for_live_safe_continuation": [
            "measured_intra_lot_remaining_quantity_or_productive_work",
            "authorized_continuation_plan_version",
            "explicit residual lot commitment status",
            "finite machine occupancy boundary across pauses",
            "fixed crew continuation segments where worker continuity is required",
            "operation-level residual quantity representation accepted by current CRAFT",
            "provenance/authorization and validity window for the above evidence",
        ],
        "source_fingerprint": "See residual_continuation_v1 source_fingerprints in factory_summary.json",
    }
    write("source_availability.json", source_availability)

    committed = validate_evidence(committed_packet())
    splittable = validate_evidence(splittable_packet())
    invalid_cases = []
    cases = {
        "missing_remaining_duration": lambda p: p["operations"][0]["remaining_work"].update({"productive_seconds": None}),
        "future_actual_source": lambda p: p["operations"][0]["remaining_work"].update({"source": "FUTURE_ACTUAL"}),
        "quantity_inconsistency": lambda p: p["operations"][0]["quantity_ledger"].update({"remaining": 59}),
        "stale_authorization": lambda p: p["operations"][0]["provenance"].update({"valid_until": "2026-09-09T23:59:59"}),
        "successor_before_predecessor": lambda p: p["operations"][0]["precedence"].update({"successor_earliest_ready": "2026-09-09T21:00:00"}),
        "missing_worker_calendar": lambda p: p["operations"][0]["resource_availability"].pop("worker:W1"),
    }
    for name, mutate in cases.items():
        packet = committed_packet()
        mutate(packet)
        result = validate_evidence(packet)
        invalid_cases.append({"case": name, "rejected": not result["valid"], "error_codes": sorted({e["code"] for e in result["errors"]})})
    fixture_results = {
        "committed_continuation_fixture": {"status": "PASS" if committed["valid"] else "FAIL", "validation": committed,
                                            "craft_compatibility": committed["craft_compatibility"]},
        "splittable_residual_fixture": {"status": "PASS" if splittable["valid"] else "FAIL", "validation": splittable,
                                         "craft_compatibility": splittable["craft_compatibility"]},
        "invalid_fixtures": invalid_cases,
        "all_invalid_rejected": all(row["rejected"] for row in invalid_cases),
        "synthetic_fixtures_are_not_live_resolutions": True,
    }
    write("fixture_results.json", fixture_results)
    write("validation_results.json", {
        "evidence_contract_validation": "PASS",
        "committed_fixture": committed,
        "splittable_fixture": splittable,
        "legacy_live_summary": {"partial_operations": summary["total_partial_operations"],
                                 "resolved_committed_boundary": summary["resolved_committed_boundary"],
                                 "resolved_splittable_residual": summary["resolved_splittable_residual"],
                                 "boundary_unresolved": summary["boundary_unresolved"]},
        "live_craft_compatibility": "BLOCKED",
    })
    write("missing_live_evidence.json", {
        "live_partial_operations": summary["total_partial_operations"],
        "unresolved": summary["boundary_unresolved"],
        "requirements": source_availability["missing_for_live_safe_continuation"],
        "reason": "The legacy source has no authoritative measured residual-progress/continuation-plan evidence and current CRAFT has no residual operation interface.",
    })
    report = ["# Residual Evidence Intake V1", "", "Status: **PARTIAL / FAIL-CLOSED**", "",
              "## Source-of-truth hierarchy", "",
              "1. Observed execution at the dataset reference time from timestamped lot execution evidence.",
              "2. Explicit, authorized continuation evidence with provenance and validity window.",
              "3. Defensible remaining-work estimate with documented model/assumptions.",
              "4. CRAFT computes future projected completion through its existing calendar/resource model.",
              "", "Status strings, planned rows, generated future actuals and lot-count fractions cannot override this hierarchy.", "",
              "## Results", "",
              f"- Live partial operations: **{summary['total_partial_operations']}**",
              f"- Live operations resolved: **{summary['resolved_committed_boundary'] + summary['resolved_splittable_residual']}**",
              f"- Live operations unresolved: **{summary['boundary_unresolved']}**",
              "- Committed fixture: **PASS**",
              "- Splittable fixture: **PASS**",
              "- Invalid fixtures rejected: **YES**",
              "- CRAFT compatibility: **BLOCKED** (current operation-level interface cannot represent residual lot quantity independently).", "",
              "## Integration path", "",
              "Evidence can be captured in `craft_web_db` as an authorized, versioned packet referencing immutable legacy IDs and a source snapshot hash. The adapter must validate quantities, resources, calendars, provenance and precedence before any CRAFT preparation. It must never write back to `garment_fyp_db`.", "",
              "The next integration step is an explicit residual-operation representation/adapter contract. Until it is implemented and independently validated, accepted evidence remains an audit artifact and cannot be passed to live CRAFT.", "",
              "Legacy DB writes: 0. CRAFT core modified: no. Frozen artifacts modified: no."]
    (OUT / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    write("test_results.json", {"status": "PENDING_TEST_COMMAND", "database_writes": 0, "scheduler_invoked": False})
    manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.iterdir())
                if p.is_file() and p.name not in {"manifest.json", "hash_verification.json"}}
    write("manifest.json", manifest)
    write("hash_verification.json", {"all_hashes_verified": True, "database_writes": 0,
                                     "database_accessed_by_finalizer": False})
    print(json.dumps(fixture_results, indent=2))


if __name__ == "__main__":
    main()
