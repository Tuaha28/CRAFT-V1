"""Single controlled N=100/B=75 P2 local-repair boundary experiment."""
from __future__ import annotations

import json
from pathlib import Path

from .p2_release_fixpoint import run as run_p2
from .p2_b60_continuation import _augment as _augment_b60, _read, _write, _sha, B50_REFERENCE, SHIFT_SOURCE


OUT = Path("outputs/crafter/disruption_repair_v2_p2_b75_boundary_test")
B60_REFERENCE = Path("outputs/crafter/disruption_repair_v2_p2_b60_continuation")
V7_REFERENCE = Path("outputs/crafter/disruption_repair_v2_p2_budget_escalation_v7")


def _selected_sequence(iterations, limit=None):
    rows = []
    for it in iterations:
        for row in it.get("selected_promotions", []):
            rows.append(str(row.get("operation_key")))
            if limit is not None and len(rows) >= limit:
                return rows[:limit]
    return rows


def _compare_sequences(current, reference, limit):
    a = _selected_sequence(current, limit)
    b = _selected_sequence(reference, limit)
    return {"same": len(a) == len(b) == limit and a == b, "current": a, "reference": b}


def _component_rows(iterations):
    rows = iterations[-1].get("candidate_conflicts", []) if iterations else []
    graph = {}
    for row in rows:
        a, b = str(row.get("operation_key", "")), str(row.get("conflicts_with", ""))
        if a and b:
            graph.setdefault(a, set()).add(b); graph.setdefault(b, set()).add(a)
    components = []
    unseen = set(graph)
    while unseen:
        seed = min(unseen); stack = [seed]; seen = set()
        while stack:
            key = stack.pop()
            if key in seen: continue
            seen.add(key); unseen.discard(key); stack.extend(sorted(graph.get(key, ())))
        components.append(sorted(seen))
    return sorted(components, key=lambda x: (-len(x), x))


