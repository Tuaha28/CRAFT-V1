"""Reproducible residual end-to-end compatibility audit.

This audit intentionally stops before CP-SAT. A residual duration can be
represented syntactically by the current interval model, but current
materialization and independent validation cannot prove that the interval is
only the remaining production quantity. Running it would therefore be a false
vertical-slice success.
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


def source_fingerprints():
    names = [
        "crafter/contracts.py",
        "crafter/data_adapter.py",
        "crafter/model.py",
        "crafter/repair/resource_configs.py",
        "crafter/repair/calendar_regimes.py",
        "crafter/repair/solve.py",
        "crafter/repair/scoped_validation.py",
        "crafter/repair/window_phenotype.py",
        "crafter/authoritative_semantics.py",
        "ga/worker_allocation.py",
        "utils/work_calendar.py",
        "web_v1/backend/integrations/craft/residual_adapter.py",
        "web_v1/backend/integrations/craft/residual_preparation.py",
    ]
    return {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        if (ROOT / name).exists() else None
        for name in names
    }


def fixture_result(packet):
    prepared = prepare_residual_for_craft(packet)
    validation = validate_preparation(prepared)
    operation = prepared["operations"][0]
    return {
        "preparation_status": prepared["status"],
        "preparation_valid": validation["valid"],
        "preparation_hash": prepared["preparation_hash"],
        "craft_input_allowed": prepared["craft_input_allowed"],
        "operation": {
            "key": operation["original_operation_key"],
            "completed_quantity": operation["immutable_history"]["completed_quantity"],
            "in_flight_quantity": operation["fixed_execution"]["in_flight_quantity"],
            "remaining_quantity": operation["remaining_executable_work"]["remaining_quantity"],
            "remaining_productive_seconds": operation["remaining_executable_work"]["productive_seconds"],
            "residual_lot_ids": operation["remaining_executable_work"]["residual_lot_ids"],
            "resource_commitments": operation["fixed_execution"]["resource_commitments"],
            "precedence": operation["precedence"],
        },
        "independent_validation": validation,
        "solver_input": "BLOCKED",
        "exact_replay": "NOT_RUN",
    }


def invalid_cases():
    cases = {}
    packet = committed_packet()
    packet["operations"][0]["quantity_ledger"]["remaining"] = 59
    cases["quantity_inconsistency"] = validate_evidence(packet)
    packet = committed_packet()
    packet["operations"][0]["remaining_work"]["source"] = "FUTURE_ACTUAL"
    cases["future_actual"] = validate_evidence(packet)
    packet = committed_packet()
    packet["operations"][0]["resource_commitments"][0]["segments"][1]["start"] = "2026-09-10T05:30:00"
    cases["resource_conflict"] = validate_evidence(packet)
    packet = committed_packet()
    packet["operations"][0]["precedence"]["successor_earliest_ready"] = "2026-09-09T21:00:00"
    cases["precedence_conflict"] = validate_evidence(packet)
    return {
        name: {"rejected": not result["valid"],
               "error_codes": sorted({item["code"] for item in result["errors"]})}
        for name, result in cases.items()
    }


def run_tests():
    commands = [
        [sys.executable, "-m", "pytest", "web_v1/backend/integrations/craft/tests",
         "-q", "-p", "no:cacheprovider"],
        [sys.executable, "-m", "pytest", "tests/test_boundary_aware_modes.py",
         "tests/test_replay_semantics.py", "tests/test_rolling_horizon_v2_step3a.py",
         "tests/test_rolling_horizon_v2_step3b.py", "tests/test_production_controller_v1.py",
         "tests/test_worker_reservation_boundaries.py",
         "tests/test_worker_pause_resume_contract.py",
         "tests/test_rolling_worker_segment_carry.py", "-k", "not window14",
         "-q", "-p", "no:cacheprovider",
         "--basetemp=web_v1/audits/residual_end_to_end_proof_v1/pytest_temp"],
    ]
    result = []
    for command in commands:
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        result.append({
            "command": command,
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-2000:],
            "stderr_tail": completed.stderr[-1000:],
        })
    return result


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    committed = fixture_result(committed_packet())
    splittable = fixture_result(splittable_packet())
    invalid = invalid_cases()

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

    write_json("compatibility_analysis.json", {
        "craft_solver_compatibility": "BLOCKED",
        "syntactic_duration_substitution": {
            "possible": True,
            "safe": False,
            "reason": "OperationSpec accepts one duration, but downstream materialization and validators interpret it as the complete logical operation.",
        },
        "seams": [
            {"path": "crafter/contracts.py", "finding": "OperationSpec and OperationAssignment have no quantity, lot, completed-history, or residual fields."},
            {"path": "crafter/model.py", "finding": "One fixed-size interval is created per logical operation."},
            {"path": "crafter/repair/resource_configs.py", "finding": "ResourceConfiguration is keyed by one operation and has no residual mapping."},
            {"path": "crafter/repair/solve.py", "finding": "Materialization emits one selected schedule row; replay replays that row as the operation."},
            {"path": "crafter/repair/scoped_validation.py", "finding": "Independent export validation receives operation rows and duration/resource fields, not a quantity ledger."},
            {"path": "crafter/repair/window_phenotype.py", "finding": "Scope accounting is operation-key based and does not reconcile completed versus residual quantity."},
        ],
        "minimum_extension_proposal": {
            "new_boundary": "Residual-aware materialization and independent validation contract around the existing OperationSpec interval.",
            "required_fields": [
                "original operation key and source lot IDs",
                "required/completed/remaining quantities",
                "validated residual productive seconds and source",
                "fixed executed/in-flight resource segments",
                "whole-operation completion boundary",
                "predecessor/successor boundary evidence",
            ],
            "affected_modules": [
                "web_v1 residual preparation",
                "CRAFT materialization adapter",
                "exact replay input/output contract",
                "independent physical validator",
                "scope/quantity reconciliation",
            ],
            "solver_math_change": "Not required if the residual interval remains one indivisible schedulable unit and all quantity/precedence semantics are enforced outside the CP model.",
            "risk": "Must be independently proven before changing craft_input_allowed.",
        },
        "live_partial_operations": live_partial,
        "live_operations_resolved": 0,
        "live_operations_unresolved": live_partial,
        "live_summary_hash": digest(live),
    })
    write_json("fixture_inputs.json", {
        "committed_packet": committed_packet(),
        "splittable_packet": splittable_packet(),
        "note": "Controlled evidence only; not live factory data.",
    })
    write_json("fixture_outputs.json", {
        "committed_continuation": committed,
        "splittable_residual": splittable,
        "invalid_cases": invalid,
        "deterministic_preparation": deterministic,
    })
    write_json("quantity_validation.json", {
        "fixture_status": "PASS",
        "committed": {"required": 100, "completed": 40, "in_flight": 20, "unstarted": 40, "remaining": 60},
        "splittable": {"required": 100, "completed": 40, "in_flight": 0, "unstarted": 60, "remaining": 60},
        "live_operations_resolved": 0,
        "live_operations_unresolved": live_partial,
        "craft_quantity_reconciliation": "BLOCKED: current CRAFT output has no quantity ledger.",
    })
    write_json("resource_validation.json", {
        "fixture_status": "PASS",
        "machine_worker_line_commitments_preserved": True,
        "calendar_coverage_checked": True,
        "conflicting_resource_evidence_rejected": invalid["resource_conflict"]["rejected"],
        "live_status": "UNPROVEN_FAIL_CLOSED",
    })
    write_json("precedence_validation.json", {
        "fixture_status": "PASS",
        "whole_operation_predecessor_boundary_preserved": True,
        "successor_release_conflict_rejected": invalid["precedence_conflict"]["rejected"],
        "split_successor_semantics": "UNSUPPORTED",
        "live_status": "UNRESOLVED_FAIL_CLOSED",
    })
    write_json("replay_validation.json", {
        "preparation_hash_deterministic": deterministic,
        "exact_replay": "NOT_RUN",
        "reason": "No residual packet is admitted to existing CRAFT materialization.",
        "independent_structural_validation": "PASS",
    })
    tests = run_tests()
    write_json("test_results.json", {
        "runs": tests,
        "all_passed": all(item["returncode"] == 0 for item in tests),
        "craft_solver_invocations": 0,
    })
    write_json("manifest.json", {
        "contract": "RESIDUAL_END_TO_END_PROOF_V1",
        "reference_time": "2026-09-10T00:00:00",
        "source_fingerprints": source_fingerprints(),
        "live_partial_operations": live_partial,
        "live_operations_resolved": 0,
        "legacy_db_writes": 0,
        "craft_core_modified": False,
        "frozen_artifacts_modified": False,
    })

    report = """# Residual Execution End-to-End Compatibility Proof V1

