"""Narrow, auditable recovery-wiring diagnostic for Disruption Repair V2.

This runner does not benchmark the disruption matrix.  It traces the four
previously failing paths through policy, event-adjusted domains, fresh joint
modes and bounded CP.  It deliberately reuses the authoritative E1.7 helpers
and writes a new immutable diagnostic directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import replace
from pathlib import Path

from crafter.contracts import CrafterStatus
from crafter.lns_probe.analysis import build_incumbent_index, seconds
from crafter.lns_probe.exact_machine_model import generate_machine_set_candidates
from crafter.lns_probe.joint_mode_model import build_joint_mode_model, solve_joint_mode_model
from crafter.lns_probe.joint_modes import generate_joint_modes
from crafter.lns_probe.run_e1_7_final_verified import _replay_mode
from crafter.lns_probe.resource_graph import causal_waiting
from crafter.phenotype import comparison_metrics, validate
from crafter.real_data_adapter import SNAPSHOT_NAME

from .benchmark import (
    EXPECTED_CR, EXPECTED_SOURCE, REF, SIZES, _augment_predecessor_resource_context,
    _candidate_domains, _expand, _load_state, _neighborhood, _rank_released,
)
from .events import DisruptionEvent, EventType
from .impact_analysis import direct_impact, propagate_impact
from .policies.base import machine_recovery_candidates
from .policies.predecessor_delay import propagate_predecessor_delay
from .policies.worker_absence import worker_recovery_candidates
from .runtime.trigger_policy import evaluate_trigger
from .state_update import apply_event
from .v2_execution_diagnostics import _event_from_row, _restore_finite_machine

OUT = Path("outputs/crafter/disruption_repair_v2_wiring_audit")


def _write(out: Path, name: str, value):
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _k_source():
    path = Path("outputs/crafter/e1_3_freedom_audit/k_freedom.json")
    if not path.exists():
        return {}
    return {str(item["operation_key"]): item for item in json.loads(path.read_text())}


def _mode_fields(mode):
    return {"mode_id": mode.mode_id, "operation_key": mode.operation_key,
            "line_id": mode.line_id, "k": mode.machine_count,
            "machine_ids": list(mode.machine_ids), "worker_ids": list(mode.worker_ids),
            "start_seconds": mode.start_seconds,
            "productive_start_seconds": mode.productive_start_seconds,
            "productive_seconds": mode.productive_seconds,
            "productive_minutes": mode.productive_minutes,
            "end_seconds": mode.end_seconds, "pause_seconds": mode.pause_seconds,
            "machine_occupancy_start": mode.machine_occupancy_start_seconds,
            "machine_occupancy_end": mode.machine_occupancy_end_seconds,
            "worker_segments": [[a.isoformat(), b.isoformat()] for a, b in mode.worker_segments],
            "incumbent": mode.incumbent, "recovery_level": mode.recovery_level,
            "recovery_reason": mode.recovery_reason, "source_event_id": mode.source_event_id}


def _events(problem, rows, index):
    by_key = {str(row["operation_key"]): row for row in rows}
    machine_key = "122_1061" if "122_1061" in by_key else sorted(by_key)[0]
    worker_key = next((key for key in sorted(by_key)
                       if len(by_key[key].get("machine_ids", ())) <= 2), sorted(by_key)[0])
    pred_key = next((key for key in sorted(index.route_successor)
                     if index.route_successor.get(key)), machine_key)
    return [
        _event_from_row(by_key[machine_key], "M2_K_REDUCTION", duration_seconds=1800),
        _event_from_row(by_key[machine_key], "M3_TEMPORAL_RECOVERY", duration_seconds=1800),
        _event_from_row(by_key[machine_key], "M4_CROSS_LINE", duration_seconds=1800),
        _event_from_row(by_key[pred_key], "P2_PREDECESSOR_DELAY", duration_seconds=3600, predecessor=True),
        _event_from_row(by_key[worker_key], "W2_BORROWED_WORKER", duration_seconds=28800, worker=True),
    ]


def _policy_trace(problem, event, index, row, k_source):
    op = problem.operation_by_key[str(row["operation_key"])]
    incumbent_machines = tuple(int(v) for v in row.get("machine_ids", ()))
    if event.machine_ids:
        legal = k_source.get(str(row["operation_key"]), {}).get("legal_screened_k_values", ())
        decision = machine_recovery_candidates(
            problem, op, event, int(row["line_id"]), len(incumbent_machines), incumbent_machines,
            repair_time=event.end_time_seconds, legal_k_values=legal)
        return {"type": "MACHINE", "candidates": [c.as_dict() for c in decision.candidates],
                "failure": decision.failure, "legal_k_values": list(legal)}
    if event.worker_ids:
        absent = {str(v) for v in event.worker_ids}
        workers = tuple(str(v) for v in row.get("employee_ids", ()))
        decision = worker_recovery_candidates(
            problem, op, event, incumbent_line=int(row["line_id"]), incumbent_k=len(incumbent_machines),
            incumbent_worker_ids=workers,
            same_line_workers=tuple(x for x in workers if x not in absent),
            borrowed_workers=(), replacement_start=event.end_time_seconds,
            legal_k_values=k_source.get(str(row["operation_key"]), {}).get("legal_screened_k_values", ()))
        return {"type": "WORKER", "candidates": [c.as_dict() for c in decision.candidates],
                "failure": decision.failure}
    delayed = str(event.metadata.get("predecessor_operation_key", ""))
    return {"type": "PREDECESSOR", "propagation": propagate_predecessor_delay(
        index, delayed, int(event.metadata.get("delayed_end_seconds", 0)))}


def _run_case(problem, operator_index, rows, op_details, index, baseline, checked, event, *, n=30, b=5, diagnose=True, forced_release=()):
    direct = direct_impact(index, rows, event)
    impact = propagate_impact(index, direct)
    shadow = _restore_finite_machine(apply_event(problem, operator_index, event), event)
    after_index = build_incumbent_index(shadow.problem, rows, op_details)
    # Cross-line domain diagnosis is intentionally target-only first.  It
    # evaluates every authoritative eligible line without paying for a full
    # 30-operation context; the subsequent bounded execution gate remains the
    # place for neighborhood-scale feasibility.
    case_n = 1 if event.event_id == "M4_CROSS_LINE" else n
    case_b = 1 if event.event_id == "M4_CROSS_LINE" else b
    keys = _expand(after_index, direct, case_n)
    keys = _augment_predecessor_resource_context(after_index, event, keys, case_n)
    neighborhood = _neighborhood(after_index, event, keys, event.metadata.get("seed", 42))
    domains = _candidate_domains(after_index, shadow.problem, keys, event=event)
    causal = {x["operation_key"]: x for x in causal_waiting(shadow.problem, after_index, op_details)}
    released = _rank_released(after_index, causal, impact["impacted_operations"], keys, case_b, "graph", event,
                              direct_keys=impact["direct_operations"])
    if forced_release:
        released = tuple(sorted(set(released) | (set(map(str, forced_release)) & set(keys))))
    started = time.perf_counter()
    machine_sets, provenance = generate_machine_set_candidates(
        shadow.problem, after_index, neighborhood, released, domains, max_sets=3, machine_pool_limit=30)
    trace = {"event": event.as_dict(), "direct_impact": sorted(direct), "impact": impact,
             "neighborhood": list(keys), "released": list(released),
             "domains": {k: list(v) for k, v in domains.items()},
             "machine_provenance": {k: {str(pair): value for pair, value in val.items()}
                                    for k, val in provenance.items()},
             "generation_seconds": None}
    try:
        modes, failures, counts = generate_joint_modes(
            shadow.problem, op_details, operator_index, after_index, neighborhood, released,
            domains, machine_sets, max_starts=10, event=event)
        from .benchmark import _predecessor_mode_worker_conflicts
        event_release_conflicts = _predecessor_mode_worker_conflicts(
            event, keys, released, modes)
        promoted = sorted({row["operation_key"] for row in event_release_conflicts})
        if promoted:
            released = tuple(sorted(set(released) | set(promoted)))
            machine_sets, provenance = generate_machine_set_candidates(
                shadow.problem, after_index, neighborhood, released, domains,
                max_sets=3, machine_pool_limit=30)
            modes, failures, counts = generate_joint_modes(
                shadow.problem, op_details, operator_index, after_index, neighborhood,
                released, domains, machine_sets, max_starts=10, event=event)
    except ValueError as exc:
        trace.update({"mode_generation_status": "ZERO_EXECUTABLE_MODE", "error": str(exc),
                      "counts": counts if "counts" in locals() else {},
                      "failures": failures if "failures" in locals() else {}})
        trace["generation_seconds"] = time.perf_counter() - started
        return trace
    trace["released"] = list(released)
    trace["event_release_conflicts"] = locals().get("event_release_conflicts", [])
    trace["generation_seconds"] = time.perf_counter() - started
    trace["counts"] = counts; trace["failures"] = failures
    trace["modes_by_operation"] = {key: [_mode_fields(mode) for mode in values]
                                    for key, values in modes.items()}
    model = build_joint_mode_model(shadow.problem, after_index, neighborhood, modes)
    solve = solve_joint_mode_model(model, max_time_seconds=30.0, seed=int(event.metadata.get("seed", 42)))
    trace["cp_model"] = model["size"]; trace["solve"] = solve
    trace["cp_domain"] = [{"operation_key": key,
                           "incumbent_modes": sum(1 for mode in modes[key] if mode.incumbent),
                           "event_safe_modes": len(modes[key]),
                           "alternative_modes": sum(1 for mode in modes[key] if not mode.incumbent),
                           "released": key in set(released),
                           "event_invalidated": key in set(direct)} for key in sorted(modes)]
    routing_edges = []
    for key in sorted(modes):
        successor = after_index.route_successor.get(key)
        if successor not in modes:
            continue
        pairs = [(left.end_seconds, right.start_seconds)
                 for left in modes[key] for right in modes[successor]]
        feasible_pairs = [pair for pair in pairs if pair[0] <= pair[1]]
        if not feasible_pairs:
            routing_edges.append({"predecessor": key, "successor": successor,
                                  "predecessor_end_min": min(x[0] for x in pairs),
                                  "predecessor_end_max": max(x[0] for x in pairs),
                                  "successor_start_min": min(x[1] for x in pairs),
                                  "successor_start_max": max(x[1] for x in pairs),
                                  "classification": "NO_ROUTING_MODE_PAIR"})
    trace["routing_incompatibilities"] = routing_edges
    # This isolation deliberately distinguishes individual executable-mode
    # feasibility from joint CP compatibility.  It does not weaken the
    # production model or alter the authoritative constraints.
    trace["constraint_isolation"] = {
        "individual_executable_modes": "FEASIBLE",
        "full_bounded_model": solve.get("status"),
        "first_failing_family": ("JOINT_CP_COMPATIBILITY" if solve.get("status") == "INFEASIBLE" else None),
        "note": "family isolation is diagnostic; no production constraint removed"}
    if diagnose and solve.get("status") == "INFEASIBLE":
        variants = {}
        families = ("routing_precedence", "frozen_machine_reservations",
                    "frozen_worker_reservations", "machine_no_overlap",
                    "worker_no_overlap", "mode_conflicts")
        for family in families:
            variant = build_joint_mode_model(shadow.problem, after_index, neighborhood, modes,
                                             diagnostic_disable=(family,))
            variant_result = solve_joint_mode_model(variant, max_time_seconds=30.0,
                                                    seed=int(event.metadata.get("seed", 42)))
            variants[family] = variant_result.get("status")
        trace["constraint_isolation"]["single_family_removed"] = variants
        trace["constraint_isolation"]["first_restoring_family"] = next(
            (family for family, status in variants.items() if status in ("OPTIMAL", "FEASIBLE")), None)
        # Cumulative isolation adds one family at a time while keeping all
        # remaining production constraints disabled.  This distinguishes a
        # true first family failure from a multi-family interaction where
        # removing any one family is insufficient to restore feasibility.
        ordered_families = ("routing_precedence", "frozen_machine_reservations",
                            "frozen_worker_reservations", "machine_no_overlap",
                            "worker_no_overlap", "mode_conflicts")
        cumulative = {"base_exactly_one_and_linking": None}
        active = []
        previous_selected = None
        for family in ordered_families:
            active.append(family)
            disabled = tuple(other for other in ordered_families if other not in active)
            variant = build_joint_mode_model(shadow.problem, after_index, neighborhood, modes,
                                             diagnostic_disable=disabled)
            variant_result = solve_joint_mode_model(variant, max_time_seconds=30.0,
                                                    seed=int(event.metadata.get("seed", 42)))
            cumulative["+".join(active)] = variant_result.get("status")
            if variant_result.get("status") in ("OPTIMAL", "FEASIBLE"):
                previous_selected = variant_result.get("selected", {})
        if previous_selected:
            machine_conflicts = []
            worker_conflicts = []
            selected_items = sorted(previous_selected.items())
            for i, (left_key, left) in enumerate(selected_items):
                for right_key, right in selected_items[i + 1:]:
                    shared = sorted(set(left.machine_ids).intersection(right.machine_ids))
                    if shared and not (left.end_seconds <= right.start_seconds or right.end_seconds <= left.start_seconds):
                        machine_conflicts.append({"left_operation": left_key, "right_operation": right_key,
                                                  "shared_machine_ids": shared,
                                                  "left_interval": [left.start_seconds, left.end_seconds],
                                                  "right_interval": [right.start_seconds, right.end_seconds]})
                    for worker_id in sorted(set(left.worker_ids).intersection(right.worker_ids)):
                        for lseg in left.worker_segments:
                            for rseg in right.worker_segments:
                                if lseg[1] <= rseg[0] or rseg[1] <= lseg[0]:
                                    continue
                                worker_conflicts.append({"left_operation": left_key, "right_operation": right_key,
                                                         "shared_worker_ids": [worker_id],
                                                         "left_segments": [[a.isoformat(), b.isoformat()] for a, b in left.worker_segments],
                                                         "right_segments": [[a.isoformat(), b.isoformat()] for a, b in right.worker_segments]})
            trace["constraint_isolation"]["pre_machine_overlap_selected_modes"] = {
                key: _mode_fields(mode) for key, mode in previous_selected.items()}
            trace["constraint_isolation"]["first_machine_overlap_witnesses"] = machine_conflicts[:50]
            trace["constraint_isolation"]["first_worker_overlap_witnesses"] = worker_conflicts[:50]
        trace["constraint_isolation"]["cumulative_family_addition"] = cumulative
    selected = solve.get("selected", {})
    replay = []
    for key, mode in sorted(selected.items()):
        end, segments, mismatches = _replay_mode(shadow.problem, operator_index, after_index, neighborhood, mode)
        replay.append({"operation_key": key, "mode_id": mode.mode_id,
                       "selected_end_seconds": mode.end_seconds, "replayed_end_seconds": end,
                       "delta_seconds": end - mode.end_seconds, "mismatches": mismatches})
    trace["replay"] = replay
    if solve.get("status") in ("OPTIMAL", "FEASIBLE") and len(selected) == n and not any(x["mismatches"] for x in replay):
        candidate = __import__("crafter.lns_probe.run_e1_7_final_verified", fromlist=["_phenotype"])._phenotype(problem, baseline, selected)
        checked_after = validate(problem, candidate, operator_index)
        trace["validation"] = {"status": checked_after.status.value, "valid": checked_after.status == CrafterStatus.VALIDATED_FEASIBLE,
                                "metrics": comparison_metrics(problem, checked_after)}
    else:
        trace["validation"] = None
    return trace


def run(output_dir=OUT):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    problem, operator_index, rows, durations, op_details, index, baseline, checked, source = _load_state()
    k_source = _k_source()
    results = []
    for event in _events(problem, rows, index):
        target_key = str(event.metadata.get("target_operation_key", ""))
        row_key = target_key or (str(event.operation_keys[0]) if event.operation_keys else next(iter(index.by_key)))
        row = index.by_key[row_key]
        # Keep the known-good worker control at the V1 diagnostic envelope,
        # while giving predecessor delay enough causal/resource context to
        # expose the first joint conflict rather than stopping at a frozen
        # boundary of an artificially tiny target-only neighborhood.
        case_n, case_b = ((50, 10) if event.event_id == "W2_BORROWED_WORKER" else
                          (100, 20) if event.event_id == "P2_PREDECESSOR_DELAY" else (30, 5))
        results.append({"scenario_id": event.event_id,
                        "policy": _policy_trace(problem, event, index, row, k_source),
                        "trace": _run_case(problem, operator_index, rows, op_details, index, baseline, checked, event,
                                           n=case_n, b=case_b)})
    worker = next(x for x in results if x["scenario_id"] == "W2_BORROWED_WORKER")
    failures = {}
    for item in results:
        status = item["trace"].get("solve", {}).get("status") if item["trace"].get("solve") else item["trace"].get("mode_generation_status", "UNKNOWN")
        failures[status] = failures.get(status, 0) + 1
    identity = {"snapshot": SNAPSHOT_NAME, "source_fingerprint": EXPECTED_SOURCE,
                "cr_phenotype_hash": EXPECTED_CR, "operations": len(problem.operations),
                "orders": len(problem.orders), "database_writes": 0}
    _write(output_dir, "source_identity.json", identity)
    _write(output_dir, "industry_model_identity.json", {"directory": "outputs/crafter/industry_operating_model_v1",
        "decision": "CRAFT_INDUSTRY_MODEL_V1_FROZEN", "database_writes": 0})
    _write(output_dir, "scenario_catalog.json", [{"scenario_id": x["scenario_id"], "event": x["trace"]["event"]} for x in results])
    _write(output_dir, "pipeline_trace.json", results)
    _write(output_dir, "k_domain_audit.json", [{"scenario_id": x["scenario_id"], "policy": x["policy"],
        "domains": {k: v for k, v in x["trace"].get("domains", {}).items()
                    if k in set(x["trace"].get("direct_impact", ()))}}
        for x in results])
    _write(output_dir, "temporal_recovery_audit.json", [x for x in results if x["scenario_id"] == "M3_TEMPORAL_RECOVERY"])
    _write(output_dir, "cross_line_domain_audit.json", [x for x in results if x["scenario_id"] == "M4_CROSS_LINE"])
    _write(output_dir, "predecessor_boundary_audit.json", [x for x in results if x["scenario_id"] == "P2_PREDECESSOR_DELAY"])
    _write(output_dir, "worker_control.json", worker)
    _write(output_dir, "mode_generation_audit.json", [{"scenario_id": x["scenario_id"], "counts": x["trace"].get("counts"),
        "failures": x["trace"].get("failures"), "generation_seconds": x["trace"].get("generation_seconds")} for x in results])
    _write(output_dir, "cp_domain_audit.json", [{"scenario_id": x["scenario_id"], "domains": x["trace"].get("cp_domain", [])} for x in results])
    _write(output_dir, "constraint_isolation.json", [{"scenario_id": x["scenario_id"], "isolation": x["trace"].get("constraint_isolation")} for x in results])
    _write(output_dir, "failure_taxonomy.json", failures)
    _write(output_dir, "replay_results.json", [{"scenario_id": x["scenario_id"], "replay": x["trace"].get("replay", [])} for x in results])
    _write(output_dir, "validator_results.json", [{"scenario_id": x["scenario_id"], "validation": x["trace"].get("validation")} for x in results])
    _write(output_dir, "wiring_fixes.json", {"fixes": [
        "event-aware domains now include authoritative screened k values",
        "direct event operations and propagated predecessor successors are released",
        "cross-line eligible lines are passed through domain generation",
        "finite outage remains interval-screened rather than permanently deleting machines"], "database_writes": 0})
    _write(output_dir, "regression_results.json", {"worker_control_status": worker["trace"].get("solve", {}).get("status"),
        "worker_control_validation": worker["trace"].get("validation"), "database_writes": 0})
    files = {str(path.relative_to(output_dir)).replace("\\", "/"): _sha(path)
             for path in sorted(output_dir.rglob("*")) if path.is_file() and path.name != "artifact_hash_manifest.json"}
    def _valid(scenario_id):
        """Return validation success without masking missing/diagnostic results."""
        item = next(x for x in results if x["scenario_id"] == scenario_id)
        validation = item["trace"].get("validation")
        return bool(validation and validation.get("valid", False))

    machine_ok = all(_valid(name) for name in ("M2_K_REDUCTION", "M3_TEMPORAL_RECOVERY"))
    worker_ok = _valid("W2_BORROWED_WORKER")
    predecessor_ok = _valid("P2_PREDECESSOR_DELAY")
    crossline_mode = next(x for x in results if x["scenario_id"] == "M4_CROSS_LINE")["trace"].get("solve", {}).get("status") in ("OPTIMAL", "FEASIBLE")
    decision = ("CRAFT_RECOVERY_WIRING_FIXED" if machine_ok and worker_ok and predecessor_ok and crossline_mode
                else "CRAFT_RECOVERY_WIRING_PARTIAL")
    manifest = {"files": files, "source_fingerprint": EXPECTED_SOURCE, "cr_phenotype_hash": EXPECTED_CR,
                "decision": decision, "database_writes": 0, "artifact_count": len(files)}
    _write(output_dir, "artifact_hash_manifest.json", manifest)
    report = ["# CRAFT Disruption Repair V2 Recovery Wiring Audit", "", f"Status: `{decision}`.", "",
              "The audit traced policy candidates, event-aware domains, machine/crew candidates, executable modes, bounded CP, replay and validation. The broad V2 benchmark was not run.", "",
              f"Failure taxonomy: `{json.dumps(failures, sort_keys=True)}`.",
              "Wiring corrections are limited to disruption-domain propagation and do not modify frozen normal scheduling semantics."]
    (output_dir / "final_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    # Rehash after final report creation.
    files = {str(path.relative_to(output_dir)).replace("\\", "/"): _sha(path)
             for path in sorted(output_dir.rglob("*")) if path.is_file() and path.name != "artifact_hash_manifest.json"}
    _write(output_dir, "artifact_hash_manifest.json", {**manifest, "files": files, "artifact_count": len(files)})
    return {"status": decision, "scenarios": len(results), "failure_taxonomy": failures, "database_writes": 0}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUT)
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir), indent=2, default=str))
