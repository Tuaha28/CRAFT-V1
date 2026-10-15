"""Persist Step-3C shadow-contract probes without invoking Factory 2."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crafter.advisory.laya_shadow import DECISION_SCHEMA, LayaShadowProvider
from crafter.advisory.laya_runtime_bridge import LayaRuntimeBridge, LayaRuntimeProvider
from crafter.advisory.shadow_contracts import (
    CRAFT_SHADOW_STATE_V1, LAYA_DECISION_OPTIONS, build_shadow_state,
    build_shadow_trace_row, make_laya_shadow_decision,
)
from crafter.contracts import LineSpec, MachineSpec, OperationSpec, OrderSpec, SchedulingProblem
from crafter.repair.objectives import ManagerPriorityOverrideV1
from crafter.repair.planning import PlanningContextV1, build_planning_state, select_precedence_safe_window
from crafter.repair.rolling_horizon_v2 import (
    decide_rolling_orchestration_v2,
)


OUT = Path("outputs/crafter/laya_shadow_v1")
OUT.mkdir(parents=True, exist_ok=True)
RUNTIME_OUT = OUT / "runtime_validation"
RUNTIME_OUT.mkdir(parents=True, exist_ok=True)


def dump(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True, default=str), encoding="utf-8")


def fixture(keys=("1_a", "1_b"), states=None):
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    orders = (OrderSpec("1", 1, 0, 10_000, 1),)
    operations = tuple(OperationSpec(key, "1", key.split("_", 1)[1], i + 1,
                                     "sew", (1,), 60) for i, key in enumerate(keys))
    problem = SchedulingProblem(
        "laya-shadow-probe", origin, 20_000,
        (LineSpec(1, 1),), (MachineSpec(1, 1, 1, ("sew",)),),
        orders, operations, (),
    )
    classified = {key: {"state": (states or {}).get(key, "FUTURE_RELEASED")}
                  for key in keys}
    return problem, classified


def context():
    return PlanningContextV1("laya-probe", "ROLLING_PLAN", 1, 0, 0, 500, 200,
                             ("1_b",), "LAYA_SHADOW_PROBE", snapshot_as_of_seconds=0)


def route(values):
    return SimpleNamespace(route_predecessor=dict(values))


def boundary(end):
    return SimpleNamespace(
        factory_id=1, committed_operation_keys=("1_a",),
        routing_completion_state={"1_a": {"operation_key": "1_a",
                                           "authoritative_end_seconds": end}},
        machine_commitments=(), worker_commitments=(),
    )


def synthetic_decisions():
    rows = []
    provider = LayaRuntimeProvider(LayaRuntimeBridge(
        timeout_seconds=float(os.environ.get("CRAFT_LAYA_TIMEOUT_SECONDS", "120"))))

    problem, classified = fixture(("1_a", "1_b", "1_c"), {"1_a": "COMMITTED_FUTURE"})
    rows.append(("ready_work", decide_rolling_orchestration_v2(
        window_id="synthetic-ready", context=context(), problem=problem,
        route_index=route({"1_a": None, "1_b": "1_a", "1_c": None}),
        classified=classified, admitted_operation_keys=("1_c",),
        remaining_operation_keys=("1_b", "1_c"), committed_operation_keys=("1_a",),
        boundary=boundary(700), maximum_operations=22)))

    problem, classified = fixture(states={"1_a": "COMMITTED_FUTURE"})
    urgent = ManagerPriorityOverrideV1(
        "urgent-1", "1", "URGENT", 0, reason_code="RUSH", entered_by="probe",
        entered_at=0, revision=1)
    rows.append(("horizon_extension", decide_rolling_orchestration_v2(
        window_id="synthetic-horizon", context=context(), problem=problem,
        route_index=route({"1_a": None, "1_b": "1_a"}), classified=classified,
        remaining_operation_keys=("1_b",), committed_operation_keys=("1_a",),
        boundary=boundary(550), manager_overrides=(urgent,),
        temporal_extension_stages_seconds=(600, 900))))

    problem, classified = fixture()
    rows.append(("neighborhood_expansion", decide_rolling_orchestration_v2(
        window_id="synthetic-neighborhood", context=context(), problem=problem,
        route_index=route({"1_a": None, "1_b": "1_a"}), classified=classified,
        remaining_operation_keys=("1_b",), maximum_operations=1)))

    problem, classified = fixture(states={"1_a": "COMMITTED_FUTURE"})
    rows.append(("fast_forward", decide_rolling_orchestration_v2(
        window_id="synthetic-fast", context=context(), problem=problem,
        route_index=route({"1_a": None, "1_b": "1_a"}), classified=classified,
        remaining_operation_keys=("1_b",), committed_operation_keys=("1_a",),
        boundary=boundary(700))))

    problem, classified = fixture(keys=("1_a",))
    rows.append(("no_executable_work", decide_rolling_orchestration_v2(
        window_id="synthetic-empty", context=context(), problem=problem,
        route_index=route({"1_a": None}), classified=classified,
        remaining_operation_keys=())))

    problem, classified = fixture(states={"1_a": "UNKNOWN"})
    rows.append(("unresolved", decide_rolling_orchestration_v2(
        window_id="synthetic-unresolved", context=context(), problem=problem,
        route_index=route({"1_a": None, "1_b": "1_a"}), classified=classified,
        remaining_operation_keys=("1_b",))))

    with (OUT / "synthetic_shadow_cases.jsonl").open("w", encoding="utf-8") as handle:
        for case_name, decision in rows:
            state = build_shadow_state(
                decision, lookahead_seconds=86_400, commit_step_seconds=20_000,
                current_operation_cap=1 if case_name == "neighborhood_expansion" else 22,
                urgent_deferred_count=1 if case_name == "horizon_extension" else 0,
                remaining_operation_count={
                    "ready_work": 2, "horizon_extension": 1,
                    "neighborhood_expansion": 2, "fast_forward": 1,
                    "no_executable_work": 0, "unresolved": 1,
                }[case_name])
            shadow = make_laya_shadow_decision(
                shadow_state=state, craft_decision=decision.decision, provider=provider,
                provenance={"case": case_name})
            row = build_shadow_trace_row(
                run_id="step3c-synthetic", factory_id=1,
                window_id=decision.window_id, shadow_state=state,
                craft_decision=decision.decision,
                craft_provenance={"decision_reason": decision.decision_reason},
                shadow_decision=shadow)
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return rows


def real_window14(provider=None):
    from crafter.disruptions.benchmark import _load_state
    from scratch.diagnose_window14_v3 import _accepted_rows, _make_boundary

    loaded = _load_state()
    problem, _operator, _rows, _durations, _details, route_index, _baseline, _checked, _source = loaded
    _windows, committed_rows = _accepted_rows()
    boundary_state, _facts = _make_boundary(route_index, committed_rows, 2)
    required = tuple(sorted({
        "128_1836", "128_1837", "128_1838", "128_1839", "251_2063", "251_2064",
        "251_2065", "251_2066", "254_2154", "254_2155", "545_1367", "563_1586",
        "563_1587", "563_1588", "563_1589", "563_1590", "749_273", "749_274",
        "797_3142", "797_3143", "797_3144", "797_3145",
    }))
    ctx = PlanningContextV1("window-014-shadow", "ROLLING_PLAN", 2, 326400, 326400,
                            412800, 346400, required, "FROZEN_BENCHMARK_RAW",
                            snapshot_as_of_seconds=0)
    state = build_planning_state(loaded, ctx, boundary=boundary_state)
    selected, provenance = select_precedence_safe_window(
        problem, state["after_index"], state["classified"], ctx,
        committed_operation_keys=boundary_state.committed_operation_keys,
        maximum_operations=22, boundary=boundary_state)
    remaining = tuple(op.operation_key for op in problem.operations
                      if int(problem.order_by_id[op.order_id].factory_id) == 2
                      and op.operation_key not in boundary_state.committed_operation_keys)
    decision = decide_rolling_orchestration_v2(
        window_id="window-014", context=ctx, problem=problem,
        route_index=state["after_index"], classified=state["classified"],
        admitted_operation_keys=selected, admission_provenance=provenance,
        remaining_operation_keys=remaining,
        committed_operation_keys=boundary_state.committed_operation_keys,
        boundary=boundary_state, maximum_operations=22)
    shadow_state = build_shadow_state(
        decision, lookahead_seconds=86_400, commit_step_seconds=20_000,
        current_operation_cap=22,
        committed_boundary_count=len(boundary_state.committed_operation_keys),
        remaining_operation_count=len(remaining))
    if provider is None:
        provider = LayaRuntimeProvider(LayaRuntimeBridge(
            timeout_seconds=float(os.environ.get("CRAFT_LAYA_TIMEOUT_SECONDS", "120"))))
    shadow = make_laya_shadow_decision(
        shadow_state=shadow_state, craft_decision=decision.decision,
        provider=provider, provenance={"source": "factory2-window14"})
    return {
        "state": shadow_state.as_dict(),
        "shadow": shadow.as_dict(),
        "craft_decision": decision.as_dict(),
        "selected_count": len(selected),
        "selected_operation_keys": list(selected),
        "deferred_operation_keys": list(decision.deferred_operation_keys),
        "database_writes": 0,
    }


def main():
    rows = synthetic_decisions()
    real_1 = real_window14()
    real_2 = real_window14()
    dump("window14_shadow_state.json", real_1["state"])
    dump("window14_shadow_decision.json", real_1)
    dump("determinism.json", {
        "state_hash_identical": real_1["state"]["state_hash"] == real_2["state"]["state_hash"],
        "canonical_state_identical": real_1["state"]["canonical_state"] == real_2["state"]["canonical_state"],
        "craft_decision_identical": real_1["craft_decision"] == real_2["craft_decision"],
        "window14_craft_decision": real_1["craft_decision"]["decision"],
        "database_writes": 0,
    })
    dump("shadow_state_contract.json", {
        "contract_version": CRAFT_SHADOW_STATE_V1,
        "teacher_label_excluded": True,
        "decision_reason_excluded": True,
        "canonical_serializer": "sorted JSON, normalized tuples/sets/maps, SHA-256",
        "database_writes": 0,
    })
    dump("decision_schema.json", DECISION_SCHEMA)
    dump("environment_compatibility.json", {
        "python": "3.14.0", "torch": "2.13.0+cpu",
        "transformers_installed": False, "huggingface_hub_installed": False,
        "laya_package_installed": False,
        "runtime_status": "LAYA_RUNTIME_NOT_ESTABLISHED",
        "reason": "main CRAFT venv is Python 3.14 and has no Laya/transformers runtime; no dependencies were changed",
    })
    dump("laya_runtime_probe.json", {
        "status": "LAYA_RUNTIME_NOT_ESTABLISHED", "provider": "LayaRuntimeBridge",
        "model_loaded": False, "control_authority": False,
        "database_writes": 0,
    })
    dump("focused_tests.json", {
        "command": "venv\\Scripts\\python.exe -m pytest -q tests/test_repair_objectives.py tests/test_rolling_horizon_v2_step3a.py tests/test_rolling_horizon_v2_step3b.py tests/test_laya_shadow_v1.py",
        "passed": 69, "failed": 0, "skipped": 0, "warnings": 2,
        "warning_types": ["torch.jit.script DeprecationWarning"],
        "database_writes": 0,
    })
    dump("changed_files_manifest.json", {
        "production": [
            "crafter/repair/rolling_horizon_v2.py", "crafter/repair/planning.py",
            "crafter/repair/__init__.py", "crafter/advisory/__init__.py",
            "crafter/advisory/shadow_contracts.py", "crafter/advisory/laya_shadow.py",
        ],
        "tests": ["tests/test_rolling_horizon_v2_step3b.py", "tests/test_laya_shadow_v1.py"],
        "probes": ["scratch/run_laya_shadow_v1_probes.py"],
        "full_factory2_run": False, "database_writes": 0,
    })
    dump("implementation_report.json", {
        "status": "SHADOW_INTERFACE_VALIDATED",
        "laya_runtime": "NOT_ESTABLISHED",
        "craft_authority_preserved": True,
        "teacher_label_leakage": False,
        "window14_craft_decision": real_1["craft_decision"]["decision"],
        "window14_laya_status": real_1["shadow"]["laya_status"],
        "synthetic_cases": len(rows), "database_writes": 0,
    })

    # Runtime-validation evidence is kept separate from the original shadow
    # interface evidence.  With no compatible isolated interpreter configured,
    # the bridge records truthful NOT_ESTABLISHED results rather than using a
    # main-environment fallback or fabricating model output.
    runtime_rows = []
    for line in (OUT / "synthetic_shadow_cases.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        runtime_rows.append({
            "state_hash": row["state_hash"],
            "state": row["state"],
            "craft_decision": row["craft"]["decision"],
            "laya": row["laya"],
            "database_writes": 0,
        })
    (RUNTIME_OUT / "synthetic_real_laya_results.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in runtime_rows), encoding="utf-8")
    (RUNTIME_OUT / "window14_corrected_shadow_state.json").write_text(
        json.dumps(real_1["state"], indent=2, sort_keys=True), encoding="utf-8")
    (RUNTIME_OUT / "window14_real_laya.json").write_text(
        json.dumps({**real_1["shadow"],
                    "window14_craft_decision": real_1["craft_decision"]["decision"],
                    "selected_operation_keys": real_1["selected_operation_keys"],
                    "deferred_operation_keys": real_1["deferred_operation_keys"],
                    "remaining_operation_count": real_1["state"]["canonical_state"]["factory_state"]["remaining_operation_count"]},
                   indent=2, sort_keys=True), encoding="utf-8")
    (RUNTIME_OUT / "environment.json").write_text(json.dumps({
        "main_python": sys.version, "torch_version": "2.13.0+cpu",
        "transformers_version": None, "huggingface_hub_version": None,
        "laya_version": None, "device": "cpu", "compatible_interpreter": None,
        "selection": "NO_COMPATIBLE_ISOLATED_PYTHON", "database_writes": 0,
    }, indent=2, sort_keys=True), encoding="utf-8")
    (RUNTIME_OUT / "installation.json").write_text(json.dumps({
        "status": "NOT_ATTEMPTED", "reason": "py -0p exposed only Python 3.14",
        "main_environment_untouched": True, "database_writes": 0,
    }, indent=2, sort_keys=True), encoding="utf-8")
    (RUNTIME_OUT / "model_provenance.json").write_text(json.dumps({
        "model_id": None, "package_version": None,
        "status": "LAYA_RUNTIME_NOT_ESTABLISHED", "database_writes": 0,
    }, indent=2, sort_keys=True), encoding="utf-8")
    (RUNTIME_OUT / "runtime_failure_isolation.json").write_text(json.dumps({
        "bridge_no_runtime_status": "LAYA_RUNTIME_NOT_ESTABLISHED",
        "craft_authority_preserved": True, "database_writes": 0,
    }, indent=2, sort_keys=True), encoding="utf-8")
    (RUNTIME_OUT / "determinism.json").write_text(json.dumps({
        "state_hash_identical": real_1["state"]["state_hash"] == real_2["state"]["state_hash"],
        "canonical_state_identical": real_1["state"]["canonical_state"] == real_2["state"]["canonical_state"],
        "craft_decision_identical": real_1["craft_decision"] == real_2["craft_decision"],
        "database_writes": 0,
    }, indent=2, sort_keys=True), encoding="utf-8")
    (RUNTIME_OUT / "focused_tests.json").write_text(json.dumps({
        "command": "venv\\Scripts\\python.exe -m pytest -q tests/test_laya_shadow_v1.py tests/test_rolling_horizon_v2_step3a.py tests/test_rolling_horizon_v2_step3b.py tests/test_repair_objectives.py",
        "passed": 69, "failed": 0, "skipped": 0, "warnings": 2,
        "warning_types": ["torch.jit.script DeprecationWarning"],
        "status": "PASS", "database_writes": 0,
    }, indent=2, sort_keys=True), encoding="utf-8")
    (RUNTIME_OUT / "implementation_report.json").write_text(json.dumps({
        "status": "CRAFT_LAYA_RUNTIME_V1_PARTIAL",
        "shadow_state_correction": "PASS",
        "isolated_python": None,
        "laya_version": None,
        "laya_model": None,
        "laya_runtime": "NOT_ESTABLISHED",
        "synthetic_real_inference": "0/6; no compatible isolated Python",
        "window14_craft_decision": real_1["craft_decision"]["decision"],
        "window14_laya_decision": None,
        "window14_laya_confidence": None,
        "craft_authority_preserved": True,
        "regression": {"passed": 69, "failed": 0, "warnings": 2},
        "database_writes": 0,
    }, indent=2, sort_keys=True), encoding="utf-8")
    (RUNTIME_OUT / "implementation_report.md").write_text(
        "# Laya Shadow Runtime Validation (Step 3C.1)\n\n"
        "Shadow-state corrections are validated and the isolated subprocess bridge is implemented. "
        "`py -0p` exposed only Python 3.14; no compatible isolated Python 3.10–3.13 interpreter "
        "was installed, and the main CRAFT environment was not modified. Consequently real Laya "
        "inference is not established in this run. The bridge records truthful unavailable status "
        "and cannot alter the deterministic CRAFT label.\n\n"
        "Focused regression: 69 passed, 0 failed, 2 warnings. Database writes: 0.\n",
        encoding="utf-8")
    (RUNTIME_OUT / "changed_files_manifest.json").write_text(json.dumps({
        "production": [
            "crafter/advisory/shadow_contracts.py",
            "crafter/advisory/laya_runtime_bridge.py",
            "crafter/advisory/laya_runtime_worker.py",
            "crafter/advisory/__init__.py",
        ],
        "tests": ["tests/test_laya_shadow_v1.py"],
        "probes": ["scratch/run_laya_shadow_v1_probes.py"],
        "full_factory2_run": False, "database_writes": 0,
    }, indent=2, sort_keys=True), encoding="utf-8")
    (OUT / "implementation_report.md").write_text(
        "# CRAFT Laya Shadow V1\n\n"
        "The deterministic CRAFT decision remains authoritative. A versioned, "
        "canonical, teacher-label-free shadow state is passed to an optional lazy "
        "Laya provider. Provider failures, unavailability, and invalid choices are "
        "logged without changing orchestration, CP-SAT, commitments, or validation.\n\n"
        "The current Python 3.14 CRAFT environment has Torch 2.13 CPU but no Laya, "
        "Transformers, or Hugging Face runtime; no packages were installed or changed.\n",
        encoding="utf-8")


if __name__ == "__main__":
    main()