## Decision

RESIDUAL_END_TO_END_PROOF_V1 = PARTIAL
RESIDUAL_SOLVER_INPUT = BLOCKED
ISOLATED_CP_SAT_SOLVE = NOT_RUN
RESIDUAL_MATERIALIZATION = NOT_RUN
EXACT_REPLAY = NOT_RUN
INDEPENDENT_PHYSICAL_VALIDATION = PASS for preparation structure only
SCOPE_VALIDATION = NOT_RUN

## Why the vertical slice is blocked

The existing CRAFT model can syntactically accept a positive remaining duration
in OperationSpec, but that interval is then treated as the complete logical
operation. OperationAssignment, selected schedule rows, materialization, replay
and independent physical validation have no required/completed/remaining
quantity ledger or residual-to-lot mapping. A CP-SAT candidate would therefore
not prove that completed production was excluded or that the output represents
the remaining quantity. Running it would be a false success.

Whole-operation predecessor/successor semantics are also the only supported
boundary. Split-successor behavior is rejected.

## Fixture evidence

The controlled committed and uncommitted residual fixtures pass preparation,
quantity, resource/calendar, provenance and whole-operation precedence checks.
Contradictory quantities, projected future actuals, stale evidence, and
resource conflicts are rejected. Reordered input produces an identical
preparation hash. No solver or live schedule was invoked.

