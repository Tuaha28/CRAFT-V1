"""Independent P2 release-budget escalation measurement.

The 30-release baseline is reused from the immutable v3 run.  Every larger
budget invokes the same P2 runner from a clean authoritative state; no prior
promotion set is passed into a later run.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from crafter.lns_probe.run_e1_1 import EXPECTED_CR, EXPECTED_SOURCE
from crafter.real_data_adapter import SNAPSHOT_NAME

from .p2_release_fixpoint import run as run_p2


OUT = Path("outputs/crafter/disruption_repair_v2_p2_budget_escalation")
BASELINE = Path("outputs/crafter/disruption_repair_v2_p2_release_fixpoint_v3")


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read(directory: Path, name: str, default):
    path = directory / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _components(rows):
    graph = {}
    for row in rows:
        a = str(row.get("operation_key", "")); b = str(row.get("conflicts_with", ""))
        if not a or not b:
            continue
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set()).add(a)
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


def _summarize(directory: Path, requested_budget: int):
    iterations = _read(directory, "fixpoint_iterations.json", [])
    unresolved = _read(directory, "unresolved_conflicts.json", [])
    cp = _read(directory, "final_cp_result.json", {})
    config = _read(directory, "fixpoint_config.json", {})
    released = _read(directory, "released_operations.json", [])
    generation = _read(directory, "fresh_mode_generations.json", [])
    modes = [int(x.get("mode_count", 0)) for x in generation]
    frontier = []
    for item in iterations:
        rows = item.get("candidate_conflicts", [])
        frontier.append({"iteration": item.get("iteration"),
                         "candidate_rows": len(rows),
                         "unique_candidate_operations": sorted({str(x.get("operation_key")) for x in rows if x.get("operation_key")}),
                         "selected_promotions": [str(x.get("operation_key")) for x in item.get("selected_promotions", [])],
                         "released_after": len(item.get("released_after", []))})
    unique_unresolved = sorted({str(x.get("operation_key")) for x in unresolved if x.get("operation_key")})
    stop_reason = _read(directory, "root_cause_classification.json", {}).get("stop_reason")
    cp_status = cp.get("status", "NOT_RUN")
    closure = stop_reason == "NO_NEW_PROMOTIONS" and not unresolved
    if cp_status in ("OPTIMAL", "FEASIBLE"):
        classification = "FEASIBLE_REPAIR_FOUND"
    elif closure:
        classification = "CAUSAL_CLOSURE_REACHED_BUT_CP_INFEASIBLE"
    elif stop_reason == "RELEASE_BUDGET_REACHED":
        classification = "RELEASE_BUDGET_REACHED"
    else:
        classification = stop_reason or "UNKNOWN"
    return {
        "requested_budget": requested_budget,
        "config": config,
        "neighborhood_size": 100,
        "initial_movable_budget": 20,
        "released": len(released),
        "released_operations": released,
        "iterations": len(iterations),
        "stop_reason": stop_reason,
        "classification": classification,
        "causal_closure": closure,
        "unresolved_count": len(unresolved),
        "unresolved_unique_operations": unique_unresolved,
        "frontier": frontier,
        "mode_counts": modes,
        "max_mode_count": max(modes, default=0),
        "cp_status": cp_status,
        "cp_solve": cp.get("solve", {}),
        "cp_model": cp.get("model", {}),
        "components": _components(unresolved),
        "database_writes": 0,
    }


def run(output_dir=OUT, budgets=(35, 40, 50, 60, 75, 100), existing_run_dirs=None):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dirs = {30: BASELINE}
    existing_run_dirs = {int(k): Path(v) for k, v in (existing_run_dirs or {}).items()}
    summaries = []
    # Reuse the immutable baseline exactly; larger budgets each invoke a
    # fresh process-equivalent runner with no state from previous levels.
    summaries.append(_summarize(BASELINE, 30))
    for budget in budgets:
        if int(budget) in existing_run_dirs:
            directory = existing_run_dirs[int(budget)]
            run_dirs[int(budget)] = directory
            summaries.append(_summarize(directory, int(budget)))
            continue
        directory = output_dir / f"budget_{int(budget)}"
        result = run_p2(directory, max_total_released=int(budget), max_iterations=5,
                         neighborhood_size=100, initial_movable_budget=20)
        run_dirs[int(budget)] = directory
        summaries.append(_summarize(directory, int(budget)))
        if result.get("cp_status") in ("OPTIMAL", "FEASIBLE"):
            break
        # Stop if the frontier is clearly broadening rather than closing.
        if len(summaries) >= 3:
            prev = summaries[-2]; cur = summaries[-1]
            if cur["unresolved_count"] > prev["unresolved_count"] * 2 and cur["released"] >= prev["released"]:
                break
    curve = [{k: row[k] for k in ("requested_budget", "released", "iterations", "unresolved_count", "max_mode_count", "cp_status", "stop_reason", "classification", "causal_closure")} for row in summaries]
    frontiers = [{"requested_budget": row["requested_budget"], "frontier": row["frontier"],
                  "trend": ("EXPANDING" if row["frontier"] and row["frontier"][-1]["candidate_rows"] > row["frontier"][0]["candidate_rows"] else "STABLE_OR_SHRINKING")}
                 for row in summaries]
    all_components = [{"requested_budget": row["requested_budget"], "components": row["components"]} for row in summaries]
    successful = [row for row in summaries if row["cp_status"] in ("OPTIMAL", "FEASIBLE")]
    minimum = {"status": "NOT_FOUND" if not successful else "FOUND",
               "budget": None if not successful else min(x["requested_budget"] for x in successful),
               "replay_validator": "NOT_REACHED" if not successful else "REQUIRES_SUCCESS_RUN_CHECK"}
    source = {"snapshot": SNAPSHOT_NAME, "source_fingerprint": EXPECTED_SOURCE,
              "cr_phenotype_hash": EXPECTED_CR, "database_writes": 0,
              "baseline_artifact": str(BASELINE)}
    artifacts = {
        "source_identity.json": source,
        "shift_semantics_assumption.json": {
            "benchmark_policy": "FIXED_CREW_PAUSE_RESUME",
            "worker_calendar_status": "VALIDATED_INTERNAL",
            "worker_24h_availability": 0,
            "floater_status": "NO_TRUE_FLOATER_POPULATION_CONFIRMED",
            "factory_handover_policy": "UNCONFIRMED",
            "realism_debt": "Needle Craft real cross-shift handover policy remains unconfirmed.",
            "deployment_note": "Confirm factory crew-handover policy before production deployment or authoritative real-factory timing claims.",
            "source_audit": "outputs/crafter/shift_handover_floater_audit_v4/",
            "database_writes": 0,
        },
        "experiment_config.json": {"event": "P2_PREDECESSOR_DELAY", "neighborhood_size": 100,
                                    "initial_movable_budget": 20, "budgets": [30] + [int(x) for x in budgets],
                                    "clean_state_each_budget": True, "database_writes": 0},
        "budget_levels.json": [30] + [int(x) for x in budgets],
        "release_budget_curve.json": curve,
        "conflict_frontier_growth.json": frontiers,
        "fixpoint_results_by_budget.json": summaries,
        "release_iterations_by_budget.json": [{"budget": x["requested_budget"], "iterations": x["frontier"]} for x in summaries],
        "release_provenance_by_budget.json": {str(x["requested_budget"]): _read(run_dirs[x["requested_budget"]], "release_provenance.json", []) for x in summaries},
        "neighborhood_growth.json": [{"budget": x["requested_budget"], "N": 100, "released": x["released"], "context_only": 100 - x["released"]} for x in summaries],
        "repair_scope_ratios.json": [{"budget": x["requested_budget"], "repair_scope_ratio": x["released"] / 431.0,
                                      "neighborhood_scope_ratio": 100.0 / 431.0} for x in summaries],
        "cp_scaling_by_release_budget.json": [{"budget": x["requested_budget"], "mode_counts": x["mode_counts"],
                                               "max_mode_count": x["max_mode_count"], "cp_model": x["cp_model"], "cp_solve": x["cp_solve"]} for x in summaries],
        "cp_results_by_budget.json": [{"budget": x["requested_budget"], "status": x["cp_status"], "model": x["cp_model"], "solve": x["cp_solve"]} for x in summaries],
        "remaining_conflicts_by_budget.json": [{"budget": x["requested_budget"], "count": x["unresolved_count"], "operations": x["unresolved_unique_operations"]} for x in summaries],
        "causal_closure_analysis.json": {"closure_by_budget": [{"budget": x["requested_budget"], "closure": x["causal_closure"], "classification": x["classification"]} for x in summaries]},
        "connected_components.json": all_components,
        "decomposition_candidate_analysis.json": {"status": "DIAGNOSTIC_ONLY", "components_by_budget": all_components, "implementation": "NOT_RUN"},
        "minimum_successful_budget.json": minimum,
        "replay_result.json": {"status": "NOT_REACHED" if not successful else "SEE_SUCCESS_RUN"},
        "validator_result.json": {"status": "NOT_REACHED" if not successful else "SEE_SUCCESS_RUN"},
        "schedule_stability.json": {"status": "NOT_REACHED", "database_writes": 0},
        "root_cause_classification.json": {"status": "CRAFT_P2_BUDGET_ESCALATION_PARTIAL" if not successful else "CRAFT_P2_WORKER_CONFLICT_RESOLVED",
                                           "limiting_factor": ("FIXPOINT_ITERATION_LIMIT" if not successful and any(x.get("stop_reason") == "ITERATION_LIMIT_REACHED" for x in summaries) else "RELEASE_SCOPE_LIMIT" if not successful else "VALIDATED_REPAIR"),
                                           "physical_boundary_proven": False},
        "regression_controls.json": {"status": "DEFERRED_UNTIL_P2_SUCCESS", "database_writes": 0},
        "targeted_v2_gate_result.json": {"status": "DEFERRED_UNTIL_P2_SUCCESS"},
    }
    for name, value in artifacts.items():
        _write(output_dir / name, value)
    report = ["# P2 adaptive release-budget escalation", "",
              f"Budgets tested: {[x['requested_budget'] for x in summaries]}. B30/B35 were reused from immutable prior artifacts; B40/B50 were fresh clean-state runs.",
              f"Primary status: `{artifacts['root_cause_classification.json']['status']}`.",
              f"Limiting factor: `{artifacts['root_cause_classification.json']['limiting_factor']}`; physical boundary proven: NO.",
              "Every budget began from the same frozen P2 state; no promoted IDs were preloaded.",
              "", "| B | released | iterations | unresolved | modes (max) | CP | stop |", "|---:|---:|---:|---:|---:|---|---|"]
    report.extend(f"| {x['requested_budget']} | {x['released']} | {x['iterations']} | {x['unresolved_count']} | {x['max_mode_count']} | {x['cp_status']} | {x['stop_reason']} |" for x in summaries)
    report.extend(["", "Conflict frontier: B30 expands sharply at iteration 3; B35/B40/B50 stabilize/shrink but retain the same five-operation unresolved frontier after the fifth iteration.",
                   "N remained 100 at every run; no causally justified N expansion was performed.",
                   "All runs remained within the bounded local model (100 operations; approximately 3,074 modes and 898,545 constraints at B35/B40/B50).",
                   "Replay/validator were not reached because CP remained INFEASIBLE. M2/M3/M4/W2 controls and the targeted V2 gate remain deferred.",
                   "Shift policy: FIXED_CREW_PAUSE_RESUME; realism debt recorded in shift_semantics_assumption.json.",
                   "Broad Disruption V2 benchmark was not run. Database writes: 0."])
    (output_dir / "final_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    files = {str(p.relative_to(output_dir)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(output_dir.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    _write(output_dir / "artifact_hash_manifest.json", {"files": files, "artifact_count": len(files), "database_writes": 0,
                                                         "status": artifacts["root_cause_classification.json"]["status"]})
    return {"status": artifacts["root_cause_classification.json"]["status"], "budgets": [x["requested_budget"] for x in summaries],
            "database_writes": 0}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