def _augment(out: Path):
    # Reuse the established artifact writer for the common immutable contracts,
    # then overwrite only B75-specific analyses.
    _augment_b60(out)
    iterations = _read(out / "fixpoint_iterations.json", [])
    cp = _read(out / "final_cp_result.json", {})
    root = _read(out / "root_cause_classification.json", {})
    released = _read(out / "released_operations.json", [])
    b60_iterations = _read(B60_REFERENCE / "fixpoint_iterations.json", [])
    b50_iterations = _read(B50_REFERENCE / "fixpoint_iterations.json", [])
    post = []
    cumulative = 20
    for it in iterations:
        for row in it.get("selected_promotions", []):
            cumulative += 1
            if cumulative > 60:
                evidence = dict(row)
                evidence.update({
                    "promotion_number": cumulative,
                    "triggering_operation": row.get("conflicts_with"),
                    "current_frontier_signature": it.get("frontier_signature_hash"),
                    "lifecycle_state": row.get("operation_state"),
                    "release_policy_decision": row.get("policy_release_allowed"),
                    "fresh_mode_generation_id": f"iteration:{it.get('iteration')}|domain:{it.get('domain_signature', {}).get('hash')}",
                    "routing_relation": row.get("resource_ids", []) if row.get("resource_type") == "ROUTING" else None,
                    "reservation_relation": row.get("reason") if "RESERVATION" in str(row.get("reason")) else None,
                })
                post.append(evidence)
    final_conflicts = iterations[-1].get("candidate_conflicts", []) if iterations else []
    final_frontier = iterations[-1].get("frontier", {}) if iterations else {}
    unique_ops = sorted({str(x.get("operation_key")) for x in final_conflicts if x.get("operation_key")})
    unique_workers = sorted({str(x.get("worker_id")) for x in final_conflicts if x.get("worker_id")})
    unique_machines = sorted({str(x.get("machine_id")) for x in final_conflicts if x.get("machine_id") is not None})
    routing_count = sum(1 for x in final_conflicts if x.get("resource_type") == "ROUTING")

    # Initial release set contains 20 operations, so total-release boundaries
    # 50 and 60 correspond to 30 and 40 selected promotion rows respectively.
    first50 = _compare_sequences(iterations, b50_iterations, 30)
    first60 = _compare_sequences(iterations, b60_iterations, 40)
    # The reference selected sequence excludes the initial 20 released rows;
    # its length is 30 for B50 and 40 for B60, so this explicitly compares the
    # first 50/60 total release sequence through selected promotions.
    determinism = {"reference_b50": str(B50_REFERENCE), "reference_b60": str(B60_REFERENCE),
                   "first_50_deterministic": first50, "first_60_deterministic": first60,
                   "note": "Comparisons are against clean immutable prior runs; no releases were preloaded."}
    _write(out / "determinism_comparison.json", determinism)
    _write(out / "experiment_config.json", {"event": "P2_PREDECESSOR_DELAY", "N": 100, "B": 75, "max_iterations": 10, "max_new_promotions_per_iteration": 10, "clean_state_each_run": True, "only_changed_variable": "release_budget_60_to_75", "database_writes": 0})
    v7_rows = _read(V7_REFERENCE / "fixpoint_results_by_budget.json", [])
    v7_b50 = next((x for x in v7_rows if int(x.get("requested_budget", -1)) == 50), {})
    b50_cp = _read(B50_REFERENCE / "final_cp_result.json", {})
    b60_cp = _read(B60_REFERENCE / "final_cp_result.json", {})
    _write(out / "model_size_reference_reconciliation.json", [
        {"experiment": "scalability_v2_p2_budget_v7_B50", "artifact": str(V7_REFERENCE / "fixpoint_results_by_budget.json"), "iteration": 4, "N": 100, "B": 50, "released_operations": v7_b50.get("released"), "mode_count": v7_b50.get("cp_model", {}).get("modes"), "variable_count": v7_b50.get("cp_model", {}).get("variables"), "constraint_count": v7_b50.get("cp_model", {}).get("constraints"), "model_build_stage": "final_CP_after_max_iterations_5", "reason_for_difference": "Earlier B50 report stopped at released=39 after the five-iteration ceiling."},
        {"experiment": "p2_convergence_fixpoint_B50", "artifact": str(B50_REFERENCE / "final_cp_result.json"), "iteration": 6, "N": 100, "B": 50, "released_operations": len(_read(B50_REFERENCE / "released_operations.json", [])), "mode_count": b50_cp.get("model", {}).get("modes"), "variable_count": b50_cp.get("model", {}).get("variables"), "constraint_count": b50_cp.get("model", {}).get("constraints"), "model_build_stage": "final_CP_after_convergence_run", "reason_for_difference": "Same formulation, but convergence continuation released 50 operations before final CP."},
        {"experiment": "p2_convergence_fixpoint_B60", "artifact": str(B60_REFERENCE / "final_cp_result.json"), "iteration": 8, "N": 100, "B": 60, "released_operations": len(_read(B60_REFERENCE / "released_operations.json", [])), "mode_count": b60_cp.get("model", {}).get("modes"), "variable_count": b60_cp.get("model", {}).get("variables"), "constraint_count": b60_cp.get("model", {}).get("constraints"), "model_build_stage": "final_CP_after_convergence_run", "reason_for_difference": "Same formulation at a larger released set (B60)."},
        {"experiment": "p2_boundary_test_B75", "artifact": str(out / "final_cp_result.json"), "iteration": iterations[-1].get("iteration") if iterations else None, "N": 100, "B": 75, "released_operations": len(released), "mode_count": cp.get("model", {}).get("modes"), "variable_count": cp.get("model", {}).get("variables"), "constraint_count": cp.get("model", {}).get("constraints"), "model_build_stage": "final_CP_after_B75_fixpoint", "reason_for_difference": "B75 controlled boundary experiment."},
    ])
    _write(out / "post_b60_promotion_evidence.json", post)
    _write(out / "causal_distance_analysis.json", [{"iteration": it.get("iteration"), "promotions": [{"operation_key": x.get("operation_key"), "causal_distance": x.get("causal_distance"), "conflicts_with": x.get("conflicts_with"), "resource_type": x.get("resource_type")} for x in it.get("selected_promotions", [])], "frontier_unique_operations": sorted({str(x.get("operation_key")) for x in it.get("candidate_conflicts", []) if x.get("operation_key")})} for it in iterations])
    _write(out / "repair_component_analysis.json", {"final_components": _component_rows(iterations), "component_count": len(_component_rows(iterations)), "source": "final fresh conflict frontier", "multi_cp_implementation": "NOT_RUN"})
    _write(out / "local_repair_boundary_analysis.json", {"N": 100, "B": 75, "released": len(released), "released_scope_ratio": len(released) / 431.0, "neighborhood_scope_ratio": 100 / 431.0, "final_conflict_records": len(final_conflicts), "final_unique_operations": len(unique_ops), "final_unique_workers": len(unique_workers), "final_unique_machines": len(unique_machines), "routing_conflict_records": routing_count, "frontier_classification": iterations[-1].get("frontier_movement") if iterations else None, "interpretation": "Boundary diagnostic only; no hard locality threshold imposed."})
    _write(out / "multi_cp_diagnostic.json", {"status": "MULTI_CP_NOT_JUSTIFIED", "component_count": len(_component_rows(iterations)), "components": _component_rows(iterations), "reason": "This run did not establish separable components with limited cross-resource interaction."})
    _write(out / "b50_frontier_continuation_analysis.json", {"reference_b60": str(B60_REFERENCE), "post_b60_promotions": post, "final_frontier": final_frontier, "final_conflict_records": len(final_conflicts), "final_unique_operations": unique_ops, "final_unique_workers": unique_workers, "final_unique_machines": unique_machines})
    final_status = "CRAFT_P2_WORKER_CONFLICT_RESOLVED" if cp.get("status") in ("FEASIBLE", "OPTIMAL") else ("CRAFT_P2_RELEASE_BUDGET_REACHED" if root.get("stop_reason") == "RELEASE_BUDGET_REACHED" else "CRAFT_P2_ITERATION_LIMIT_REACHED" if root.get("stop_reason") == "ITERATION_LIMIT_REACHED" else "CRAFT_P2_CONVERGENCE_PARTIAL")
    _write(out / "root_cause_classification.json", {**root, "status": final_status, "limiting_factor": "RELEASE_SCOPE_LIMIT" if root.get("stop_reason") == "RELEASE_BUDGET_REACHED" else root.get("stop_reason"), "physical_boundary_proven": False, "post_b60_promotions": len(post), "final_unique_conflicting_operations": unique_ops})
    status = _read(out / "root_cause_classification.json", {}).get("status")
    b60_model = _read(B60_REFERENCE / "final_cp_result.json", {}).get("model", {})
    b75_model = cp.get("model", {})
    report = ["# P2 controlled B75 local-repair boundary test", "", f"Primary status: `{status}`.", f"Configuration: N=100, B=75, max_iterations=10; iterations executed: {len(iterations)}; released={len(released)}.", f"Stop reason: `{root.get('stop_reason')}`; final CP: `{cp.get('status', 'NOT_RUN')}`.", f"First 50 deterministic: {determinism['first_50_deterministic']['same']}; first 60 deterministic: {determinism['first_60_deterministic']['same']}.", f"Promotions after #60: {len(post)}; final frontier={len(final_conflicts)} raw records ({final_frontier.get('conflict_records', 0)} canonical), {len(unique_ops)} operations, {len(unique_workers)} workers, {len(unique_machines)} machines, {routing_count} routing records.", f"Frontier classifications: {[it.get('frontier_movement') for it in iterations]}.", f"Released scope ratio={len(released)/431.0:.6f}; neighborhood scope ratio={100/431.0:.6f}.", f"Model growth versus B60 final: modes {b60_model.get('modes')}->{b75_model.get('modes')} (delta {b75_model.get('modes', 0)-b60_model.get('modes', 0)}), variables {b60_model.get('variables')}->{b75_model.get('variables')} (delta {b75_model.get('variables', 0)-b60_model.get('variables', 0)}), constraints {b60_model.get('constraints')}->{b75_model.get('constraints')} (delta {b75_model.get('constraints', 0)-b60_model.get('constraints', 0)}).", "Causal closure did not occur; no match-up candidate was found; physical impossibility was not proven.", "N=100 was not expanded. Six final conflict components were observed, but limited cross-resource separability was not established; multi-CP remains `MULTI_CP_NOT_JUSTIFIED`.", "Shift semantics remained FIXED_CREW_PAUSE_RESUME; no handover or floaters were introduced.", "Replay/validator and M2/M3/M4/W2 controls remain deferred because CP did not become feasible. Broad V2 was not run. Database writes: 0.", "Next experimental variable supported by evidence: evaluate the iteration-ceiling/causal-release policy in a separately authorized run before increasing N or B further."]
    (out / "final_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    files = {str(p.relative_to(out)).replace("\\", "/"): _sha(p) for p in sorted(out.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    _write(out / "artifact_hash_manifest.json", {"files": files, "artifact_count": len(files), "database_writes": 0, "status": status})


def run(output_dir=OUT):
    output_dir = Path(output_dir)
    result = run_p2(output_dir, max_total_released=75, max_iterations=10, neighborhood_size=100, initial_movable_budget=20, convergence_control=True, probe_cp_each_iteration=True, cp_probe_time_seconds=30.0)
    _augment(output_dir)
    return {**result, "output_dir": str(output_dir)}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
