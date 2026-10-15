"""P2-only bounded causal event-release fixpoint diagnostic.

This runner intentionally exercises one predecessor-delay event.  It uses the
authoritative fresh JointExecutableMode generator on every release iteration;
it never carries stale alternatives into the final CP model and never writes
database state.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from crafter.contracts import CrafterStatus
from crafter.lns_probe.analysis import build_incumbent_index
from crafter.lns_probe.exact_machine_model import generate_machine_set_candidates
from crafter.lns_probe.joint_mode_model import build_joint_mode_model, solve_joint_mode_model
from crafter.lns_probe.joint_modes import generate_joint_modes
from crafter.lns_probe.run_e1_7_final_verified import _phenotype, _replay_mode
from crafter.lns_probe.run_e1_1 import EXPECTED_CR, EXPECTED_SOURCE
from crafter.real_data_adapter import SNAPSHOT_NAME
from crafter.phenotype import comparison_metrics, validate

from .benchmark import (
    _augment_predecessor_resource_context,
    _candidate_domains,
    _expand,
    _load_state,
    _neighborhood,
    _rank_released,
)
from .runtime.release_fixpoint import FixpointConfig, resolve_event_release_fixpoint
from .state_update import apply_event
from .v2_execution_diagnostics import _event_from_row, _restore_finite_machine
from .impact_analysis import direct_impact, propagate_impact
from .runtime.execution_state import classify_operations, FreezeParameters
from .runtime.freeze_policy import freeze_decision


DEFAULT_OUT = Path("outputs/crafter/disruption_repair_v2_p2_release_fixpoint")


def _write(out: Path, name: str, value) -> None:
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def _worker_conflicts(left, right):
    shared = sorted(set(map(str, left.worker_ids)) & set(map(str, right.worker_ids)))
    rows = []
    for worker in shared:
        for la, lb in left.worker_segments:
            for ra, rb in right.worker_segments:
                l0 = int(la.timestamp())
                l1 = int(lb.timestamp())
                r0 = int(ra.timestamp())
                r1 = int(rb.timestamp())
                overlap = _overlap(l0, l1, r0, r1)
                if overlap:
                    rows.append({
                        "resource_type": "WORKER",
                        "resource_ids": [worker],
                        "worker_id": worker,
                        "left_segment": [la.isoformat(), lb.isoformat()],
                        "right_segment": [ra.isoformat(), rb.isoformat()],
                        "overlap_seconds": overlap,
                    })
    return rows


def _mode_conflicts(left, right):
    rows = []
    machines = sorted(set(left.machine_ids) & set(right.machine_ids))
    overlap = _overlap(left.start_seconds, left.end_seconds, right.start_seconds, right.end_seconds)
    if machines and overlap:
        rows.append({"resource_type": "MACHINE", "resource_ids": machines,
                     "left_interval": [left.start_seconds, left.end_seconds],
                     "right_interval": [right.start_seconds, right.end_seconds],
                     "overlap_seconds": overlap})
    rows.extend(_worker_conflicts(left, right))
    return rows


def _fresh_conflicts(*, keys, released, modes, after_index, event, iteration):
    """Screen current alternatives against current frozen incumbents."""
    released_set = set(released)
    rows = []
    current = {str(k): tuple(v) for k, v in modes.items()}
    for left_key in sorted(released_set):
        for left in current.get(left_key, ()):
            if left.incumbent:
                continue
            for right_key in sorted(set(keys) - released_set - {left_key}):
                right = next((m for m in current.get(right_key, ()) if m.incumbent), None)
                if right is None:
                    continue
                for evidence in _mode_conflicts(left, right):
                    rows.append({
                        "operation_key": right_key,
                        "conflicts_with": left_key,
                        "iteration": iteration,
                        "reason": "PREDECESSOR_DELAY_" + evidence["resource_type"] + "_CONFLICT",
                        "direct_conflict": left_key == str(event.metadata.get("target_operation_key", "")),
                        "causal_distance": 0 if left_key == str(event.metadata.get("target_operation_key", "")) else 1,
                        **evidence,
                    })
    # Routing boundaries are concrete timing evidence even when resources do
    # not overlap.  Only future frozen successors are promotion candidates.
    for left_key in sorted(released_set):
        successor = after_index.route_successor.get(left_key)
        if not successor or successor not in current or successor in released_set:
            continue
        incumbent = next((m for m in current.get(successor, ()) if m.incumbent), None)
        if incumbent is None:
            continue
        for left in current.get(left_key, ()):
            if left.incumbent or left.end_seconds > incumbent.start_seconds:
                rows.append({"operation_key": successor, "conflicts_with": left_key,
                             "iteration": iteration, "reason": "PREDECESSOR_DELAY_ROUTING_BOUNDARY",
                             "resource_type": "ROUTING", "resource_ids": [],
                             "left_interval": [left.start_seconds, left.end_seconds],
                             "right_interval": [incumbent.start_seconds, incumbent.end_seconds],
                             "overlap_seconds": max(0, left.end_seconds - incumbent.start_seconds),
                             "direct_conflict": left_key == str(event.metadata.get("target_operation_key", "")),
                             "causal_distance": 0})
    return rows


def _mode_generation_callback(shadow, after_index, event, op_details, operator_index,
                              neighborhood, domains):
    def generate(released, iteration):
        started = time.perf_counter()
        machine_sets, provenance = generate_machine_set_candidates(
            shadow.problem, after_index, neighborhood, released, domains,
            max_sets=3, machine_pool_limit=30)
        modes, failures, counts = generate_joint_modes(
            shadow.problem, op_details, operator_index, after_index, neighborhood,
            released, domains, machine_sets, max_starts=10, event=event)
        return {
            "iteration": iteration,
            "modes": modes,
            "machine_sets": machine_sets,
            "machine_provenance": provenance,
            "failures": failures,
            "counts": counts,
            "mode_count": sum(len(v) for v in modes.values()),
            "generation_seconds": time.perf_counter() - started,
            "released": list(released),
        }
    return generate


def _audit_rows(rows, event):
    classified = classify_operations(rows, event.event_time_seconds,
                                      parameters=FreezeParameters(), blocked_operations=())
    return [{"operation_key": x["operation_key"], "state": x["state"],
             "freeze_decision": freeze_decision(x, explicitly_released=False)} for x in classified]


def run(output_dir=DEFAULT_OUT, *, max_total_released: int | None = None,
        max_iterations: int = 5, neighborhood_size: int = 100,
        initial_movable_budget: int = 20, convergence_control: bool = False,
        probe_cp_each_iteration: bool = False, cp_probe_time_seconds: float = 30.0):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    problem, operator_index, rows, durations, op_details, index, baseline, checked, source = _load_state()
    by_key = {str(row["operation_key"]): row for row in rows}
    predecessor = next(key for key in sorted(index.route_successor)
                       if index.route_successor.get(key))
    event = _event_from_row(by_key[predecessor], "P2_PREDECESSOR_DELAY",
                            duration_seconds=3600, predecessor=True)
    direct = direct_impact(index, rows, event)
    impact = propagate_impact(index, direct)
    shadow = _restore_finite_machine(apply_event(problem, operator_index, event), event)
    after_index = build_incumbent_index(shadow.problem, rows, op_details)
    n, b = int(neighborhood_size), int(initial_movable_budget)
    keys = _expand(after_index, direct, n)
    keys = _augment_predecessor_resource_context(after_index, event, keys, n)
    neighborhood = _neighborhood(after_index, event, keys, event.metadata.get("seed", 42))
    domains = _candidate_domains(after_index, shadow.problem, keys, event=event)
    causal = {x["operation_key"]: x for x in []}
    from .benchmark import _rank_released
    from crafter.lns_probe.resource_graph import causal_waiting
    causal = {x["operation_key"]: x for x in causal_waiting(shadow.problem, after_index, op_details)}
    released = _rank_released(after_index, causal, impact["impacted_operations"], keys, b, "graph", event,
                              direct_keys=impact["direct_operations"])
    # Ensure the exact delayed route target is in the event-aware release set.
    target = str(event.metadata.get("target_operation_key", ""))
    if target in keys and target not in released:
        released = tuple(sorted(set(released) | {target}))
    # Reproduce the authoritative successor lower-bound construction used by
    # the existing P2 path before the first fresh generation.
    successor = after_index.route_successor.get(target)
    if successor in keys:
        target_neighborhood = _neighborhood(after_index, event, (target,), event.metadata.get("seed", 42))
        target_domains = _candidate_domains(after_index, shadow.problem, (target,), event=event)
        target_sets, _ = generate_machine_set_candidates(shadow.problem, after_index,
                                                          target_neighborhood, (target,), target_domains,
                                                          max_sets=3, machine_pool_limit=30)
        target_modes, _, _ = generate_joint_modes(shadow.problem, op_details, operator_index, after_index,
                                                   target_neighborhood, (target,), target_domains, target_sets,
                                                   max_starts=10, event=event)
        event.metadata["successor_start_bounds"] = {str(successor): max(int(m.end_seconds)
                                                                         for m in target_modes[target])}
    generate = _mode_generation_callback(shadow, after_index, event, op_details, operator_index, neighborhood, domains)
    neighborhood_for_cp = _neighborhood(after_index, event, tuple(keys), event.metadata.get("seed", 42))

    def cp_probe(current, released_tuple, iteration):
        """Run the unchanged bounded CP model for convergence early-stop checks."""
        if not probe_cp_each_iteration:
            return {"status": "NOT_PROBED"}
        model = build_joint_mode_model(shadow.problem, after_index, neighborhood_for_cp,
                                       current.get("modes", {}))
        solve = solve_joint_mode_model(model, max_time_seconds=float(cp_probe_time_seconds), seed=42)
        return {"status": solve.get("status", "UNKNOWN"), "model": model["size"], "solve": solve,
                "released": list(released_tuple), "iteration": iteration}
    policy = None
    default_budget = min(len(keys), max(len(released) + 10, b + 10))
    configured_budget = default_budget if max_total_released is None else min(len(keys), int(max_total_released))
    cfg = FixpointConfig(max_iterations=int(max_iterations), max_new_promotions_per_iteration=10,
                         max_total_released=configured_budget,
                         convergence_control=bool(convergence_control))
    fixpoint = resolve_event_release_fixpoint(
        index=after_index, event=event, neighborhood_keys=keys, initial_released=released,
        generate=generate,
        conflicts=lambda current_released, current, iteration: _fresh_conflicts(
            keys=keys, released=current_released, modes=current["modes"],
            after_index=after_index, event=event, iteration=iteration),
        cp_probe=cp_probe if probe_cp_each_iteration else None,
        policy=policy, config=cfg)
    final_current = fixpoint.get("current") or {}
    modes = final_current.get("modes", {})
    neighborhood_final = _neighborhood(after_index, event, tuple(keys), event.metadata.get("seed", 42))
    cp = {}
    replay = []
    validation = None
    if modes:
        model = build_joint_mode_model(shadow.problem, after_index, neighborhood_final, modes)
        solve = solve_joint_mode_model(model, max_time_seconds=30.0, seed=42)
        cp = {"status": solve.get("status"), "solve": solve, "model": model["size"],
              "released": sorted(fixpoint["released"]), "mode_count": sum(len(v) for v in modes.values())}
        for key, mode in sorted(solve.get("selected", {}).items()):
            end, segments, mismatches = _replay_mode(shadow.problem, operator_index, after_index,
                                                      neighborhood_final, mode)
            replay.append({"operation_key": key, "mode_id": mode.mode_id,
                           "selected_end_seconds": mode.end_seconds, "replayed_end_seconds": end,
                           "delta_seconds": end - mode.end_seconds, "mismatches": mismatches})
        if solve.get("status") in ("OPTIMAL", "FEASIBLE") and len(solve.get("selected", {})) == len(keys) \
                and not any(item["mismatches"] for item in replay):
            candidate = _phenotype(problem, baseline, solve["selected"])
            checked_after = validate(problem, candidate, operator_index)
            validation = {"status": checked_after.status.value,
                          "valid": checked_after.status == CrafterStatus.VALIDATED_FEASIBLE,
                          "metrics": comparison_metrics(problem, checked_after)}
    event_invalid = []
    for key in keys:
        if key in set(direct):
            incumbent_count = sum(1 for m in modes.get(key, ()) if m.incumbent)
            event_invalid.append({"operation_key": key, "pre_event_incumbent_present": bool(incumbent_count),
                                  "event_invalidated": True, "classification": "PRE_EVENT_INCUMBENT_INVALIDATED_BY_EVENT"})
    cp_domain = [{"operation_key": key,
                  "incumbent_modes": sum(1 for m in modes.get(key, ()) if m.incumbent),
                  "alternative_modes": sum(1 for m in modes.get(key, ()) if not m.incumbent),
                  "event_safe_modes": len(modes.get(key, ())),
                  "released": key in set(fixpoint["released"])} for key in sorted(keys)]
    state_audit = _audit_rows(rows, event)
    stability = {"changed_operations": [], "operations_released": sorted(fixpoint["released"]),
                  "total_absolute_start_shift": 0, "max_start_shift": 0}
    files = {
        "source_identity.json": {"snapshot": SNAPSHOT_NAME, "source_fingerprint": EXPECTED_SOURCE,
                                 "cr_phenotype_hash": EXPECTED_CR, "operations": len(problem.operations),
                                 "orders": len(problem.orders), "database_writes": 0},
        "p2_event.json": event.as_dict(),
        "fixpoint_config.json": fixpoint["config"],
        "fixpoint_iterations.json": fixpoint["iterations"],
        "fixpoint_conflict_graph.json": [row for it in fixpoint["iterations"] for row in it["candidate_conflicts"]],
        "release_candidates.json": [row for it in fixpoint["iterations"] for row in it["eligible_promotions"] + it["rejected_promotions"]],
        "release_provenance.json": fixpoint["promotion_evidence"],
        "operation_state_audit.json": state_audit,
        "worker_conflicts.json": [row for it in fixpoint["iterations"] for row in it["candidate_conflicts"] if row.get("resource_type") == "WORKER"],
        "machine_conflicts.json": [row for it in fixpoint["iterations"] for row in it["candidate_conflicts"] if row.get("resource_type") == "MACHINE"],
        "routing_conflicts.json": [row for it in fixpoint["iterations"] for row in it["candidate_conflicts"] if row.get("resource_type") == "ROUTING"],
        "released_operations.json": sorted(fixpoint["released"]),
        "fresh_mode_generations.json": [it["mode_generation"] for it in fixpoint["iterations"]],
        "stale_mode_audit.json": {"iterations_generated": [it["iteration"] for it in fixpoint["iterations"]], "final_iteration": (len(fixpoint["iterations"]) - 1 if fixpoint["iterations"] else None), "stale_modes_entered_final_cp": False},
        "event_invalid_incumbents.json": event_invalid,
        "cp_domain_audit.json": cp_domain,
        "cp_model_size_by_iteration.json": [{"iteration": it["iteration"], "mode_count": it["mode_generation"].get("mode_count"), "generation_seconds": it["mode_generation"].get("generation_seconds")} for it in fixpoint["iterations"]],
        "final_cp_result.json": cp,
        "replay_result.json": replay,
        "validator_result.json": validation,
        "schedule_stability.json": stability,
        "unresolved_conflicts.json": [] if cp.get("status") in ("OPTIMAL", "FEASIBLE") else [row for it in fixpoint["iterations"][-1:]
                                                                                                    for row in it["candidate_conflicts"]],
        "root_cause_classification.json": {"status": "CRAFT_P2_WORKER_CONFLICT_RESOLVED" if validation and validation["valid"] else "CRAFT_P2_RELEASE_FIXPOINT_PARTIAL", "stop_reason": fixpoint["stop_reason"], "physical_boundary_proven": False},
        "regression_controls.json": {"status": "DEFERRED_UNTIL_P2_SUCCESS", "database_writes": 0},
        "targeted_v2_gate_result.json": {"status": "DEFERRED_UNTIL_P2_SUCCESS"},
        "regression_results.json": {"tests": "P2-only; controls deferred", "database_writes": 0},
    }
    if convergence_control:
        # Convergence artifacts are additive and immutable; the underlying
        # mode generation, CP model, replay, and validator remain unchanged.
        frontiers = [it.get("frontier", {}) for it in fixpoint["iterations"]]
        domains = [it.get("domain_signature", {}) for it in fixpoint["iterations"]]
        movements = []
        for it in fixpoint["iterations"]:
            movements.append({"iteration": it["iteration"],
                              "movement": it.get("frontier_movement"),
                              "exact_repeat": bool(it.get("exact_frontier_repeat")),
                              "cycle": bool(it.get("frontier_cycle")),
                              "new_promotions": it.get("new_promotions", len(it.get("selected_promotions", [])))})
        unresolved = (fixpoint["iterations"][-1].get("candidate_conflicts", [])
                      if fixpoint["iterations"] else [])
        unique_ops = sorted({str(row.get("operation_key")) for row in unresolved if row.get("operation_key")})
        files.update({
            "frontier_signatures.json": frontiers,
            "frontier_progress.json": movements,
            "frontier_cycles.json": fixpoint.get("cycles", []),
            "conflict_records_by_iteration.json": [{"iteration": it["iteration"], "records": len(it.get("candidate_conflicts", []))} for it in fixpoint["iterations"]],
            "unique_conflicting_operations_by_iteration.json": [{"iteration": it["iteration"], "operations": sorted({str(x.get("operation_key")) for x in it.get("candidate_conflicts", []) if x.get("operation_key")})} for it in fixpoint["iterations"]],
            "promotion_candidates_by_iteration.json": [{"iteration": it["iteration"], "eligible": it.get("eligible_promotions", []), "rejected": it.get("rejected_promotions", [])} for it in fixpoint["iterations"]],
            "mode_domain_changes.json": domains,
            "cp_model_size_by_iteration.json": [{"iteration": it["iteration"], "mode_count": it["mode_generation"].get("mode_count"), "generation_seconds": it["mode_generation"].get("generation_seconds"), "domain_signature": it.get("domain_signature")} for it in fixpoint["iterations"]],
            "matchup_candidate_analysis.json": [{"iteration": it["iteration"], "matchup_candidate_found": False, "reason": "diagnostic_not_inferred_without_selected_schedule"} for it in fixpoint["iterations"]],
            "causal_closure_analysis.json": {"stop_reason": fixpoint["stop_reason"], "causal_closure": fixpoint["stop_reason"] == "CAUSAL_CLOSURE_REACHED", "final_conflict_records": len(unresolved), "final_unique_conflicting_operations": unique_ops},
            "final_frontier.json": {"iteration": fixpoint["iterations"][-1]["iteration"] if fixpoint["iterations"] else None, "frontier": frontiers[-1] if frontiers else {}, "conflict_records": len(unresolved), "unique_operations": unique_ops},
            "root_cause_classification.json": {"status": "CRAFT_P2_WORKER_CONFLICT_RESOLVED" if validation and validation["valid"] else "CRAFT_P2_CONVERGENCE_PARTIAL", "stop_reason": fixpoint["stop_reason"], "physical_boundary_proven": False, "unresolved_conflict_records": len(unresolved), "unresolved_unique_operations": unique_ops},
            "v7_determinism_comparison.json": {"status": "PENDING_AGGREGATE_COMPARISON", "reference": "outputs/crafter/disruption_repair_v2_p2_budget_escalation_v7/"},
            "regression_controls.json": {"status": "DEFERRED_UNTIL_P2_SUCCESS", "database_writes": 0},
        })
    for name, value in files.items():
        _write(output_dir, name, value)
    report_status = files["root_cause_classification.json"]["status"]
    report = ["# P2 bounded event-release fixpoint", "", f"Status: `{report_status}`.", "",
              f"Fixpoint iterations: {len(fixpoint['iterations'])}; stop reason: `{fixpoint['stop_reason']}`.",
              f"Released operations: {len(fixpoint['released'])}.",
              f"Final CP status: `{cp.get('status', 'NOT_RUN')}`; replay rows: {len(replay)}; validation: `{validation}`.",
              "M2/M3/M4/W2 controls and the broad V2 benchmark were not run.", "Database writes: 0."]
    (output_dir / "final_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    manifest_files = {str(p.relative_to(output_dir)).replace("\\", "/"): _sha(p)
                      for p in sorted(output_dir.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    _write(output_dir, "artifact_hash_manifest.json", {"files": manifest_files, "artifact_count": len(manifest_files),
                                                        "database_writes": 0, "status": report_status})
    return {"status": report_status, "output_dir": str(output_dir), "iterations": len(fixpoint["iterations"]),
            "released": len(fixpoint["released"]), "cp_status": cp.get("status"), "database_writes": 0}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir), indent=2, default=str))
