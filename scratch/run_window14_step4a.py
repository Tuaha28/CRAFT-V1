"""Frozen Factory-2 Window-14 Step-4A diagnostics.

This runner is intentionally diagnostic-only.  It reconstructs the persisted
Window-14 state from the accepted Step-4 windows and compares bounded legal
resource/neighborhood domains and diagnostic boundary projections.  It never
writes the database and never changes the rolling controller.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, ".")

import scratch.diagnose_window14_v3 as diag
from crafter.disruptions.benchmark import _load_state
from crafter.repair.planning import (
    PlanningContextV1,
    build_planning_state,
    select_precedence_safe_window,
)
from crafter.repair.temporal_escalation import TemporalEscalationPolicyV3

RUN = Path("outputs/crafter/factory2_172_v1")
OUT = RUN / "window14_step4a"
OUT.mkdir(parents=True, exist_ok=True)
diag.RUN = RUN
diag.OUT = OUT

W14_START = 326400
W14_END = 412800
W14_COMMIT = 346400
FACTORY_ID = 2


def write_json(path: Path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, default=str, sort_keys=True) + "\n")


def quiet_call(fn, *args, **kwargs):
    # Database and solver helpers log diagnostically; keep evidence files and
    # progress output machine-readable without changing their behavior.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def compact_result(result):
    return {
        "label": result.get("label"),
        "machine_cap": result.get("machine_cap"),
        "worker_cap": result.get("worker_cap"),
        "unrestricted_final_stage": result.get("unrestricted_final_stage"),
        "required_operation_count": result.get("required_operation_count"),
        "resource_configuration_count": result.get("resource_configuration_count"),
        "calendar_regime_count": result.get("calendar_regime_count"),
        "zero_regime_operation_keys": result.get("zero_regime_operation_keys", []),
        "resource_domain_summary": result.get("resource_domain_summary", {}),
        "candidate_exists": result.get("candidate_exists", False),
        "canonical_status": result.get("canonical_status"),
        "cp_status": result.get("cp_status"),
        "stages": result.get("stages", []),
        "scoped_acceptance": result.get("scoped_acceptance"),
        "probe": result.get("probe"),
        "prepare_seconds": result.get("prepare_seconds"),
        "canonical_seconds": result.get("canonical_seconds"),
        "cache_profile": result.get("cache_profile"),
    }


def make_context(window_id, selected):
    return PlanningContextV1(
        window_id,
        "ROLLING_PLAN",
        FACTORY_ID,
        W14_START,
        W14_START,
        W14_END,
        W14_COMMIT,
        tuple(selected),
        "FROZEN_BENCHMARK_RAW",
        snapshot_as_of_seconds=0,
        previous_provisional_plan_reference="provisional-013",
    )


def run_one(label, state, facts, keys, machine_cap, worker_cap, *, unrestricted=False, plan=None):
    print(f"START {label} ops={len(keys)} machine={machine_cap} worker={worker_cap}", flush=True)
    result = quiet_call(
        diag._run_case,
        label,
        state,
        facts,
        tuple(keys),
        int(machine_cap),
        int(worker_cap),
        unrestricted=bool(unrestricted),
        plan=plan,
    )
    out = compact_result(result)
    print(
        f"DONE {label} candidate={out['candidate_exists']} cp={out['cp_status']} "
        f"configs={out['resource_configuration_count']} regimes={out['calendar_regime_count']}",
        flush=True,
    )
    return out


def main():
    started = time.perf_counter()
    # The source loader is the same authoritative frozen benchmark loader used
    # by Step 4.  It is read-only; all evidence is written under the new
    # Step-4A directory.
    loaded = quiet_call(_load_state)
    problem, _operator_index, _rows, _durations, _details, route_index, _baseline, _checked, source = loaded
    windows, committed_rows = quiet_call(diag._accepted_rows)
    failure = json.loads((RUN / "window_014.json").read_text(encoding="utf-8"))
    selected = tuple(failure["window_acceptance"]["scope"]["missing_required_operations"])
    if len(selected) != 22:
        raise RuntimeError(f"persisted Window-14 required set changed: {len(selected)}")
    boundary, boundary_facts = quiet_call(diag._make_boundary, route_index, committed_rows, FACTORY_ID)
    base_context = make_context("window-014", selected)
    base_state = quiet_call(build_planning_state, loaded, base_context, boundary=boundary)

    # Admission/provenance reconstruction is included so the frozen case can
    # be independently checked rather than relying on a handwritten key list.
    _selected_again, admission = quiet_call(
        select_precedence_safe_window,
        problem,
        base_state["after_index"],
        base_state["classified"],
        make_context("window-014-probe", ()),
        committed_operation_keys=boundary.committed_operation_keys,
        maximum_operations=22,
        boundary=boundary,
    )
    deferred = {
        str(row.get("operation_key")): row
        for row in admission
        if row.get("reason") == "BOUNDARY_DEFERRED"
    }
    frozen = {
        "source": str(source),
        "factory_id": FACTORY_ID,
        "window_id": "window-014",
        "snapshot_as_of_seconds": 0,
        "window_start_seconds": W14_START,
        "window_end_seconds": W14_END,
        "commit_end_seconds": W14_COMMIT,
        "initial_step4_status": "CRAFT_FACTORY2_172_V1_BLOCKED",
        "accepted_prior_window_count": len(windows),
        "committed_operation_count": len(committed_rows),
        "committed_operation_keys": sorted(committed_rows),
        "required_operation_keys": sorted(selected),
        "deferred_operation_keys": sorted(deferred),
        "deferred_readiness": deferred,
        "boundary_counts": {
            "machine": len(boundary.machine_commitments),
            "worker": len(boundary.worker_commitments),
            "routing": len(boundary_facts),
        },
        "boundary_provenance": "accepted Step-4 windows 001-013; exact carried boundary reconstruction",
        "database_writes": 0,
    }
    write_json(OUT / "frozen_state.json", frozen)

    default_plan = None  # _run_case's default is the exact four-stage baseline.
    unrestricted_plan = TemporalEscalationPolicyV3().plans[1]

    baseline = run_one("BASELINE_22_1X1_BOUNDED", base_state, boundary_facts, selected, 1, 1)
    write_json(OUT / "baseline_reproduction.json", {
        "expected": {"operations": 22, "cp_statuses": ["INFEASIBLE"] * 4, "resource_domain_truncated": True},
        "observed": baseline,
        "reproduction_match": (
            baseline["required_operation_count"] == 22
            and baseline["candidate_exists"] is False
            and [x.get("status") for x in baseline.get("stages", [])] == ["INFEASIBLE"] * 4
            and bool(baseline.get("resource_domain_summary", {}).get("resource_domain_truncated"))
        ),
        "database_writes": 0,
    })

    # A: fixed 22-operation neighborhood, bounded canonical time scope.
    resource_path = OUT / "resource_domain_experiments.jsonl"
    if resource_path.exists():
        resource_path.unlink()
    resource_cases = [("22_1X1_BOUNDED", 1, 1), ("22_3X1_BOUNDED", 3, 1),
                      ("22_1X3_BOUNDED", 1, 3), ("22_3X3_BOUNDED", 3, 3)]
    resource_results = []
    for label, machine_cap, worker_cap in resource_cases:
        out = baseline if label == "22_1X1_BOUNDED" else run_one(
            label, base_state, boundary_facts, selected, machine_cap, worker_cap)
        resource_results.append(out)
        append_jsonl(resource_path, out)

    # B: deterministic readiness/precedence selection with the same exact
    # boundary and baseline compact domain.  Actual selected counts are used;
    # numeric caps are targets, never fabricated operation sets.
    neighborhood_path = OUT / "neighborhood_experiments.jsonl"
    if neighborhood_path.exists():
        neighborhood_path.unlink()
    neighborhood_results = []
    for target in (22, 35, 50, 70):
        probe_context = make_context(f"window-014-cap-{target}", ())
        chosen, provenance = quiet_call(
            select_precedence_safe_window,
            problem,
            base_state["after_index"],
            base_state["classified"],
            probe_context,
            committed_operation_keys=boundary.committed_operation_keys,
            maximum_operations=target,
            boundary=boundary,
        )
        chosen = tuple(sorted(chosen))
        context = make_context(f"window-014-cap-{target}", chosen)
        state = quiet_call(build_planning_state, loaded, context, boundary=boundary)
        out = run_one(f"NEIGHBORHOOD_TARGET_{target}_ACTUAL_{len(chosen)}_1X1_BOUNDED",
                      state, boundary_facts, chosen, 1, 1)
        out.update({"target_neighborhood_size": target,
                    "actual_selected_operation_count": len(chosen),
                    "selected_operation_keys": list(chosen),
                    "admission_provenance": provenance})
        neighborhood_results.append(out)
        append_jsonl(neighborhood_path, out)

    # C: run a small combined set only where the fixed-domain probes did not
    # recover a candidate.  It is deliberately bounded and auditable.
    combined_path = OUT / "combined_experiments.jsonl"
    if combined_path.exists():
        combined_path.unlink()
    combined_results = []
    for target in (35, 50, 70):
        probe_context = make_context(f"window-014-combined-{target}", ())
        chosen, provenance = quiet_call(
            select_precedence_safe_window,
            problem,
            base_state["after_index"],
            base_state["classified"],
            probe_context,
            committed_operation_keys=boundary.committed_operation_keys,
            maximum_operations=target,
            boundary=boundary,
        )
        chosen = tuple(sorted(chosen))
        context = make_context(f"window-014-combined-{target}", chosen)
        state = quiet_call(build_planning_state, loaded, context, boundary=boundary)
        out = run_one(f"COMBINED_TARGET_{target}_ACTUAL_{len(chosen)}_3X3_BOUNDED",
                      state, boundary_facts, chosen, 3, 3)
        out.update({"target_neighborhood_size": target,
                    "actual_selected_operation_count": len(chosen),
                    "selected_operation_keys": list(chosen),
                    "admission_provenance": provenance})
        combined_results.append(out)
        append_jsonl(combined_path, out)

    # Boundary projections are diagnostic-only.  A candidate here cannot be
    # promoted because it omits authoritative commitments.
    boundary_path = OUT / "boundary_ablations.jsonl"
    if boundary_path.exists():
        boundary_path.unlink()
    boundary_cases = {
        "EXACT_CARRIED_BOUNDARY": (True, True, True),
        "REMOVE_WORKER_BOUNDARY": (True, False, True),
        "REMOVE_MACHINE_BOUNDARY": (False, True, True),
        "REMOVE_ROUTING_BOUNDARY": (True, True, False),
        "REMOVE_MACHINE_WORKER_BOUNDARY": (False, False, True),
        "REMOVE_ALL_RELEVANT_BOUNDARY": (False, False, False),
    }
    boundary_results = []
    for label, (keep_machine, keep_worker, keep_routing) in boundary_cases.items():
        variant, facts = diag._boundary_variant(
            boundary, boundary_facts,
            keep_machine=keep_machine,
            keep_worker=keep_worker,
            keep_routing=keep_routing,
        )
        state = quiet_call(build_planning_state, loaded, base_context, boundary=variant)
        out = run_one(label + "_1X3_UNRESTRICTED_DIAGNOSTIC", state, facts, selected,
                      1, 3, unrestricted=True, plan=unrestricted_plan)
        out.update({"keep_machine": keep_machine, "keep_worker": keep_worker,
                    "keep_routing": keep_routing, "accepted_for_production": False,
                    "diagnostic_only": True})
        boundary_results.append(out)
        append_jsonl(boundary_path, out)

    # The legal worker domain has millions of possible crews for several
    # operations; complete enumeration is not computationally practical for
    # this 22-op local state.  The 3x3 unrestricted run is retained as the
    # largest bounded diagnostic reference, not mislabelled as an oracle.
    best_combined = combined_results[-1] if combined_results else resource_results[-1]
    write_json(OUT / "complete_domain_diagnostic.json", {
        "status": "ORACLE_NOT_COMPLETED",
        "reason": "complete worker-crew enumeration is combinatorial (legal crew capacities include millions); bounded 3x3 is not complete",
        "largest_bounded_reference": best_combined,
        "legal_domain_evidence": baseline.get("resource_domain_summary", {}),
        "database_writes": 0,
    })

    # Existing Step-3B decision classification is observational in the Step-4
    # harness: selection/solve branches in run_one_factory_craft.py do not
    # execute horizon/fast-forward/neighborhood actions.
    step3b = {
        "status": "PARTIALLY_OBSERVATIONAL",
        "evidence": [
            "scratch/run_factory2_172_step4.py wraps selector to call decide_rolling_orchestration_v2 for telemetry",
            "scratch/run_one_factory_craft.py invokes select_precedence_safe_window and solve directly",
            "GlobalCRAFTPlanner.plan_factory_windows does not dispatch all six Step-3B actions",
        ],
        "database_writes": 0,
    }
    write_json(OUT / "step3b_control_path_audit.json", step3b)

    # Structural summary.  Exact-boundary candidates are the only candidates
    # eligible for production acceptance; all projections are explicitly
    # diagnostic-only.
    exact_boundary = next(x for x in boundary_results if x["label"].startswith("EXACT_"))
    any_exact_valid = any(
        bool(x.get("scoped_acceptance", {}).get("accepted"))
        for x in resource_results + neighborhood_results + combined_results
    )
    root = {
        "classification": "UNKNOWN",
        "first_proven_blocker": "WINDOW14_EXACT_BOUNDARY_REMAINS_INFEASIBLE_WITH_TRUNCATED_RESOURCE_DOMAIN",
        "resource_domain_truncated": True,
        "physical_infeasibility_proven": False,
        "exact_boundary_candidate_accepted": bool(exact_boundary.get("scoped_acceptance", {}).get("accepted")),
        "any_exact_boundary_valid_candidate": any_exact_valid,
        "boundary_ablation_summary": [
            {"label": x["label"], "candidate_exists": x["candidate_exists"],
             "cp_status": x["cp_status"], "accepted_for_production": False}
            for x in boundary_results
        ],
        "resource_summary": [
            {"label": x["label"], "candidate_exists": x["candidate_exists"],
             "cp_status": x["cp_status"], "configs": x["resource_configuration_count"]}
            for x in resource_results
        ],
        "neighborhood_summary": [
            {"label": x["label"], "actual_count": x["actual_selected_operation_count"],
             "candidate_exists": x["candidate_exists"], "cp_status": x["cp_status"]}
            for x in neighborhood_results
        ],
        "combined_summary": [
            {"label": x["label"], "actual_count": x["actual_selected_operation_count"],
             "candidate_exists": x["candidate_exists"], "cp_status": x["cp_status"]}
            for x in combined_results
        ],
        "why_not_physical_infeasibility": "legal worker alternatives and complete domain are not exhaustively represented",
        "database_writes": 0,
    }
    write_json(OUT / "conflict_analysis.json", {
        "status": "UNRESOLVED_TRUNCATED_DOMAIN",
        "evidence": root,
        "diagnostic_limitations": [
            "resource candidate sets are bounded by deterministic caps",
            "complete worker-crew enumeration was not attempted because capacities are combinatorial",
            "boundary projections are not physically authoritative",
        ],
        "database_writes": 0,
    })
    write_json(OUT / "root_cause.json", root)
    write_json(OUT / "proposed_adaptive_policy.json", {
        "status": "PROPOSED_NOT_IMPLEMENTED",
        "policy": "retain 22-operation normal admission; if exact bounded stages fail while legal alternatives remain, issue a targeted resource/neighborhood expansion request; do not claim physical infeasibility until complete relevant domains/boundaries are represented",
        "calibration_required": True,
        "no_window_or_operation_id_special_case": True,
        "database_writes": 0,
    })
    write_json(OUT / "schedule_quality_comparison.json", {
        "status": "NO_VALID_EXACT_BOUNDARY_CANDIDATE",
        "note": "quality metrics are reported only for physically valid exact-boundary candidates; diagnostic boundary projections are excluded",
        "resource_results": resource_results,
        "neighborhood_results": neighborhood_results,
        "combined_results": combined_results,
        "database_writes": 0,
    })
    report = {
        "status": "CRAFT_WINDOW14_STEP4A_PARTIAL",
        "classification": root["classification"],
        "baseline_reproduction": baseline["candidate_exists"] is False,
        "step3b_control_path_status": step3b["status"],
        "resource_experiments": len(resource_results),
        "neighborhood_experiments": len(neighborhood_results),
        "combined_experiments": len(combined_results),
        "boundary_ablations": len(boundary_results),
        "complete_domain": "ORACLE_NOT_COMPLETED",
        "physical_infeasibility_proven": False,
        "database_writes": 0,
        "elapsed_seconds": time.perf_counter() - started,
    }
    write_json(OUT / "implementation_report.json", report)
    (OUT / "implementation_report.md").write_text(
        "# Factory-2 Window-14 Step-4A\n\n"
        f"Status: `{report['status']}`\n\n"
        f"The exact persisted 22-operation failure was reconstructed with {len(committed_rows)} committed operations. "
        "Bounded resource/neighborhood probes and diagnostic boundary projections were run without DB writes. "
        "The legal worker domain remains truncated, so physical infeasibility is not claimed.\n\n"
        f"Step-3B control path: `{step3b['status']}`.\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
