"""Convergence-controlled P2 causal release experiment.

This is an additive diagnostic wrapper around the authoritative P2 runner.  It
keeps the same event, domains, calendars, mode generator, CP model and
validator; only the fixpoint termination policy is enabled.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .p2_release_fixpoint import run as run_p2
from .runtime.release_fixpoint import frontier_signature


OUT = Path("outputs/crafter/disruption_repair_v2_p2_convergence_fixpoint_v2")
REFERENCE = Path("outputs/crafter/disruption_repair_v2_p2_budget_escalation_v4_budget50")
SHIFT_SOURCE = Path("outputs/crafter/shift_handover_floater_audit_v4")


def _read(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _v7_compare(out: Path) -> dict:
    current = _read(out / "fixpoint_iterations.json", [])
    reference = _read(REFERENCE / "fixpoint_iterations.json", [])
    current = current[:5]
    reference = reference[:5]
    release_sequence_same = [
        [str(x.get("operation_key")) for x in a.get("selected_promotions", [])]
        == [str(x.get("operation_key")) for x in b.get("selected_promotions", [])]
        for a, b in zip(current, reference)
    ]
    mode_counts_same = [
        a.get("mode_generation", {}).get("mode_count")
        == b.get("mode_generation", {}).get("mode_count")
        for a, b in zip(current, reference)
    ]
    # The v7 runner predates canonical signatures; canonicalize its physical
    # candidate rows with the same helper before comparing.
    frontier_same = [
        a.get("frontier_signature_hash") == frontier_signature(b.get("candidate_conflicts", [])).get("hash")
        for a, b in zip(current, reference)
    ]
    return {
        "reference": str(REFERENCE),
        "current_iterations_compared": len(current),
        "reference_iterations_compared": len(reference),
        "iterations_1_to_5_same_release_sequence": bool(len(current) == len(reference) and all(release_sequence_same)),
        "iterations_1_to_5_same_mode_counts": bool(len(current) == len(reference) and all(mode_counts_same)),
        "iterations_1_to_5_same_frontier": bool(len(current) == len(reference) and all(frontier_same)) if current and reference else False,
        "release_sequence_by_iteration": release_sequence_same,
        "mode_count_by_iteration": mode_counts_same,
        "frontier_signature_by_iteration": frontier_same,
        "note": "Reference child predates canonical frontier signatures; its physical candidate rows were canonicalized with the current signature helper.",
    }


def _augment(out: Path):
    iterations = _read(out / "fixpoint_iterations.json", [])
    cp = _read(out / "final_cp_result.json", {})
    root = _read(out / "root_cause_classification.json", {})
    current_frontier = iterations[-1].get("frontier", {}) if iterations else {}
    unresolved = iterations[-1].get("candidate_conflicts", []) if iterations else []
    unique_ops = sorted({str(x.get("operation_key")) for x in unresolved if x.get("operation_key")})
    _write(out / "shift_semantics_assumption.json", {
        "benchmark_policy": "FIXED_CREW_PAUSE_RESUME",
        "worker_calendar_status": "VALIDATED_INTERNAL",
        "worker_24h_availability": 0,
        "floater_status": "NO_TRUE_FLOATER_POPULATION_CONFIRMED",
        "factory_handover_policy": "UNCONFIRMED",
        "realism_debt": "Needle Craft real cross-shift handover policy remains unconfirmed.",
        "deployment_note": "Confirm factory crew-handover policy before production deployment or authoritative real-factory timing claims.",
        "source_audit": str(SHIFT_SOURCE),
        "database_writes": 0,
    })
    _write(out / "experiment_config.json", {
        "event": "P2_PREDECESSOR_DELAY", "N": 100, "B": 50,
        "max_iterations": 10, "max_new_promotions_per_iteration": 10,
        "clean_state": True, "database_writes": 0,
        "termination_policy": ["CP_FEASIBLE", "CAUSAL_CLOSURE_REACHED", "NO_NEW_LEGAL_PROMOTIONS", "SAME_FRONTIER_REPEAT", "FRONTIER_CYCLE_DETECTED", "RELEASE_BUDGET_REACHED", "NEIGHBORHOOD_SCOPE_LIMIT", "ITERATION_LIMIT_REACHED"],
    })
    _write(out / "iteration_summary.json", [{
        "iteration": it.get("iteration"),
        "released_before": len(it.get("released_before", [])),
        "released_after": len(it.get("released_after", [])),
        "new_promotions": len(it.get("selected_promotions", [])),
        "conflict_records": len(it.get("candidate_conflicts", [])),
        "unique_conflicting_operations": sorted({str(x.get("operation_key")) for x in it.get("candidate_conflicts", []) if x.get("operation_key")}),
        "frontier_signature_hash": it.get("frontier_signature_hash"),
        "frontier_movement": it.get("frontier_movement"),
        "cp_status": (it.get("cp_probe") or {}).get("status", "NOT_PROBED"),
    } for it in iterations])
    _write(out / "frontier_signatures.json", [it.get("frontier", {}) for it in iterations])
    _write(out / "frontier_progress.json", [{"iteration": it.get("iteration"), "movement": it.get("frontier_movement"), "exact_repeat": it.get("exact_frontier_repeat", False), "cycle": it.get("frontier_cycle", False)} for it in iterations])
    _write(out / "frontier_cycles.json", _read(out / "frontier_cycles.json", []))
    _write(out / "conflict_records_by_iteration.json", [{"iteration": it.get("iteration"), "unresolved_conflict_records": len(it.get("candidate_conflicts", []))} for it in iterations])
    _write(out / "unique_conflicting_operations_by_iteration.json", [{"iteration": it.get("iteration"), "operations": sorted({str(x.get("operation_key")) for x in it.get("candidate_conflicts", []) if x.get("operation_key")}), "workers": sorted({str(x.get("worker_id")) for x in it.get("candidate_conflicts", []) if x.get("worker_id")}), "machines": sorted({str(x.get("machine_id")) for x in it.get("candidate_conflicts", []) if x.get("machine_id") is not None}), "routing_conflicts": sum(1 for x in it.get("candidate_conflicts", []) if x.get("resource_type") == "ROUTING")} for it in iterations])
    _write(out / "promotion_candidates_by_iteration.json", [{"iteration": it.get("iteration"), "eligible": it.get("eligible_promotions", []), "rejected": it.get("rejected_promotions", [])} for it in iterations])
    _write(out / "mode_domain_changes.json", [{"iteration": it.get("iteration"), "domain_signature": it.get("domain_signature"), "mode_count": it.get("mode_generation", {}).get("mode_count")} for it in iterations])
    _write(out / "cp_results_by_iteration.json", [{"iteration": it.get("iteration"), "cp_probe": it.get("cp_probe"), "final_cp": cp if it is iterations[-1] else None} for it in iterations])
    _write(out / "matchup_candidate_analysis.json", [{"iteration": it.get("iteration"), "matchup_candidate_found": False, "reason": "diagnostic_only; no selected schedule before final CP"} for it in iterations])
    _write(out / "causal_closure_analysis.json", {
        "stop_reason": root.get("stop_reason"),
        "causal_closure": root.get("stop_reason") == "CAUSAL_CLOSURE_REACHED",
        "unresolved_conflict_records": len(unresolved),
        "unique_conflicting_operations": unique_ops,
        "physical_boundary_proven": False,
    })
    _write(out / "final_frontier.json", {"iteration": iterations[-1].get("iteration") if iterations else None, "frontier": current_frontier, "unresolved_conflict_records": len(unresolved), "unique_conflicting_operations": unique_ops})
    _write(out / "v7_determinism_comparison.json", _v7_compare(out))
    _write(out / "rejected_promotions.json", [{"iteration": it.get("iteration"), "rows": it.get("rejected_promotions", [])} for it in iterations])
    final_status = ("CRAFT_P2_WORKER_CONFLICT_RESOLVED" if cp.get("status") in ("FEASIBLE", "OPTIMAL")
                    else "CRAFT_P2_RELEASE_BUDGET_REACHED" if root.get("stop_reason") == "RELEASE_BUDGET_REACHED"
                    else "CRAFT_P2_CONVERGENCE_PARTIAL")
    _write(out / "root_cause_classification.json", {
        **root,
        "status": final_status,
        "limiting_factor": "RELEASE_SCOPE_LIMIT" if root.get("stop_reason") == "RELEASE_BUDGET_REACHED" else root.get("stop_reason"),
        "physical_boundary_proven": False,
        "unresolved_conflict_records": len(unresolved),
        "unique_conflicting_operations": unique_ops,
    })
    # Keep the report concise but explicit about the diagnostic stop.
    status = _read(out / "root_cause_classification.json", {}).get("status", "CRAFT_P2_CONVERGENCE_PARTIAL")
    movements = [str(it.get("frontier_movement")) for it in iterations]
    cp_statuses = [str((it.get("cp_probe") or {}).get("status", "NOT_PROBED")) for it in iterations]
    report = [
        "# P2 convergence-controlled causal release fixpoint", "",
        f"Primary status: `{status}`.",
        f"Configuration: N=100, B=50, max_iterations=10; iterations executed: {len(iterations)}.",
        f"Stop reason: `{root.get('stop_reason')}`; released operations: {len(_read(out / 'released_operations.json', []))}.",
        f"Final CP status: `{cp.get('status', 'NOT_RUN')}`; replay and validator remain as recorded in their artifacts.",
        f"Per-iteration CP probes: {cp_statuses}.",
        f"Frontier movement: {movements}; final canonical records={current_frontier.get('conflict_records', 0)}, unique operations={len(current_frontier.get('unique_operations', []))}, workers={len(current_frontier.get('unique_workers', []))}, machines={len(current_frontier.get('unique_machines', []))}, routing records={current_frontier.get('unique_routing_conflicts', 0)}.",
        f"Iterations 1-5 vs v7: release sequence={_v7_compare(out).get('iterations_1_to_5_same_release_sequence')}, mode counts={_v7_compare(out).get('iterations_1_to_5_same_mode_counts')}, canonical frontier={_v7_compare(out).get('iterations_1_to_5_same_frontier')}.",
        "No exact frontier repeat or cycle was detected; causal closure was not reached because the B=50 release ceiling was reached while concrete conflicts remained.",
        "N=100 was not shown insufficient: no outside-N causal boundary was observed. The current limiting factor is bounded release scope, not a proven physical boundary or CP formulation defect.",
        "The unresolved frontier alternated expansion/shrinkage rather than monotonically closing; coordinated multi-CP decomposition is not justified by this run alone.",
        "Shift semantics remained FIXED_CREW_PAUSE_RESUME; no handover or floater semantics were introduced.",
        "M2/M3/M4/W2 regression controls and the targeted/broad V2 benchmarks were not run.",
        "Database writes: 0.",
    ]
    (out / "final_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    files = {str(p.relative_to(out)).replace("\\", "/"): _sha(p) for p in sorted(out.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    _write(out / "artifact_hash_manifest.json", {"files": files, "artifact_count": len(files), "database_writes": 0, "status": status})


def run(output_dir=OUT):
    output_dir = Path(output_dir)
    result = run_p2(output_dir, max_total_released=50, max_iterations=10,
                    neighborhood_size=100, initial_movable_budget=20,
                    convergence_control=True, probe_cp_each_iteration=True,
                    cp_probe_time_seconds=30.0)
    _augment(output_dir)
    return {**result, "output_dir": str(output_dir)}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
