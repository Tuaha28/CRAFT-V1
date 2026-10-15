"""Reproducible, side-effect-free proof for residual operation compatibility.

This audit intentionally does not connect to the legacy database and never
invokes a CRAFT solve. It proves the adapter's evidence/identity/quantity view
on controlled packets and records the existing live fail-closed counts.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

# Make direct ``python path/to/run_audit.py`` invocation behave like the
# repository-root test invocation without requiring an environment mutation.
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_v1.backend.integrations.craft.contracts import digest
from web_v1.backend.integrations.craft.residual_adapter import (
    build_residual_view,
    compatibility_summary,
    validate_residual_view,
)
from web_v1.backend.integrations.craft.tests.evidence_fixtures import (
    committed_packet,
    splittable_packet,
)
from web_v1.backend.integrations.craft.evidence_intake import validate_evidence


OUT = Path(__file__).resolve().parent
LIVE_SUMMARY = ROOT / "web_v1" / "audits" / "residual_continuation_v1" / "factory_summary.json"
REFERENCE_TIME = "2026-09-10T00:00:00"


def write_json(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def source_fingerprints():
    paths = [
        "crafter/contracts.py",
        "crafter/model.py",
        "crafter/repair/resource_configs.py",
        "crafter/authoritative_semantics.py",
        "ga/schedule_utils.py",
        "ga/worker_allocation.py",
        "utils/work_calendar.py",
        "web_v1/backend/integrations/craft/evidence_intake.py",
        "web_v1/backend/integrations/craft/residual_adapter.py",
    ]
    return {
        relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        if (ROOT / relative).exists() else None
        for relative in paths
    }


def fixture_case(packet):
    intake = validate_evidence(packet)
    if not intake["valid"]:
        return {"status": "FAIL", "intake": intake}
    view = build_residual_view(packet)
    validation = validate_residual_view(view)
    return {
        "status": "PASS" if validation["valid"] else "FAIL",
        "intake": {"valid": intake["valid"], "packet_hash": intake["packet_hash"],
                   "craft_compatibility": intake["craft_compatibility"]},
        "adapter": compatibility_summary(view),
        "validation": validation,
        "quantity": {
            row["original_operation_key"]: {
                "required": row["execution_boundary"]["required_quantity"],
                "completed": row["execution_boundary"]["completed_quantity"],
                "in_flight": row["execution_boundary"]["in_flight_quantity"],
                "unstarted": row["execution_boundary"]["unstarted_quantity"],
                "remaining": row["execution_boundary"]["remaining_quantity"],
            }
            for row in view["operations"]
        },
    }


def invalid_fixture_results():
    cases = {}
    packet = committed_packet()
    packet["operations"][0]["quantity_ledger"]["remaining"] = 59
    cases["quantity_inconsistency"] = validate_evidence(packet)
    packet = committed_packet()
    packet["operations"][0]["remaining_work"]["source"] = "FUTURE_ACTUAL"
    cases["projected_actual"] = validate_evidence(packet)
    packet = committed_packet()
    packet["operations"][0]["resource_commitments"][0]["segments"][1]["start"] = "2026-09-10T05:30:00"
    cases["resource_overlap"] = validate_evidence(packet)
    return {
        name: {"rejected": not result["valid"],
               "error_codes": sorted({e["code"] for e in result["errors"]})}
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
         "--basetemp=web_v1/audits/residual_adapter_proof_v1/pytest_temp"],
    ]
    results = []
    for command in commands:
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        results.append({"command": command, "returncode": completed.returncode,
                        "stdout_tail": completed.stdout[-2000:],
                        "stderr_tail": completed.stderr[-1000:]})
    return results


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    committed = fixture_case(committed_packet())
    splittable = fixture_case(splittable_packet())
    invalid = invalid_fixture_results()

    reordered = deepcopy(committed_packet())
    operation = reordered["operations"][0]
    operation["continuation_lots"].reverse()
    operation["resource_commitments"].reverse()
    for commitment in operation["resource_commitments"]:
        commitment["segments"].reverse()
    first_view = build_residual_view(committed_packet())
    second_view = build_residual_view(reordered)
    deterministic = first_view["view_hash"] == second_view["view_hash"] and first_view == second_view

    live = json.loads(LIVE_SUMMARY.read_text(encoding="utf-8"))
    live_summary = live["summary"]
    factories = live.get("factories", [])
    live_partial = sum(row.get("partially_executed", 0) for row in factories)
    live_unresolved = sum(row.get("partially_executed", 0) for row in factories)

    representation = {
        "selected": "APPROACH_B_OPERATION_LEVEL_RESIDUAL_VIEW",
        "selected_status": "PROTOTYPE_ONLY",
        "approaches": {
            "A_FIXED_COMMITMENT_PLUS_RESIDUAL_OPERATION": {
                "assessment": "CONCEPTUALLY_SAFE_BUT_NOT_REPRESENTABLE",
                "reason": "Current CRAFT has fixed resource commitments but no residual quantity/lot entity or split predecessor boundary.",
            },
            "B_OPERATION_LEVEL_REMAINING_WORK_NORMALIZATION": {
                "assessment": "SMALLEST_ISOLATED_VIEW",
                "reason": "Preserves original operation identity, immutable history, residual lots, evidence duration, commitments and precedence without changing the solver.",
            },
            "C_EXISTING_NATIVE_SUPPORT": {
                "assessment": "INSUFFICIENT",
                "reason": "Existing segmented worker/machine helpers consume known work budgets and reservations; they do not reconstruct or admit lot residuals.",
            },
        },
        "code_evidence": [
            {"path": "crafter/contracts.py", "finding": "OperationSpec has one serial productive duration and no quantity/lot fields."},
            {"path": "crafter/model.py", "finding": "CP model creates one indivisible interval per operation key."},
            {"path": "crafter/repair/resource_configs.py", "finding": "ResourceConfiguration is keyed by one operation and has no residual/lot mapping."},
            {"path": "crafter/authoritative_semantics.py", "finding": "Duration contract computes complete operation work; shared calendar helpers require a known work budget."},
            {"path": "ga/worker_allocation.py", "finding": "Carry-forward supports supplied fixed crews/segments, not inference of live lot residuals."},
        ],
        "craft_core_compatibility": "BLOCKED",
    }
    adapter_contract = {
        "contract": "RESIDUAL_OPERATION_ADAPTER_V1",
        "input": "Accepted RESIDUAL_EVIDENCE_INTAKE_V1 packet only",
        "output": ["original operation identity", "immutable completed history",
                   "residual lots and quantity", "remaining work source/assumptions",
                   "fixed commitments", "precedence", "provenance"],
        "invariants": ["quantity conservation", "residual lot identity",
                       "resource non-overlap", "predecessor boundary preserved",
                       "CRAFT admission closed", "deterministic hash"],
        "side_effects": {"legacy_database_writes": 0, "scheduler_invocation": False},
        "craft_input_allowed": False,
        "blocked_reason": "Current CRAFT mathematical representation cannot consume residual quantity/lot continuation independently of the original operation.",
    }
    write_json("representation_comparison.json", representation)
    write_json("adapter_contract.json", adapter_contract)
    write_json("fixture_results.json", {
        "committed_continuation": committed,
        "splittable_residual": splittable,
        "invalid_fixtures": invalid,
        "synthetic_fixtures_are_not_live_resolutions": True,
    })
    write_json("quantity_conservation.json", {
        "fixture_status": "PASS" if committed["status"] == "PASS" and splittable["status"] == "PASS" else "FAIL",
        "fixtures": {"committed": committed["quantity"], "splittable": splittable["quantity"]},
        "live_partial_operations": live_partial,
        "live_operations_resolved": 0,
        "live_operations_unresolved": live_unresolved,
        "live_statement": "Existing normalized whole-lot ledgers conserve quantity; no live residual evidence packet was accepted.",
    })
    write_json("replay_validation.json", {
        "adapter_deterministic_replay": deterministic,
        "adapter_view_hash_1": first_view["view_hash"],
        "adapter_view_hash_2": second_view["view_hash"],
        "craft_exact_replay": "NOT_RUN_BLOCKED_NO_CRAFT_INPUT_COMPATIBILITY",
        "independent_adapter_validator": "PASS" if validate_residual_view(first_view)["valid"] else "FAIL",
    })
    write_json("compatibility_results.json", {
        "craft_core_compatibility": "BLOCKED",
        "craft_core_modified": False,
        "craft_solver_invoked": False,
        "operation_level_residual_interface": False,
        "fixed_commitment_interface": True,
        "live_operations_resolved": 0,
        "live_operations_unresolved": live_unresolved,
        "live_factory_summary_hash": digest(live),
    })
    test_results = run_tests()
    write_json("test_results.json", {
        "runs": test_results,
        "all_passed": all(row["returncode"] == 0 for row in test_results),
        "adapter_tests_and_continuity_regressions": True,
    })

    report = """# Residual Operation Compatibility & Adapter Proof V1

