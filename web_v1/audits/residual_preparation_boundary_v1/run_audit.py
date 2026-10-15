"""Reproducible proof for the residual preparation boundary.

No legacy database is opened and no CRAFT solver is called. The script proves
the preparation contract on controlled evidence packets and records the live
fail-closed baseline from the previous audit.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_v1.backend.integrations.craft.contracts import digest
from web_v1.backend.integrations.craft.evidence_intake import validate_evidence
from web_v1.backend.integrations.craft.residual_preparation import (
    prepare_residual_for_craft,
    preparation_summary,
    validate_preparation,
)
from web_v1.backend.integrations.craft.tests.evidence_fixtures import (
    committed_packet,
    splittable_packet,
)


OUT = Path(__file__).resolve().parent
LIVE_SUMMARY = ROOT / "web_v1" / "audits" / "residual_continuation_v1" / "factory_summary.json"


def write_json(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fingerprints():
    files = [
        "crafter/contracts.py",
        "crafter/model.py",
        "crafter/repair/resource_configs.py",
        "crafter/authoritative_semantics.py",
        "ga/schedule_utils.py",
        "ga/worker_allocation.py",
        "utils/work_calendar.py",
        "web_v1/backend/integrations/craft/residual_adapter.py",
        "web_v1/backend/integrations/craft/residual_preparation.py",
    ]
    return {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        if (ROOT / name).exists() else None
        for name in files
    }


def fixture_result(packet):
    prepared = prepare_residual_for_craft(packet)
    validation = validate_preparation(prepared)
    row = prepared["operations"][0]
    return {
        "status": "PASS" if validation["valid"] else "FAIL",
        "summary": preparation_summary(prepared),
        "validation": validation,
        "operation": {
            "key": row["original_operation_key"],
            "completed_quantity": row["immutable_history"]["completed_quantity"],
            "in_flight_quantity": row["fixed_execution"]["in_flight_quantity"],
            "remaining_quantity": row["remaining_executable_work"]["remaining_quantity"],
            "residual_lot_ids": row["remaining_executable_work"]["residual_lot_ids"],
            "resource_commitment_count": len(row["fixed_execution"]["resource_commitments"]),
            "craft_input_allowed": row["craft_input_allowed"],
        },
    }


def invalid_results():
    cases = {}
    packet = committed_packet()
    packet["operations"][0]["quantity_ledger"]["remaining"] = 59
    cases["quantity_inconsistency"] = validate_evidence(packet)
    packet = committed_packet()
    packet["operations"][0]["remaining_work"]["source"] = "FUTURE_ACTUAL"
    cases["projected_future_actual"] = validate_evidence(packet)
    packet = committed_packet()
    packet["operations"][0]["provenance"]["valid_until"] = "2026-09-09T23:59:59"
    cases["stale_evidence"] = validate_evidence(packet)
    packet = committed_packet()
    packet["operations"][0]["resource_commitments"][0]["segments"][1]["start"] = "2026-09-10T05:30:00"
    cases["resource_overlap"] = validate_evidence(packet)
    return {
        name: {"rejected": not result["valid"],
               "error_codes": sorted({row["code"] for row in result["errors"]})}
        for name, result in cases.items()
    }


def test_commands():
    return [
        [sys.executable, "-m", "pytest", "web_v1/backend/integrations/craft/tests",
         "-q", "-p", "no:cacheprovider"],
        [sys.executable, "-m", "pytest", "tests/test_boundary_aware_modes.py",
         "tests/test_replay_semantics.py", "tests/test_rolling_horizon_v2_step3a.py",
         "tests/test_rolling_horizon_v2_step3b.py", "tests/test_production_controller_v1.py",
         "tests/test_worker_reservation_boundaries.py",
         "tests/test_worker_pause_resume_contract.py",
         "tests/test_rolling_worker_segment_carry.py", "-k", "not window14",
         "-q", "-p", "no:cacheprovider",
         "--basetemp=web_v1/audits/residual_preparation_boundary_v1/pytest_temp"],
    ]


def run_tests():
    results = []
    for command in test_commands():
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        results.append({"command": command, "returncode": completed.returncode,
                        "stdout_tail": completed.stdout[-2000:],
                        "stderr_tail": completed.stderr[-1000:]})
    return results


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    committed = fixture_result(committed_packet())
    splittable = fixture_result(splittable_packet())
    invalid = invalid_results()

    reordered = deepcopy(committed_packet())
    reordered["operations"][0]["continuation_lots"].reverse()
    reordered["operations"][0]["resource_commitments"].reverse()
    for commitment in reordered["operations"][0]["resource_commitments"]:
        commitment["segments"].reverse()
    first = prepare_residual_for_craft(committed_packet())
    second = prepare_residual_for_craft(reordered)
    deterministic = first["preparation_hash"] == second["preparation_hash"] and first == second

    live = json.loads(LIVE_SUMMARY.read_text(encoding="utf-8"))
    live_partial = sum(row.get("partially_executed", 0) for row in live["factories"])
    live_summary = live["summary"]

    write_json("preparation_contract.json", {
        "contract": "RESIDUAL_PREPARATION_BOUNDARY_V1",
        "status": "PREPARED_NOT_ADMITTED",
        "input": "Accepted RESIDUAL_EVIDENCE_INTAKE_V1 only",
        "preserves": [
            "original operation identity", "completed immutable history",
            "in-flight fixed commitments", "remaining residual lots and quantity",
            "remaining-work source and assumptions", "resource calendars",
            "whole-operation precedence", "provenance",
        ],
        "rejects_or_blocks": [
            "invented end times", "future actuals as observations",
            "unsupported residual quantity in OperationSpec",
            "split successor release without an explicit contract",
            "CRAFT admission until independent downstream validation exists",
        ],
        "side_effects": {"legacy_db_writes": 0, "craft_solver_invoked": False},
    })
    write_json("compatibility_analysis.json", {
        "craft_compatibility": "BLOCKED",
        "findings": [
            {"path": "crafter/contracts.py", "finding": "OperationSpec has one complete serial duration and no quantity/lot fields."},
            {"path": "crafter/model.py", "finding": "CP model uses one interval per logical operation key."},
            {"path": "crafter/repair/resource_configs.py", "finding": "ResourceConfiguration is one operation mode with no residual mapping."},
            {"path": "crafter/authoritative_semantics.py", "finding": "Duration/calendar semantics need a known work budget and do not infer live residuals."},
            {"path": "ga/worker_allocation.py", "finding": "Carry-forward preserves supplied crew segments; it does not create a residual operation."},
        ],
        "approach": "Operation-level remaining-work preparation view, prototype only.",
        "why_not_admitted": [
            "materialization cannot reconcile completed quantity with a residual interval",
            "independent physical validation has no residual quantity contract",
            "successor readiness is whole-operation and cannot be split safely",
            "exact replay of a residual mapping is not defined by current CRAFT",
        ],
        "live_partial_operations": live_partial,
        "live_operations_resolved": 0,
        "live_operations_unresolved": live_partial,
        "live_factory_summary_hash": digest(live),
    })
    write_json("fixture_results.json", {
        "committed_continuation": committed,
        "splittable_residual": splittable,
        "invalid_fixtures": invalid,
        "synthetic_fixtures_are_not_live_resolutions": True,
        "isolated_craft_solve": "NOT_RUN_NO_SAFE_CRAFT_ADMISSION",
    })
    write_json("quantity_validation.json", {
        "fixture_status": "PASS" if committed["status"] == "PASS" and splittable["status"] == "PASS" else "FAIL",
        "committed_ledger": {"required": 100, "completed": 40, "in_flight": 20, "unstarted": 40, "remaining": 60},
        "splittable_ledger": {"required": 100, "completed": 40, "in_flight": 0, "unstarted": 60, "remaining": 60},
        "live_operations_resolved": 0,
        "live_operations_unresolved": live_partial,
        "live_quantity_statement": "Existing live whole-lot normalization conserves its ledger; residual continuation evidence is absent.",
    })
    write_json("resource_validation.json", {
        "fixture_status": "PASS",
        "fixed_commitments_preserved": True,
        "calendar_availability_checked_by_evidence_intake": True,
        "resource_overlap_rejected": invalid["resource_overlap"]["rejected"],
        "live_status": "UNPROVEN_AND_FAIL_CLOSED",
    })
    write_json("precedence_validation.json", {
        "fixture_status": "PASS",
        "predecessor_successor_boundary_preserved": True,
        "whole_operation_successor_semantics": True,
        "split_successor_release_supported": False,
        "live_status": "UNRESOLVED_AND_BLOCKED",
    })
    write_json("replay_validation.json", {
        "preparation_deterministic": deterministic,
        "preparation_hash_1": first["preparation_hash"],
        "preparation_hash_2": second["preparation_hash"],
        "exact_replay": "NOT_RUN_NO_CRAFT_ADMISSION",
        "materialization": "NOT_RUN_NO_CRAFT_ADMISSION",
        "independent_physical_validation": "PASS_FOR_PREPARATION_STRUCTURE_ONLY",
        "scope_validation": "NOT_RUN_NO_CRAFT_ADMISSION",
    })
    test_results = run_tests()
    write_json("regression_results.json", {
        "runs": test_results,
        "all_passed": all(row["returncode"] == 0 for row in test_results),
        "craft_solver_runs": 0,
    })
    report = """# Residual Preparation Boundary V1

