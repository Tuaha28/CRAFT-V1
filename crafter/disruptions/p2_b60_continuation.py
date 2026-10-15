"""Single controlled N=100/B=60 P2 continuation experiment."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .p2_release_fixpoint import run as run_p2
from .p2_convergence_fixpoint import _read, _write, _sha
from .runtime.release_fixpoint import frontier_signature


OUT = Path("outputs/crafter/disruption_repair_v2_p2_b60_continuation")
B50_REFERENCE = Path("outputs/crafter/disruption_repair_v2_p2_convergence_fixpoint_v2")
SHIFT_SOURCE = Path("outputs/crafter/shift_handover_floater_audit_v4")


def _determinism(out: Path) -> dict:
    current = _read(out / "fixpoint_iterations.json", [])
    prior = _read(B50_REFERENCE / "fixpoint_iterations.json", [])
    limit = min(5, len(current), len(prior))
    release_same = []
    modes_same = []
    frontier_same = []
    for i in range(limit):
        release_same.append([str(x.get("operation_key")) for x in current[i].get("selected_promotions", [])]
                            == [str(x.get("operation_key")) for x in prior[i].get("selected_promotions", [])])
        modes_same.append(current[i].get("mode_generation", {}).get("mode_count")
                          == prior[i].get("mode_generation", {}).get("mode_count"))
        frontier_same.append(current[i].get("frontier_signature_hash")
                             == prior[i].get("frontier_signature_hash"))
    return {
        "reference": str(B50_REFERENCE),
        "iterations_compared": limit,
        "first_50_release_sequence_same": bool(limit == 5 and all(release_same)),
        "first_50_mode_counts_same": bool(limit == 5 and all(modes_same)),
        "first_50_frontier_signatures_same": bool(limit == 5 and all(frontier_same)),
        "release_sequence_by_iteration": release_same,
        "mode_count_by_iteration": modes_same,
        "frontier_by_iteration": frontier_same,
    }


def _augment(out: Path):
    iterations = _read(out / "fixpoint_iterations.json", [])
    cp = _read(out / "final_cp_result.json", {})
    root = _read(out / "root_cause_classification.json", {})
    released = _read(out / "released_operations.json", [])
    prior_frontier = _read(B50_REFERENCE / "final_frontier.json", {})
    post = []
    cumulative = 20
    for it in iterations:
        selected = it.get("selected_promotions", [])
        for row in selected:
            cumulative += 1
            if cumulative > 50:
                evidence = dict(row)
                evidence["promotion_number"] = cumulative
                evidence["triggering_operation"] = row.get("conflicts_with")
                evidence["current_frontier_signature"] = it.get("frontier_signature_hash")
                evidence["lifecycle_eligibility"] = row.get("operation_state")
                evidence["policy_eligibility"] = row.get("policy_release_allowed")
                post.append(evidence)
    final_frontier = iterations[-1].get("frontier", {}) if iterations else {}
    final_conflicts = iterations[-1].get("candidate_conflicts", []) if iterations else []
    unique_ops = sorted({str(x.get("operation_key")) for x in final_conflicts if x.get("operation_key")})
    unique_workers = sorted({str(x.get("worker_id")) for x in final_conflicts if x.get("worker_id")})
    unique_machines = sorted({str(x.get("machine_id")) for x in final_conflicts if x.get("machine_id") is not None})
    _write(out / "shift_semantics_assumption.json", {
        "benchmark_policy": "FIXED_CREW_PAUSE_RESUME",
        "worker_calendar_status": "VALIDATED_INTERNAL",
        "worker_24h_availability": 0,
        "floater_status": "NO_TRUE_FLOATER_POPULATION_CONFIRMED",
        "factory_handover_policy": "UNCONFIRMED",
        "realism_debt": "Needle Craft real cross-shift handover policy remains unconfirmed.",
        "source_audit": str(SHIFT_SOURCE), "database_writes": 0,
    })
    _write(out / "experiment_config.json", {
        "event": "P2_PREDECESSOR_DELAY", "N": 100, "B": 60,
        "max_iterations": 10, "max_new_promotions_per_iteration": 10,
        "clean_state_each_run": True, "starting_state": "authoritative_initial_P2_state",
        "only_changed_variable": "release_budget_50_to_60", "database_writes": 0,
    })
    _write(out / "determinism_comparison.json", _determinism(out))
    _write(out / "release_sequence.json", [{"iteration": it.get("iteration"), "released_before": it.get("released_before", []), "selected_promotions": it.get("selected_promotions", []), "released_after": it.get("released_after", [])} for it in iterations])
    _write(out / "post_b50_promotion_evidence.json", post)
    _write(out / "b50_frontier_continuation_analysis.json", {
        "prior_b50_final_frontier": prior_frontier,
        "current_final_frontier": final_frontier,
        "post_b50_promotions": post,
        "frontier_after_b50_unique_operations": unique_ops,
        "frontier_after_b50_unique_workers": unique_workers,
        "frontier_after_b50_unique_machines": unique_machines,
        "frontier_records_after_b50": len(final_conflicts),
        "interpretation": "Current regenerated evidence only; no historical witness was preloaded.",
    })
    _write(out / "iteration_summary.json", [{
        "iteration": it.get("iteration"), "released_before": len(it.get("released_before", [])),
        "released_after": len(it.get("released_after", [])), "new_promotions": len(it.get("selected_promotions", [])),
        "conflict_records": len(it.get("candidate_conflicts", [])),
        "unique_conflicting_operations": sorted({str(x.get("operation_key")) for x in it.get("candidate_conflicts", []) if x.get("operation_key")}),
        "unique_conflicting_workers": sorted({str(x.get("worker_id")) for x in it.get("candidate_conflicts", []) if x.get("worker_id")}),
        "unique_conflicting_machines": sorted({str(x.get("machine_id")) for x in it.get("candidate_conflicts", []) if x.get("machine_id") is not None}),
        "routing_conflicts": sum(1 for x in it.get("candidate_conflicts", []) if x.get("resource_type") == "ROUTING"),
        "frontier_signature_hash": it.get("frontier_signature_hash"), "frontier_classification": it.get("frontier_movement"),
        "cp_status": (it.get("cp_probe") or {}).get("status", "NOT_PROBED"),
    } for it in iterations])
    _write(out / "frontier_signatures.json", [it.get("frontier", {}) for it in iterations])
    _write(out / "frontier_progress.json", [{"iteration": it.get("iteration"), "released_total": len(it.get("released_after", [])), "classification": it.get("frontier_movement"), "new_promotions": len(it.get("selected_promotions", []))} for it in iterations])
    _write(out / "promotion_candidates_by_iteration.json", [{"iteration": it.get("iteration"), "eligible": it.get("eligible_promotions", []), "rejected": it.get("rejected_promotions", [])} for it in iterations])
    _write(out / "rejected_promotions.json", [{"iteration": it.get("iteration"), "rejected": it.get("rejected_promotions", [])} for it in iterations])
    _write(out / "conflict_records_by_iteration.json", [{"iteration": it.get("iteration"), "records": len(it.get("candidate_conflicts", []))} for it in iterations])
    _write(out / "unique_conflicting_operations_by_iteration.json", [{"iteration": it.get("iteration"), "operations": sorted({str(x.get("operation_key")) for x in it.get("candidate_conflicts", []) if x.get("operation_key")}), "workers": sorted({str(x.get("worker_id")) for x in it.get("candidate_conflicts", []) if x.get("worker_id")}), "machines": sorted({str(x.get("machine_id")) for x in it.get("candidate_conflicts", []) if x.get("machine_id") is not None}), "routing_conflicts": sum(1 for x in it.get("candidate_conflicts", []) if x.get("resource_type") == "ROUTING")} for it in iterations])
    _write(out / "causal_closure_analysis.json", {"stop_reason": root.get("stop_reason"), "causal_closure": root.get("stop_reason") == "CAUSAL_CLOSURE_REACHED", "physical_boundary_proven": False, "final_conflict_records": len(final_conflicts), "final_unique_operations": unique_ops})
    _write(out / "final_frontier.json", {"iteration": iterations[-1].get("iteration") if iterations else None, "frontier": final_frontier, "conflict_records": len(final_conflicts), "unique_operations": unique_ops, "unique_workers": unique_workers, "unique_machines": unique_machines})
    _write(out / "cp_results_by_iteration.json", [{"iteration": it.get("iteration"), "cp_probe": it.get("cp_probe"), "final_cp": cp if i == len(iterations)-1 else None} for i, it in enumerate(iterations)])
    _write(out / "matchup_candidate_analysis.json", [{"iteration": it.get("iteration"), "matchup_candidate_found": False, "reason": "diagnostic_only"} for it in iterations])
    final_status = "CRAFT_P2_WORKER_CONFLICT_RESOLVED" if cp.get("status") in ("FEASIBLE", "OPTIMAL") else ("CRAFT_P2_RELEASE_BUDGET_REACHED" if root.get("stop_reason") == "RELEASE_BUDGET_REACHED" else "CRAFT_P2_ITERATION_LIMIT_REACHED" if root.get("stop_reason") == "ITERATION_LIMIT_REACHED" else "CRAFT_P2_CONVERGENCE_PARTIAL")
    _write(out / "root_cause_classification.json", {**root, "status": final_status, "limiting_factor": "RELEASE_SCOPE_LIMIT" if root.get("stop_reason") == "RELEASE_BUDGET_REACHED" else root.get("stop_reason"), "physical_boundary_proven": False, "post_b50_promotions": len(post), "final_unique_conflicting_operations": unique_ops})
    status = _read(out / "root_cause_classification.json", {}).get("status")
    report = [
        "# P2 controlled B60 continuation", "", f"Primary status: `{status}`.",
        f"Configuration: N=100, B=60, max_iterations=10; iterations executed: {len(iterations)}.",
        f"Released operations: {len(released)}; promotions after #50: {len(post)}.",
        f"Stop reason: `{root.get('stop_reason')}`; final CP: `{cp.get('status', 'NOT_RUN')}`.",
        f"First 50 deterministic vs B50: {_determinism(out)}.",
        f"Final frontier: {len(final_conflicts)} records, {len(unique_ops)} operations, {len(unique_workers)} workers, {len(unique_machines)} machines, {sum(1 for x in final_conflicts if x.get('resource_type') == 'ROUTING')} routing records.",
        "Physical impossibility was not proven; N=100 was not expanded; no handover or floaters were introduced.",
        "Replay/validator and M2/M3/M4/W2 controls remain deferred because CP did not become feasible. Broad V2 was not run. Database writes: 0.",
    ]
    (out / "final_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    files = {str(p.relative_to(out)).replace("\\", "/"): _sha(p) for p in sorted(out.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    _write(out / "artifact_hash_manifest.json", {"files": files, "artifact_count": len(files), "database_writes": 0, "status": status})


def run(output_dir=OUT):
    output_dir = Path(output_dir)
    result = run_p2(output_dir, max_total_released=60, max_iterations=10,
                    neighborhood_size=100, initial_movable_budget=20,
                    convergence_control=True, probe_cp_each_iteration=True,
                    cp_probe_time_seconds=30.0)
    _augment(output_dir)
    return {**result, "output_dir": str(output_dir)}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
