"""Focused P2 predecessor-delay worker-conflict resolution audit.

This runner deliberately evaluates only the causal delayed route and the
future worker-conflicting order suffixes identified by the v29/v31 audit.  It
does not run the broad Disruption V2 benchmark and does not alter frozen
execution semantics.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import time
from dataclasses import replace
from pathlib import Path

from crafter.contracts import CrafterStatus
from crafter.lns_probe.analysis import build_incumbent_index
from crafter.lns_probe.exact_machine_model import generate_machine_set_candidates
from crafter.lns_probe.joint_mode_model import build_joint_mode_model, solve_joint_mode_model
from crafter.lns_probe.joint_modes import generate_joint_modes
from crafter.lns_probe.run_e1_7_final_verified import _phenotype, _replay_mode
from crafter.phenotype import comparison_metrics, validate

from .benchmark import (_candidate_domains, _load_state, _neighborhood,
                        _rank_released)
from .impact_analysis import direct_impact, propagate_impact
from .state_update import apply_event
from .v2_execution_diagnostics import _event_from_row, _restore_finite_machine


OUT = Path("outputs/crafter/disruption_repair_v2_p2_worker_resolution")
SOURCE = Path("outputs/crafter/CRAFT_SESSION_CHECKPOINT_20260923.md")
V29 = Path("outputs/crafter/disruption_repair_v2_wiring_audit_v29")


def _write(out: Path, name: str, value):
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mode_dict(mode):
    return {
        "mode_id": mode.mode_id, "operation_key": mode.operation_key,
        "line_id": mode.line_id, "k": mode.machine_count,
        "machine_ids": list(mode.machine_ids), "worker_ids": list(mode.worker_ids),
        "start_seconds": mode.start_seconds, "productive_start_seconds": mode.productive_start_seconds,
        "productive_seconds": mode.productive_seconds, "productive_minutes": mode.productive_minutes,
        "end_seconds": mode.end_seconds, "pause_seconds": mode.pause_seconds,
        "machine_occupancy_start": mode.machine_occupancy_start_seconds,
        "machine_occupancy_end": mode.machine_occupancy_end_seconds,
        "worker_segments": [[a.isoformat(), b.isoformat()] for a, b in mode.worker_segments],
        "incumbent": mode.incumbent, "recovery_level": mode.recovery_level,
        "recovery_reason": mode.recovery_reason, "source_event_id": mode.source_event_id,
    }


def _segments_overlap(left, right):
    return left[0] < right[1] and right[0] < left[1]


def _worker_witnesses(selected):
    rows = []
    items = sorted(selected.items())
    for i, (left_key, left) in enumerate(items):
        for right_key, right in items[i + 1:]:
            shared = sorted(set(left.worker_ids).intersection(right.worker_ids))
            for worker in shared:
                for la, lb in left.worker_segments:
                    for ra, rb in right.worker_segments:
                        if not _segments_overlap((la, lb), (ra, rb)):
                            continue
                        start = max(la, ra)
                        end = min(lb, rb)
                        rows.append({
                            "worker_id": str(worker), "left_operation": left_key,
                            "right_operation": right_key,
                            "left_segment": [la.isoformat(), lb.isoformat()],
                            "right_segment": [ra.isoformat(), rb.isoformat()],
                            "overlap_seconds": max(0, int((end - start).total_seconds())),
                        })
    return rows


def _run_variant(problem, operator_index, rows, op_details, after_index,
                 baseline, checked_before, event, keys, released, *, diagnose=False):
    neighborhood = _neighborhood(after_index, event, tuple(sorted(keys)), event.metadata.get("seed", 42))
    domains = _candidate_domains(after_index, problem, neighborhood.operation_keys, event=event)
    started = time.perf_counter()
    machine_sets, provenance = generate_machine_set_candidates(
        problem, after_index, neighborhood, tuple(sorted(released)), domains,
        max_sets=3, machine_pool_limit=30)
    try:
        modes, failures, counts = generate_joint_modes(
            problem, op_details, operator_index, after_index, neighborhood,
            tuple(sorted(released)), domains, machine_sets, max_starts=10, event=event)
    except ValueError as exc:
        return {
            "operations": len(keys), "released": sorted(released),
            "neighborhood": sorted(keys), "domains": {k: list(v) for k, v in domains.items()},
            "machine_provenance": {k: {str(pair): value for pair, value in v.items()}
                                   for k, v in provenance.items()},
            "counts": {}, "failures": {"mode_generation": [str(exc)]},
            "modes": {}, "mode_count": 0, "generation_seconds": time.perf_counter() - started,
            "cp_model": None, "solve": {"status": "MODE_GENERATION_FAILURE", "error": str(exc)},
            "worker_disabled_solve": None, "worker_witnesses": [], "replay": [],
            "validation": None,
        }
    generation_seconds = time.perf_counter() - started
    model = build_joint_mode_model(problem, after_index, neighborhood, modes)
    solve = solve_joint_mode_model(model, max_time_seconds=30.0, seed=42)
    selected = solve.get("selected", {})
    replay = []
    for key, mode in sorted(selected.items()):
        end, segments, mismatches = _replay_mode(
            problem, operator_index, after_index, neighborhood, mode)
        replay.append({"operation_key": key, "mode_id": mode.mode_id,
                       "selected_end_seconds": mode.end_seconds,
                       "replayed_end_seconds": end, "delta_seconds": end - mode.end_seconds,
                       "mismatches": mismatches})
    validation = None
    if solve.get("status") in ("OPTIMAL", "FEASIBLE") and len(selected) == len(keys) \
            and not any(row["mismatches"] for row in replay):
        candidate = _phenotype(problem, baseline, selected)
        checked = validate(problem, candidate, operator_index)
        validation = {"status": checked.status.value,
                      "valid": checked.status == CrafterStatus.VALIDATED_FEASIBLE,
                      "metrics": comparison_metrics(problem, checked)}
    # A diagnostic worker-only witness is computed from the first feasible
    # selection with worker NoOverlap disabled.  No production constraint is
    # removed from the normal result.
    worker_disabled = build_joint_mode_model(
        problem, after_index, neighborhood, modes,
        diagnostic_disable=("worker_no_overlap",))
    worker_disabled_result = solve_joint_mode_model(worker_disabled, max_time_seconds=30.0, seed=42)
    isolation = {}
    isolation_selected = {}
    worker_relaxation = {}
    worker_relaxation_pairs = {}
    worker_relaxation_groups = {}
    if diagnose and solve.get("status") == "INFEASIBLE":
        families = ("routing_precedence", "frozen_machine_reservations",
                    "frozen_worker_reservations", "machine_no_overlap",
                    "worker_no_overlap", "mode_conflicts")
        active = []
        for family in families:
            active.append(family)
            disabled = tuple(other for other in families if other not in active)
            variant = build_joint_mode_model(problem, after_index, neighborhood, modes,
                                             diagnostic_disable=disabled)
            result = solve_joint_mode_model(variant, max_time_seconds=30.0, seed=42)
            label = "+".join(active)
            isolation[label] = result.get("status")
            if result.get("status") in ("OPTIMAL", "FEASIBLE"):
                isolation_selected[label] = result.get("selected", {})
        # Diagnostic only: remove worker demand for one operation at a time.
        # This does not alter the production model; it identifies whether a
        # particular operation's worker domain participates in the joint
        # contradiction.
        for key in sorted(keys):
            relaxed_modes = {
                operation_key: tuple(
                    replace(mode, worker_ids=(), worker_segments=())
                    if operation_key == key else mode
                    for mode in values
                )
                for operation_key, values in modes.items()
            }
            relaxed_model = build_joint_mode_model(
                problem, after_index, neighborhood, relaxed_modes)
            relaxed_result = solve_joint_mode_model(
                relaxed_model, max_time_seconds=30.0, seed=42)
            worker_relaxation[key] = {
                "status": relaxed_result.get("status"),
                "selected_mode_ids": {
                    operation_key: mode.mode_id
                    for operation_key, mode in relaxed_result.get("selected", {}).items()
                },
            }
        restoring_keys = [
            key for key, value in worker_relaxation.items()
            if value.get("status") in ("OPTIMAL", "FEASIBLE")
        ]
        for pair in itertools.combinations(restoring_keys, 2):
            pair_set = set(pair)
            relaxed_modes = {
                operation_key: tuple(
                    replace(mode, worker_ids=(), worker_segments=())
                    if operation_key in pair_set else mode
                    for mode in values
                )
                for operation_key, values in modes.items()
            }
            relaxed_model = build_joint_mode_model(
                problem, after_index, neighborhood, relaxed_modes)
            relaxed_result = solve_joint_mode_model(
                relaxed_model, max_time_seconds=30.0, seed=42)
            worker_relaxation_pairs["+".join(pair)] = {
                "status": relaxed_result.get("status"),
                "selected_mode_ids": {
                    operation_key: mode.mode_id
                    for operation_key, mode in relaxed_result.get("selected", {}).items()
                },
            }
        route_keys = [key for key in keys if key in set(str(event.metadata.get("predecessor_operation_key")))]
        route_order = str(event.metadata.get("predecessor_operation_key", "")).split("_")[0]
        group_defs = {
            "DELAYED_ROUTE": {key for key in keys if str(key).split("_")[0] == route_order},
            "ORDER_61_SUFFIX": {key for key in keys if str(key).split("_")[0] == "61"},
            "ORDER_563_SUFFIX": {key for key in keys if str(key).split("_")[0] == "563"},
            "ORDER_595_SUFFIX": {key for key in keys if str(key).split("_")[0] == "595"},
            "DIRECT_SUCCESSOR_ONLY": {str(event.metadata.get("successor_operation_key"))}.intersection(keys),
        }
        for group_name, group_keys in group_defs.items():
            if not group_keys:
                continue
            relaxed_modes = {
                operation_key: tuple(
                    replace(mode, worker_ids=(), worker_segments=())
                    if operation_key in group_keys else mode
                    for mode in values
                )
                for operation_key, values in modes.items()
            }
            relaxed_model = build_joint_mode_model(
                problem, after_index, neighborhood, relaxed_modes)
            relaxed_result = solve_joint_mode_model(
                relaxed_model, max_time_seconds=30.0, seed=42)
            worker_relaxation_groups[group_name] = {
                "operations": sorted(group_keys),
                "status": relaxed_result.get("status"),
                "selected_mode_ids": {
                    operation_key: mode.mode_id
                    for operation_key, mode in relaxed_result.get("selected", {}).items()
                },
            }
        group_items = [(name, keys_set) for name, keys_set in group_defs.items() if keys_set]
        for (left_name, left_keys), (right_name, right_keys) in itertools.combinations(group_items, 2):
            pair_name = f"{left_name}+{right_name}"
            pair_keys = left_keys | right_keys
            relaxed_modes = {
                operation_key: tuple(
                    replace(mode, worker_ids=(), worker_segments=())
                    if operation_key in pair_keys else mode
                    for mode in values
                )
                for operation_key, values in modes.items()
            }
            relaxed_model = build_joint_mode_model(
                problem, after_index, neighborhood, relaxed_modes)
            relaxed_result = solve_joint_mode_model(
                relaxed_model, max_time_seconds=30.0, seed=42)
            worker_relaxation_groups[pair_name] = {
                "operations": sorted(pair_keys),
                "status": relaxed_result.get("status"),
                "selected_mode_ids": {
                    operation_key: mode.mode_id
                    for operation_key, mode in relaxed_result.get("selected", {}).items()
                },
            }
    diagnostic_witnesses = []
    for label, selected_modes in isolation_selected.items():
        if label.endswith("machine_no_overlap"):
            diagnostic_witnesses = _worker_witnesses(selected_modes)
            break
    return {
        "operations": len(keys), "released": sorted(released),
        "neighborhood": sorted(keys), "domains": {k: list(v) for k, v in domains.items()},
        "machine_provenance": {k: {str(pair): value for pair, value in v.items()}
                               for k, v in provenance.items()},
        "counts": counts, "failures": failures,
        "modes": {k: [_mode_dict(mode) for mode in values] for k, values in modes.items()},
        "mode_count": sum(len(v) for v in modes.values()),
        "generation_seconds": generation_seconds, "cp_model": model["size"],
        "solve": solve, "worker_disabled_solve": worker_disabled_result,
        "constraint_isolation": isolation,
        "isolation_selected": isolation_selected,
        "worker_relaxation": worker_relaxation,
        "worker_relaxation_pairs": worker_relaxation_pairs,
        "worker_relaxation_groups": worker_relaxation_groups,
        "worker_witnesses": diagnostic_witnesses or _worker_witnesses(worker_disabled_result.get("selected", {})),
        "replay": replay, "validation": validation,
    }


def run(output_dir=OUT):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    problem, operator_index, rows, durations, op_details, index, baseline, checked_before, source = _load_state()
    by_key = {str(row["operation_key"]): row for row in rows}
    pred_key = next(key for key in sorted(index.route_successor)
                    if index.route_successor.get(key))
    event = _event_from_row(by_key[pred_key], "P2_PREDECESSOR_DELAY",
                            duration_seconds=3600, predecessor=True)
    direct = direct_impact(index, rows, event)
    impact = propagate_impact(index, direct)
    shadow = _restore_finite_machine(apply_event(problem, operator_index, event), event)
    after_index = build_incumbent_index(shadow.problem, rows, op_details)
    route = []
    cursor = str(event.metadata["predecessor_operation_key"])
    while cursor and cursor not in route:
        route.append(cursor)
        cursor = after_index.route_successor.get(cursor)
    # These are the concrete future operations identified by the v29 worker
    # witnesses.  The minimum-set progression below is intentional: it tests
    # the delayed route, then each witness operation, then both witnesses.
    conflict_operations = {"563_1583", "61_1371"}
    suffixes = {}
    suffixes = {key: (key,) for key in conflict_operations}
    # 595_3811 is the smallest known machine-boundary context required to
    # screen the delayed route mode; the witness progression is then tested
    # independently on top of that context.
    base_context = tuple(route) + ("595_3811",)
    base_released = set(route) | {"595_3811"}
    variants = {
        "ROUTE_PLUS_595_CONTEXT": (base_context, base_released),
        "ROUTE_PLUS_595_AND_563_1583": (
            base_context + ("563_1583",), base_released | {"563_1583"}),
        "ROUTE_PLUS_595_AND_61_1371": (
            base_context + ("61_1371",), base_released | {"61_1371"}),
        "ROUTE_PLUS_595_AND_BOTH_WITNESSES": (
            base_context + ("563_1583", "61_1371"),
            base_released | {"563_1583", "61_1371"},
        ),
    }
    results = {}
    release_provenance = []
    for name, (variant_keys, variant_released) in variants.items():
        keys = variant_keys
        keys = tuple(dict.fromkeys(keys))
        released = set(variant_released)
        for key in sorted(released - set(route)):
            release_provenance.append({"operation_key": key,
                "release_reason": "PREDECESSOR_DELAY_WORKER_CONFLICT",
                "conflicts_with": "1029_293", "worker_ids": [],
                "event_id": event.event_id})
        results[name] = _run_variant(
            shadow.problem, operator_index, rows, op_details, after_index,
            baseline, checked_before, event, keys, released,
            diagnose=(name != "ROUTE_PLUS_595_CONTEXT"))
    identity = {"snapshot": "parity_fix_20260916_s42", "source_fingerprint": source,
                "database_writes": 0, "event_id": event.event_id,
                "checkpoint": str(SOURCE).replace("\\", "/"),
                "reference_audit": str(V29).replace("\\", "/")}
    _write(output_dir, "source_identity.json", identity)
    _write(output_dir, "checkpoint_identity.json", {"checkpoint": str(SOURCE), "audit": str(V29),
                                                     "status": "CRAFT_RECOVERY_WIRING_PARTIAL"})
    _write(output_dir, "p2_causal_chain.json", {"event": event.as_dict(), "route": route,
                                                 "direct_impact": sorted(direct), "impact": impact,
                                                 "worker_conflict_operations": sorted(conflict_operations)})
    witness_rows = []
    for name, result in results.items():
        for row in result["worker_witnesses"]:
            witness_rows.append({"variant": name, **row})
    _write(output_dir, "worker_conflict_witnesses.json", witness_rows)
    reference_variant = next(iter(results))
    audit = {key: {"operation_key": key, "future": True,
                   "released_in_variants": [name for name, value in results.items() if key in value["released"]],
                   "incumbent": next((value for value in results[reference_variant]["modes"].get(key, []) if value["incumbent"]), None)}
            for key in ("563_1583", "61_1371")}
    _write(output_dir, "563_1583_audit.json", audit["563_1583"])
    _write(output_dir, "61_1371_audit.json", audit["61_1371"])
    _write(output_dir, "worker_segment_overlap_audit.json", {"witnesses": witness_rows,
        "uses_productive_segments": True, "worker_id_normalization": "canonical_string"})
    _write(output_dir, "release_provenance.json", release_provenance)
    _write(output_dir, "repair_neighborhood.json", {name: {"operations": value["operations"],
        "released": value["released"], "neighborhood": value["neighborhood"]}
        for name, value in results.items()})
    _write(output_dir, "fresh_modes.json", {name: {"mode_count": value["mode_count"], "modes": value["modes"]}
        for name, value in results.items()})
    _write(output_dir, "cp_domain_audit.json", {name: {"operations": value["operations"],
        "mode_count": value["mode_count"], "cp_model": value["cp_model"]}
        for name, value in results.items()})
    _write(output_dir, "constraint_isolation.json", {name: {"normal": value["solve"],
        "worker_no_overlap_disabled": value["worker_disabled_solve"],
        "cumulative": value.get("constraint_isolation", {}),
            "cumulative_selected_mode_ids": {
            label: {key: mode.mode_id for key, mode in selected.items()}
            for label, selected in value.get("isolation_selected", {}).items()
        },
        "worker_relaxation": value.get("worker_relaxation", {}),
        "worker_relaxation_pairs": value.get("worker_relaxation_pairs", {}),
        "worker_relaxation_groups": value.get("worker_relaxation_groups", {})}
        for name, value in results.items()})
    _write(output_dir, "cp_result.json", {name: value["solve"] for name, value in results.items()})
    _write(output_dir, "replay_result.json", {name: value["replay"] for name, value in results.items()})
    _write(output_dir, "validator_result.json", {name: value["validation"] for name, value in results.items()})
    _write(output_dir, "schedule_stability.json", {"note": "subset diagnostic; no accepted full repair yet",
        "changed_operations": [], "database_writes": 0})
    _write(output_dir, "regression_controls.json", {"controls_deferred": ["M2", "M3", "M4", "W2"],
        "reason": "P2 subset diagnosis only"})
    _write(output_dir, "wiring_fixes.json", {"changes": [
        "bounded causal release through conflicting order suffix",
        "worker end envelopes published for released route modes"], "database_writes": 0})
    statuses = {name: value["solve"].get("status") for name, value in results.items()}
    feasible = [name for name, status in statuses.items() if status in ("OPTIMAL", "FEASIBLE")]
    decision = "CRAFT_P2_WORKER_CONFLICT_RESOLVED" if feasible else "CRAFT_P2_RESOLUTION_PARTIAL"
    _write(output_dir, "targeted_v2_gate_result.json", {"status": "DEFERRED",
        "reason": "M2/M3/M4/W2 controls not rerun in this focused subset pass"})
    _write(output_dir, "final_report.md", "\n".join([
        "# P2 Predecessor-Delay Worker Resolution",
        "", f"Status: `{decision}`.", "",
        f"Subset statuses: `{json.dumps(statuses, sort_keys=True)}`.",
        "The broad Disruption V2 benchmark was not run.",
        "Worker feasibility is evaluated on authoritative productive segments.",
    ]) + "\n")
    files = {str(path.relative_to(output_dir)).replace("\\", "/"): _sha(path)
             for path in sorted(output_dir.rglob("*")) if path.is_file() and path.name != "artifact_hash_manifest.json"}
    _write(output_dir, "artifact_hash_manifest.json", {"files": files, "decision": decision,
        "database_writes": 0, "source_fingerprint": source, "artifact_count": len(files)})
    return {"status": decision, "subset_statuses": statuses, "database_writes": 0}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUT)
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir), indent=2, default=str))