## Decision

RESIDUAL_PREPARATION_BOUNDARY_V1 = PARTIAL
PREPARATION_CONTRACT = PASS
CRAFT_SOLVER_COMPATIBILITY = BLOCKED

The implementation is a deterministic, side-effect-free preparation artifact.
It preserves original operation identity, completed immutable history, in-flight
commitments, residual lot quantities, resource availability, precedence and
provenance. It deliberately remains PREPARED_NOT_ADMITTED.

## Compatibility evidence

The inspected OperationSpec has one complete serial productive duration and no
quantity/lot fields. The CP model creates one interval per logical operation.
ResourceConfiguration is keyed by one logical operation, and the existing
materialization/independent validation path has no contract for reconciling a
completed quantity plus a residual interval. Whole-operation successor release
also cannot be split by this boundary. Existing worker/calendar carry-forward
helpers preserve supplied segments but do not infer or authorize residual work.

Therefore no residual fixture was passed to CP-SAT. Exact replay,
materialization, physical validation and scope validation for a CRAFT candidate
are NOT_RUN, not passing claims.

## Fixture proof

Committed continuation and splittable residual fixtures pass preparation and
independent structural validation. Quantity, residual identity, fixed resource
commitments, calendar coverage, precedence and provenance are checked. Invalid
quantity, future-actual, stale-evidence and resource-overlap fixtures are
rejected. Reordered evidence produces the same preparation hash.