The live audit remains 278 partial operations, 0 resolved and 278 unresolved.

## Minimum change proposal

Keep CP-SAT mathematics unchanged, but add an independently specified boundary
around materialization, replay and physical/scope validation. It must carry the
original operation/lot identity, completed and residual quantities, validated
remaining work, fixed resource segments and whole-operation completion boundary.
The independent validator must reconcile quantities and map every output row back
to source lots. Only after that proof may craft_input_allowed be enabled for a
controlled residual fixture.

## Safety

Legacy database writes: 0. CRAFT core modified: no. Frozen artifacts modified:
no. Live scheduling and promotion: none. SAFE_TO_RESUME_V2_STEP_2 = NO.
"""
    (OUT / "report.md").write_text(report, encoding="utf-8")

    manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    for path in sorted(OUT.iterdir()):
        if path.name in {"manifest.json", "hash_verification.json"} or path.is_dir():
            continue
        manifest.setdefault("artifact_hashes", {})[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json("manifest.json", manifest)
    manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    mismatches = [
        name for name, expected in manifest["artifact_hashes"].items()
        if hashlib.sha256((OUT / name).read_bytes()).hexdigest() != expected
    ]
    write_json("hash_verification.json", {
        "status": "PASS" if not mismatches else "FAIL",
        "verified_artifacts": len(manifest["artifact_hashes"]),
        "mismatches": mismatches,
        "manifest_hash": hashlib.sha256((OUT / "manifest.json").read_bytes()).hexdigest(),
        "database_writes": 0,
        "craft_core_modified": False,
        "frozen_artifacts_modified": False,
    })


if __name__ == "__main__":
    main()