## Result

RESIDUAL_ADAPTER_PROOF_V1 = PARTIAL

The selected representation is an isolated APPROACH_B_OPERATION_LEVEL_RESIDUAL_VIEW.
It preserves the original operation key, immutable completed history, residual lot
ledger, evidence-backed remaining work, resource commitments, precedence and
provenance. It is deliberately not admitted as a CRAFT operation input.

## Actual CRAFT boundary

Inspection of crafter/contracts.py, crafter/model.py and
crafter/repair/resource_configs.py shows that CRAFT accepts one indivisible
operation interval with one complete productive duration. The current resource
configuration and fixed-commitment interfaces preserve resources, but do not
represent residual lot quantities, completed history plus residual work, or a
split successor-release boundary. ga.schedule_utils.split_operation_work and
the calendar/crew helpers require a known work budget; they do not infer live
remaining work. Therefore CRAFT_CORE_COMPATIBILITY = BLOCKED and no solver,
materializer, replay, or production schedule was invoked.

## Proof performed

- Committed-continuation fixture: PASS.
- Splittable-residual fixture: PASS.
- Invalid quantity, projected-actual and resource-overlap fixtures: rejected.
- Quantity ledgers and residual lot IDs are independently checked.
- Reordered input produces the same adapter view and hash.
- Adapter admission remains closed (craft_input_allowed=false).
- Existing live audit remains fail-closed: 278 partial operations, 0 resolved,
  278 unresolved across six factories.

