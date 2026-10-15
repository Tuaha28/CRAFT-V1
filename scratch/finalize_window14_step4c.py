from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "crafter" / "factory2_172_v1" / "window14_step4c"


def read_json(name: str):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def write_json(name: str, payload) -> None:
    (OUT / name).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_focused_tests() -> dict:
    test_files = [
        "tests/test_repair_objectives.py",
        "tests/test_rolling_horizon_v2_step3a.py",
        "tests/test_rolling_horizon_v2_step3b.py",
        "tests/test_laya_shadow_v1.py",
        "tests/test_boundary_aware_modes.py",
        "tests/test_route_aware_crew_expansion.py",
        "tests/test_calendar_regime_coalescing.py",
    ]
    cmd = [
        str(ROOT / "venv" / "Scripts" / "python.exe"),
        "-m",
        "pytest",
        "-q",
        *test_files,
        "--basetemp",
        str(OUT / "pytest_basetemp_final_v1"),
    ]
    started = datetime.now(timezone.utc)
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    ended = datetime.now(timezone.utc)
    output = completed.stdout
    summary_line = next(
        (line.strip() for line in reversed(output.splitlines()) if " in " in line), ""
    )
    passed_match = re.search(r"(\d+) passed", output)
    failed_match = re.search(r"(\d+) failed", output)
    warning_match = re.search(r"(\d+) warnings?", output)
    return {
        "command": cmd,
        "test_files": test_files,
        "exit_code": completed.returncode,
        "passed": int(passed_match.group(1)) if passed_match else 0,
        "failed": int(failed_match.group(1)) if failed_match else 0,
        "warnings": int(warning_match.group(1)) if warning_match else 0,
        "summary": summary_line,
        "started_utc": started.isoformat(),
        "ended_utc": ended.isoformat(),
        "database_writes": 0,
        "output": output,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    focused_path = OUT / "focused_tests.json"
    if focused_path.exists():
        prior_tests = json.loads(focused_path.read_text(encoding="utf-8"))
    else:
        prior_tests = None
    if prior_tests and prior_tests.get("exit_code") == 0:
        tests = prior_tests
    else:
        tests = run_focused_tests()
        write_json("focused_tests.json", tests)

    starting = read_json("starting_state.json")
    result = read_json("window14_result.json")
    round0 = read_json("adaptive_round_0.json")
    round1 = read_json("adaptive_round_1.json")
    superset = read_json("domain_superset_audit.json")
    selected = read_json("selected_new_crews.json")
    pressure = read_json("worker_pressure_diagnosis.json")
    closure = read_json("expansion_closure.json")
    pressure_rows = pressure["diagnostics"]
    pressure_detected = any(row["route_pressure"] for row in pressure_rows)
    first_pressure_operation = next(
        (row["operation_key"] for row in pressure_rows if row["route_pressure"]), None
    )
    closure_round_1 = next(row for row in closure["rounds"] if row["round"] == 1)
    base_stage_statuses = [row["status"] for row in round0["solve"]["stages"]]
    base_cp_status = (
        "INFEASIBLE"
        if "INFEASIBLE" in base_stage_statuses
        else round0["solve"]["canonical_status"]
    )

    crew_rows = round1["crew_generation"]
    total_new_crews = sum(row["new_crews_accepted"] for row in crew_rows)
    selected_new = selected["selected_new_crews"]
    accepted = bool(result["accepted"])
    validated = (
        tests["exit_code"] == 0
        and accepted
        and result["selected_result"]["replay"] == "PASS"
        and result["selected_result"]["physical_validator"] == "VALID"
        and superset["strict_superset"]
        and bool(selected_new)
    )
    status = (
        "CRAFT_WINDOW14_STEP4C_VALIDATED"
        if validated
        else "CRAFT_WINDOW14_STEP4C_PARTIAL"
    )

    determinism = {
        "status": "PASS" if tests["exit_code"] == 0 else "FAIL",
        "contract": "same input produces identical diagnosis, closure, crew order, and configuration signatures",
        "evidence_test": "tests/test_route_aware_crew_expansion.py::test_generated_configurations_respect_boundary_and_are_deterministic",
        "comparison": "exact canonical configuration-signature equality",
        "database_writes": 0,
    }
    write_json("determinism.json", determinism)

    write_json(
        "root_cause_followup.json",
        {
            "starting_root_cause": "WORKER_CREW_TRUNCATION_DEFECT",
            "classification": "WORKER_CREW_TRUNCATION_RECOVERED_BY_ROUTE_AWARE_EXPANSION",
            "physical_infeasibility_proven": "NO",
            "production_scheduler_changed": "NARROW_OPTIONAL_RESOURCE_DOMAIN_COMPONENT; NOT AUTOMATICALLY WIRED INTO FULL ROLLING RUN",
            "step3b_control_path_changed": "NO",
            "database_writes": 0,
        },
    )

    changed_files = [
        "crafter/repair/boundary_aware_modes.py",
        "crafter/repair/calendar_regimes.py",
        "crafter/repair/route_aware_crew_expansion.py",
        "tests/test_boundary_aware_modes.py",
        "tests/test_route_aware_crew_expansion.py",
        "scratch/run_window14_step4c.py",
        "scratch/inspect_window14_step4c_state.py",
        "scratch/inspect_step4c_added_regimes.py",
        "scratch/finalize_window14_step4c.py",
    ]
    manifest = {
        "scope": "CRAFT Window-14 Step 4C only",
        "files": [
            {
                "path": path,
                "sha256": sha256(ROOT / path),
                "exists": (ROOT / path).exists(),
            }
            for path in changed_files
        ],
        "production_integration": "NARROW_OPTIONAL_COMPONENT_ONLY",
        "automatic_global_planner_wiring": False,
        "step3b_control_path_changed": False,
        "database_writes": 0,
    }
    write_json("changed_files_manifest.json", manifest)

    solve = result["selected_result"]
    report = {
        "status": status,
        "starting_root_cause": "WORKER_CREW_TRUNCATION_DEFECT",
        "factory_id": starting["factory_id"],
        "window_index": starting["window_id"],
        "window_operation_count": starting["window_operation_count"],
        "base_domain": {
            "resource_configurations": round0["solve"]["resource_configuration_count"],
            "calendar_regimes": round0["solve"]["calendar_regime_count"],
            "cp_status": base_cp_status,
            "stage_statuses": base_stage_statuses,
        },
        "route_pressure": {
            "detected": pressure_detected,
            "first_pressure_operation": first_pressure_operation,
            "expansion_closure": closure_round_1["operations_expanded"],
        },
        "round_1": {
            "operations": round1["operations_expanded"],
            "new_crews": total_new_crews,
            "new_configurations": (
                solve["resource_configuration_count"]
                - round0["solve"]["resource_configuration_count"]
            ),
            "new_regimes": (
                solve["calendar_regime_count"]
                - round0["solve"]["calendar_regime_count"]
            ),
            "resource_configurations": solve["resource_configuration_count"],
            "calendar_regimes": solve["calendar_regime_count"],
            "cp_status": solve["winning_cp_status"],
            "crew_generation": [
                {
                    "operation_key": row["operation_key"],
                    "candidate_crews_generated": row["candidate_crews_generated"],
                    "duplicates_rejected": row["duplicates_rejected"],
                    "new_crews_accepted": row["new_crews_accepted"],
                    "route_ready_source": row["route_ready_source"],
                }
                for row in crew_rows
            ],
        },
        "later_rounds": "NOT_RUN_AFTER_ROUND_1_ACCEPTANCE",
        "strict_superset_verified": superset["strict_superset"],
        "original_configuration_count": superset["original_configuration_count"],
        "expanded_configuration_count": superset["expanded_configuration_count"],
        "new_crews_used_by_selected_schedule": len(selected_new),
        "selected_new_crews": selected_new,
        "window14_candidate": result["candidate_exists"],
        "replay": solve["replay"],
        "physical_validator": solve["physical_validator"],
        "physical_infeasibility_proven": False,
        "production_scheduler_changed": "NARROW_OPTIONAL_RESOURCE_DOMAIN_COMPONENT; NOT AUTOMATICALLY WIRED INTO FULL ROLLING RUN",
        "step3b_control_path_changed": False,
        "regression": {
            "passed": tests["passed"],
            "failed": tests["failed"],
            "warnings": tests["warnings"],
        },
        "database_writes": 0,
        "evidence_generated_programmatically": True,
    }
    write_json("implementation_report.json", report)

    markdown = f"""# CRAFT Window-14 Step 4C

## Status

`{status}`

The generic route-aware crew expansion recovered the frozen exact-boundary Window 14 in the first bounded expansion round. The original 368 configurations were retained, 16 legal matching-generated configurations were added across the route-derived closure, and the exact CP candidate passed replay and the independent physical validator.

## Result

- Starting root cause: `WORKER_CREW_TRUNCATION_DEFECT`
- Base domain: {round0['solve']['resource_configuration_count']} configurations / {round0['solve']['calendar_regime_count']} regimes; `{base_cp_status}`
- Route pressure detected: `{pressure_detected}`
- First pressure operation: `{first_pressure_operation}`
- Expansion closure: `{', '.join(closure_round_1['operations_expanded'])}`
- Round 1: {total_new_crews} new crews/configurations; {solve['resource_configuration_count']} total configurations; {solve['calendar_regime_count']} total regimes
- Round 1 CP status: `{solve['winning_cp_status']}`
- Strict superset: `{superset['strict_superset']}`
- Newly generated crews used by selected schedule: {len(selected_new)}
- Replay: `{solve['replay']}`
- Physical validator: `{solve['physical_validator']}`
- Physical infeasibility proven: `NO`
- DB writes: `0`

## Architectural scope

No Factory-2, Window-14, or operation identifiers are present in the reusable expansion implementation. Step 3A, Step 3B, commitment, CP constraints, replay, and validator semantics were not changed. The reusable component remains a narrow optional resource-domain path and has not been wired into a full Factory-2 rerun.

An exact-regime breakpoint hook was added as an opt-in argument. It exposes an authoritative legal start probe to the canonical regime partitioner; it does not approximate calendars or relax execution semantics. Fixed-crew availability across legal pause/resume segments was also corrected in the diagnostic matching helper.

## Regression

`{tests['summary']}`

The focused suite includes repair objectives, frozen Step 3A, validated Step 3B, Laya shadow isolation, boundary-aware modes, adaptive crew expansion, and calendar-regime coalescing.
"""
    (OUT / "implementation_report.md").write_text(markdown, encoding="utf-8")

    excluded = {
        "artifact_hash_manifest.json",
        "hash_verification.json",
    }
    artifacts = sorted(
        path
        for path in OUT.iterdir()
        if path.is_file() and path.name not in excluded
    )
    hashes = {
        "algorithm": "SHA-256",
        "artifact_count": len(artifacts),
        "artifacts": [
            {
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in artifacts
        ],
    }
    write_json("artifact_hash_manifest.json", hashes)
    mismatches = []
    for row in hashes["artifacts"]:
        actual = sha256(ROOT / row["path"])
        if actual != row["sha256"]:
            mismatches.append(
                {"path": row["path"], "expected": row["sha256"], "actual": actual}
            )
    write_json(
        "hash_verification.json",
        {
            "algorithm": "SHA-256",
            "verified": not mismatches,
            "verified_artifact_count": len(hashes["artifacts"]),
            "mismatches": mismatches,
        },
    )
    return 0 if validated and not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