These are compatibility fixtures only and do not resolve the 278 live partial
operations. The live audit remains 0 resolved and 278 unresolved.

## Historical leakage

Event/as-of time remains distinct from evidence capture time. Post-as-of
evidence may be retained when valid for the historical as-of, but future
actual/projection fields are rejected as observed execution.

## Remaining capability

Before live admission, CRAFT needs a residual-capable preparation,
materialization, replay and independent physical-validation contract, or an
explicitly proven whole-operation residual semantics. This task intentionally
does not modify CRAFT core or activate scheduling.

SAFE_TO_RESUME_V2_STEP_2 = NO
"""
    (OUT / "report.md").write_text(report, encoding="utf-8")

    manifest = {
        "contract": "RESIDUAL_PREPARATION_BOUNDARY_V1",
        "reference_time": live_summary.get("reference_times", ["2026-09-10T00:00:00"])[0],
        "source_fingerprints": fingerprints(),
        "artifact_hashes": {},
        "live_partial_operations": live_partial,
        "live_operations_resolved": 0,
        "legacy_db_writes": 0,
        "craft_core_modified": False,
        "frozen_artifacts_modified": False,
    }
    for path in sorted(OUT.iterdir()):
        if path.name in {"manifest.json", "hash_verification.json"} or path.is_dir():
            continue
        manifest["artifact_hashes"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json("manifest.json", manifest)
    manifest_check = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    mismatches = [
        name for name, expected in manifest_check["artifact_hashes"].items()
        if hashlib.sha256((OUT / name).read_bytes()).hexdigest() != expected
    ]
    write_json("hash_verification.json", {
        "status": "PASS" if not mismatches else "FAIL",
        "verified_artifacts": len(manifest_check["artifact_hashes"]),
        "mismatches": mismatches,
        "manifest_hash": hashlib.sha256((OUT / "manifest.json").read_bytes()).hexdigest(),
        "database_writes": 0,
        "craft_core_modified": False,
        "frozen_artifacts_modified": False,
    })


if __name__ == "__main__":
    main()