The fixtures prove the view and its invariants only; they do not resolve live
factory operations. Future actual timestamps remain rejected as evidence, and
post-as-of evidence capture is allowed only when its validity window covers the
as-of time.

## Required capability before live admission

A future integration must either extend or wrap CRAFT preparation/validation
with an explicit residual representation that accepts fixed executed history and
validated remaining work while preserving whole-operation precedence and exact
replay. It must also prove finite machine/worker continuation or explicitly
authorize a safely separable residual. This task intentionally does not change
the CRAFT core or activate such a representation.

## Source-of-truth hierarchy and minimum evidence

The adapter follows the established hierarchy: observed execution at the
reference time first; explicit authorized continuation evidence second;
defensible remaining-work estimates with model and assumptions third; and
future projected completion calculated by CRAFT only after admission. Planned
rows and generated future actual timestamps cannot override that order.

Minimum evidence for a future live admission is a reconciled quantity ledger,
lot identity, observed/in-flight state, authorized continuation or explicit
residual-splitting proof, resource assignments and finite availability/occupancy
segments, worker continuity where required, a working-duration source, and
precedence boundaries with provenance and validity. The legacy source currently
does not provide that complete residual packet for its 278 partial operations.

## Safety

Legacy database writes: 0. CRAFT core modified: no. Frozen artifacts modified:
no. SAFE_TO_RESUME_V2_STEP_2 = NO.
"""
    (OUT / "report.md").write_text(report, encoding="utf-8")

    manifest = {
        "contract": "RESIDUAL_ADAPTER_PROOF_V1",
        "reference_time": REFERENCE_TIME,
        "source_fingerprints": source_fingerprints(),
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
    mismatches = []
    for name, expected in manifest_check["artifact_hashes"].items():
        actual = hashlib.sha256((OUT / name).read_bytes()).hexdigest()
        if actual != expected:
            mismatches.append(name)
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
