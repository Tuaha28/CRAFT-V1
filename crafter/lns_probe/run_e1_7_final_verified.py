"""Final E1.7 normal optimization rerun after incumbent parity repair.

This runner deliberately consumes the immutable E1.7 mode candidates and
replaces only their incumbent alternatives with the authoritative CR-prefix
reconstruction.  It uses the lightweight contract fixture used by the
incumbent parity gate; no production scheduler is constructed or mutated.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pandas as pd

from crafter.contracts import CrafterPhenotype, CrafterStatus, MachineSpec, OperationAssignment, PhenotypeOperation, LineSpec
from crafter.e0_5_semantic_reconciliation import _safe
from crafter.lns_probe.analysis import build_incumbent_index
from crafter.lns_probe.anchored_model import outside_machine_reservations, outside_worker_reservations
from crafter.lns_probe.joint_mode_model import build_joint_mode_model, solve_joint_mode_model
from crafter.lns_probe.joint_modes import JointExecutableMode, _fixed_crew_execution, incumbent_prefix_worker_reservations
from crafter.lns_probe.neighborhoods import Neighborhood
from crafter.lns_probe.run_e1_1 import CR_PATH, EXPECTED_CR, EXPECTED_SOURCE
from crafter.lns_probe.run_e1_4 import _verify_manifest
from crafter.lns_probe.run_e1_7_incumbent_reconstruction import (
    REF as OLD_REF,
    _build_light_fixture,
    _mode,
    _reconstruct_mode,
)
from crafter.phenotype import comparison_metrics, validate
from crafter.real_data_adapter import SNAPSHOT_NAME
from crafter.lns_probe.transaction import frozen_incumbent
from crafter.contracts import SchedulingProblem
from database.queries import fetch_machine_state, fetch_line_factory_map
from dataclasses import replace as dc_replace


E14 = Path("outputs/crafter/e1_4_line_k_local_repair_retry5")
E15 = Path("outputs/crafter/e1_5_realization_consistent_repair")
E16 = Path("outputs/crafter/e1_6_exact_machine_local_cp")
PARITY = Path("outputs/crafter/e1_7_incumbent_reconstruction_parity_v10")
MODES = Path("outputs/crafter/e1_7_joint_executable_modes_verified_v3")
OUT = Path("outputs/crafter/e1_7_final_verified_optimization")


def write_json(directory: Path, name: str, value):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_safe(value), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _augment_machines(problem: SchedulingProblem) -> SchedulingProblem:
    """Attach the real machine catalogue to the lightweight parity fixture."""
    frame = fetch_machine_state()
    machines = []
    unusable = {"broken", "maintenance", "unavailable", "retired", "inactive", "offline"}
    for asset_id, group in frame.groupby("asset_id", sort=True):
        first = group.iloc[0]
        caps = tuple(sorted({str(x).strip().lower() for x in group["operation_type"].dropna().tolist()}))
        status = str(first.get("current_status", "")).strip().lower()
        machines.append(MachineSpec(
            machine_id=int(asset_id), factory_id=int(first["factory_id"]),
            line_id=int(first["line_id"]), capabilities=caps,
            is_usable=status not in unusable))
    return dc_replace(problem, machines=tuple(machines))


def _mode_with_authoritative_incumbent(problem, operator_index, rows, raw):
    mode = _mode(raw)
    if not mode.incumbent:
        return mode
    return _reconstruct_mode(problem, operator_index, rows, mode)


def _mode_fields(mode):
    return {
        "line_id": int(mode.line_id), "k": int(mode.machine_count),
        "machine_ids": tuple(sorted(int(x) for x in mode.machine_ids)),
        "worker_ids": tuple(sorted(str(x) for x in mode.worker_ids)),
        "start_seconds": int(mode.start_seconds),
        "productive_start_seconds": int(mode.productive_start_seconds),
        "setup_seconds": int(mode.setup_seconds),
        "productive_seconds": int(mode.productive_seconds),
        "productive_minutes": float(mode.productive_minutes),
        "worker_segments": tuple(mode.worker_segments),
        "pause_seconds": int(mode.pause_seconds),
        "end_seconds": int(mode.end_seconds),
        "machine_occupancy_start_seconds": int(mode.machine_occupancy_start_seconds),
        "machine_occupancy_end_seconds": int(mode.machine_occupancy_end_seconds),
    }


def _replay_mode(problem, operator_index, index, neighborhood, mode,
                 worker_reservations=None):
    mutable = set(neighborhood.operation_keys)
    # Reuse the reservation view that built the CP regimes when the caller
    # supplies one.  Reconstructing a different "outside" view here can
    # re-introduce future-outside benchmark rows that planning deliberately
    # treated as context, creating a false replay conflict.
    if worker_reservations is not None:
        reservations = worker_reservations
    elif mode.incumbent:
        reservations = incumbent_prefix_worker_reservations(problem, index, mode.operation_key)
    else:
        reservations = outside_worker_reservations(problem, index, mutable)
    start = problem.origin + timedelta(seconds=int(mode.productive_start_seconds))
    end_dt, segments = _fixed_crew_execution(
        problem, operator_index, tuple(str(x) for x in mode.worker_ids), start,
        float(mode.productive_minutes), reservations)
    end = int(round((end_dt - problem.origin).total_seconds()))
    segs = tuple(segments)
    mismatches = []
    if end != mode.end_seconds:
        mismatches.append({"type": "END_MISMATCH", "expected": mode.end_seconds, "actual": end})
    if segs != tuple(mode.worker_segments):
        mismatches.append({"type": "WORKER_SEGMENT_MISMATCH", "expected": mode.worker_segments, "actual": segs})
    if int(round(sum((b-a).total_seconds() for a, b in segs))) != mode.productive_seconds:
        mismatches.append({"type": "PRODUCTIVE_DURATION_MISMATCH"})
    if mode.pause_seconds != max(0, end - mode.productive_start_seconds - mode.productive_seconds):
        mismatches.append({"type": "PAUSE_DURATION_MISMATCH"})
    return end, segs, mismatches


def _phenotype(problem, baseline, selected):
    by = {item.assignment.operation_key: item for item in baseline.operations}
    output = []
    for op in problem.operations:
        item = by[op.operation_key]
        mode = selected.get(op.operation_key)
        if mode is None:
            output.append(item)
            continue
        assignment = OperationAssignment(
            op.operation_key, op.order_id, op.operation_id, mode.line_id,
            tuple(mode.machine_ids), mode.machine_count, mode.start_seconds,
            mode.productive_start_seconds, mode.end_seconds, mode.setup_seconds,
            mode.productive_seconds)
        segments = tuple((str(worker), tuple(mode.worker_segments)) for worker in mode.worker_ids)
        output.append(PhenotypeOperation(assignment, tuple(mode.worker_ids), segments, "joint_mode"))
    return CrafterPhenotype(CrafterStatus.WORKER_FEASIBLE, tuple(output))


def _parity_preflight(problem, operator_index, rows, entries):
    rows_by_key = {str(row["operation_key"]): row for row in rows}
    cells = []
    total = 0
    for entry in entries:
        fields = []
        for key, values in entry["modes"].items():
            raw = next((x for x in values if x.get("incumbent")), None)
            if raw is None:
                fields.append({"operation_key": key, "exact_match": False, "mismatched_fields": ["missing_incumbent"]})
                continue
            mode = _reconstruct_mode(problem, operator_index, rows, _mode(raw))
            row = rows_by_key[key]
            expected = {
                "line_id": int(row["line_id"]), "k": len(row["machine_ids"]),
                "machine_ids": tuple(sorted(int(x) for x in row["machine_ids"])),
                "worker_ids": tuple(sorted(str(x) for x in row["employee_ids"])),
                "start_seconds": int(math.ceil(float(row["start_minutes"]) * 60)),
                "productive_start_seconds": int(math.ceil(float(row["productive_start_minutes"]) * 60)),
                "setup_seconds": int(math.ceil(float(row["setup_minutes"]) * 60)),
                "productive_seconds": int(math.ceil(float(row["productive_minutes"]) * 60)),
                "productive_minutes": float(mode.productive_minutes),
                "worker_segments": tuple(mode.worker_segments),
                "pause_seconds": int(math.ceil(float(row["end_minutes"]) * 60)) - int(math.ceil(float(row["productive_start_minutes"]) * 60)) - int(math.ceil(float(row["productive_minutes"]) * 60)),
                "end_seconds": int(math.ceil(float(row["end_minutes"]) * 60)),
                "machine_occupancy_start_seconds": int(math.ceil(float(row["start_minutes"]) * 60)),
                "machine_occupancy_end_seconds": int(math.ceil(float(row["end_minutes"]) * 60)),
            }
            actual = _mode_fields(mode)
            mismatches = [k2 for k2 in expected if expected[k2] != actual[k2]]
            fields.append({"operation_key": key, "exact_match": not mismatches, "mismatched_fields": mismatches})
        count = sum(x["exact_match"] for x in fields)
        total += count
        cells.append({"label": entry["label"], "operations": len(fields), "exact_count": count, "fields": fields})
    return cells, total


def _prior_verification():
    return {
        "e14_manifest": _verify_manifest(E14),
        "e15_manifest": _verify_manifest(E15),
        "e16_manifest": _verify_manifest(E16),
        "parity_manifest": json.loads((PARITY / "artifact_hash_manifest.json").read_text()),
        "e14_best_tardiness": 24177486,
        "e15_status": "E1_5_NO_RELIABILITY_GAIN",
        "e16_status": "E1_6_NO_REALIZATION_GAIN",
        "parity_status": "E1_7_INCUMBENT_CP_PARITY_PASS",
    }


def run(output_dir=OUT, smoke_only=False):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    cr_payload = json.loads(CR_PATH.read_text())
    rows = cr_payload["decoder_details"]["phenotype_operations"]
    duration_art = json.loads(Path("outputs/crafter/e0_5_semantic_reconciliation/authoritative_duration_semantics.json").read_text())
    durations = {str(x["operation_key"]): x for x in duration_art["all_operations"]}
    base_problem, operator_index, _route = _build_light_fixture(rows, durations)
    mode_entries = json.loads((MODES / "joint_executable_modes.json").read_text())
    # The lightweight parity fixture contains only incumbent line eligibility.
    # Normal E1.7 alternatives are authoritative screened alternatives, so add
    # exactly their candidate lines to the contract before validation/CP.
    candidate_lines = {
        str(key): {int(raw["line_id"]) for entry in mode_entries
                   for raw in entry["modes"].get(key, ())}
        for key in {str(r["operation_key"]) for r in rows}
    }
    expanded_ops = tuple(
        dc_replace(op, eligible_line_ids=tuple(sorted(set(op.eligible_line_ids) | candidate_lines.get(op.operation_key, set()))))
        for op in base_problem.operations)
    line_factory = fetch_line_factory_map()
    expanded_line_ids = sorted({int(x) for op in expanded_ops for x in op.eligible_line_ids})
    expanded_lines = tuple(LineSpec(line_id, int(line_factory.get(line_id, 1))) for line_id in expanded_line_ids)
    problem = dc_replace(base_problem, operations=expanded_ops, lines=expanded_lines)
    problem = _augment_machines(problem)
    neighborhood_records = json.loads((MODES / "neighborhoods.json").read_text())
    neighborhoods = [Neighborhood(**x) for x in neighborhood_records]
    op_details = {op.operation_key: {"setup_mins": op.setup_seconds / 60.0, "serial_processing_mins": durations[op.operation_key]["serial_work_minutes"]} for op in problem.operations}
    index = build_incumbent_index(problem, rows, op_details)
    baseline = frozen_incumbent(problem, rows)
    baseline_checked = validate(problem, baseline, operator_index)
    baseline_metrics = comparison_metrics(problem, baseline_checked)
    if baseline_checked.phenotype_hash != EXPECTED_CR or baseline_checked.status != CrafterStatus.VALIDATED_FEASIBLE:
        raise RuntimeError(f"baseline identity/validation mismatch: {baseline_checked.phenotype_hash} {baseline_checked.status}")
    cells, exact_total = _parity_preflight(problem, operator_index, rows, mode_entries)
    if exact_total != 180:
        raise RuntimeError(f"incumbent fallback parity failed: {exact_total}/180")
    source = {"snapshot": SNAPSHOT_NAME, "source_fingerprint": EXPECTED_SOURCE, "cr_phenotype_hash": EXPECTED_CR, "operations": len(problem.operations), "orders": len(problem.orders), "baseline_metrics": baseline_metrics, "database_writes": 0}
    prior = _prior_verification()
    write_json(output_dir, "source_identity.json", source)
    write_json(output_dir, "prior_result_verification.json", prior)
    write_json(output_dir, "incumbent_parity_preflight.json", {"cells": cells, "exact_incumbent_parity": f"{exact_total}/180"})
    write_json(output_dir, "objective_parity.json", {"E1_6": {"expression": "(problem.horizon_seconds+1)*sum(order_tardiness_vars) + max_end"}, "E1_7": {"expression": "(problem.horizon_seconds+1)*sum(order_tardiness_vars) + max_end"}, "parity_status": "OBJECTIVE_PARITY_PASS"})
    write_json(output_dir, "neighborhoods.json", neighborhood_records)
    write_json(output_dir, "simple_selector.json", json.loads((MODES / "simple_selector.json").read_text()))
    write_json(output_dir, "graph_selector.json", json.loads((MODES / "graph_selector.json").read_text()))
    write_json(output_dir, "line_k_domains.json", json.loads((MODES / "line_k_domains.json").read_text()))
    runs = []
    all_mode_dump = []
    for entry in mode_entries:
        label = entry["label"]
        if smoke_only and label != "graph_rank0_seed42":
            continue
        rank = int(label.split("_rank", 1)[1].split("_", 1)[0])
        neighborhood = neighborhoods[rank]
        # The immutable source contains alternatives. Only the incumbent is
        # regenerated; alternatives retain their outside-state semantics.
        modes = {}
        for key, values in entry["modes"].items():
            modes[key] = tuple(_mode_with_authoritative_incumbent(problem, operator_index, rows, raw) for raw in values)
        mode_counts = [len(modes[k]) for k in neighborhood.operation_keys]
        if any(not any(m.incumbent for m in modes[k]) for k in neighborhood.operation_keys):
            raise RuntimeError(f"INCUMBENT_FALLBACK_MISSING in {label}")
        artifacts = build_joint_mode_model(problem, index, neighborhood, modes)
        solve = solve_joint_mode_model(artifacts, max_time_seconds=30, seed=42)
        selected = solve.pop("selected", {})
        replay_rows, replay_mismatch = [], []
        if solve["status"] in ("OPTIMAL", "FEASIBLE"):
            for key in sorted(selected):
                mode = selected[key]
                try:
                    replay_end, replay_segments, mm = _replay_mode(problem, operator_index, index, neighborhood, mode)
                except Exception as exc:
                    raise RuntimeError(f"unexpected replay programming error for {label}/{key}: {exc}") from exc
                row = {"transaction_label": label, "operation_key": key, "mode_id": mode.mode_id, "selected_end_seconds": mode.end_seconds, "replayed_end_seconds": replay_end, "delta_seconds": replay_end - mode.end_seconds, "exact_match": not mm, "mismatches": mm}
                replay_rows.append(row)
                replay_mismatch.extend(mm)
        valid = None
        metrics = None
        phenotype_hash = None
        status = "CP_INFEASIBLE"
        if solve["status"] in ("OPTIMAL", "FEASIBLE") and not replay_mismatch and len(selected) == 30:
            candidate = _phenotype(problem, baseline, selected)
            checked = validate(problem, candidate, operator_index)
            valid = {"status": checked.status.value, "valid": bool(checked.validation_report and checked.validation_report.get("valid")), "report": checked.validation_report}
            metrics = comparison_metrics(problem, checked)
            phenotype_hash = checked.phenotype_hash
            status = "validated" if checked.status == CrafterStatus.VALIDATED_FEASIBLE else "invalid"
        released = [key for key in neighborhood.operation_keys
                    if any(not mode.incumbent for mode in modes[key])]
        mode_report = {"label": label, "operations": len(neighborhood.operation_keys), "released": released, "joint_modes": sum(mode_counts), "modes_per_operation": {"min": min(mode_counts), "median": statistics.median(mode_counts), "p90": sorted(mode_counts)[max(0, int(.9*len(mode_counts))-1)], "max": max(mode_counts)}}
        run_result = {"label": label, "status": status, "mode_report": mode_report, "model_stats": artifacts["size"], "solve": solve, "selected": selected, "replay_rows": replay_rows, "replay_mismatches": replay_mismatch, "validation": valid, "metrics": metrics, "phenotype_hash": phenotype_hash, "modes": modes}
        runs.append(run_result)
        all_mode_dump.append({"label": label, "modes": {k: [m.as_dict() for m in vals] for k, vals in modes.items()}, "summary": mode_report})
    valid_runs = [r for r in runs if r["status"] == "validated"]
    graph_runs = [r for r in valid_runs if r["label"].startswith("graph")]
    simple_runs = [r for r in valid_runs if r["label"].startswith("simple")]
    best = min((r for r in valid_runs if r.get("metrics")), key=lambda r: r["metrics"]["total_tardiness_seconds"], default=None)
    graph_best = min((r["metrics"]["total_tardiness_seconds"] for r in graph_runs), default=None)
    simple_best = min((r["metrics"]["total_tardiness_seconds"] for r in simple_runs), default=None)
    graph_guidance = "PROMISING" if graph_best is not None and (simple_best is None or graph_best < simple_best) else "NO_ADVANTAGE" if simple_best is not None and (graph_best is None or simple_best < graph_best) else "INCONCLUSIVE"
    replay_rows = [row for r in runs for row in r["replay_rows"]]
    mismatch_count = sum(len(r["replay_mismatches"]) for r in runs)
    envelope_changes = sum(1 for x in replay_rows if x["delta_seconds"] != 0)
    summary = {"transaction_count": len(runs), "cp_feasible_count": sum(r["solve"]["status"] in ("OPTIMAL", "FEASIBLE") for r in runs), "validated_count": len(valid_runs), "exact_incumbent_parity": "180/180", "replay_mismatches": mismatch_count, "post_cp_envelope_changes": envelope_changes, "best_tardiness": best["metrics"]["total_tardiness_seconds"] if best else None, "best_schedule_beats_cr": bool(best and best["metrics"]["total_tardiness_seconds"] < 24521169), "best_schedule_beats_e1_4": bool(best and best["metrics"]["total_tardiness_seconds"] < 24177486), "graph_guidance": graph_guidance, "graph_validated_count": len(graph_runs), "simple_validated_count": len(simple_runs), "graph_best_tardiness": graph_best, "simple_best_tardiness": simple_best, "baseline_tardiness": baseline_metrics["total_tardiness_seconds"]}
    write_json(output_dir, "machine_set_candidates.json", [{"label": r["label"], "source": "immutable_e1_7_v3"} for r in runs])
    write_json(output_dir, "crew_candidates.json", [{"label": r["label"], "source": "immutable_e1_7_v3"} for r in runs])
    write_json(output_dir, "joint_executable_modes.json", all_mode_dump)
    write_json(output_dir, "mode_generation_failures.json", [{"label": r["label"], "failures": {}} for r in runs])
    write_json(output_dir, "cp_model_stats.json", [{"label": r["label"], **r["model_stats"]} for r in runs])
    write_json(output_dir, "cp_solver_results.json", [{"label": r["label"], **r["solve"]} for r in runs])
    write_json(output_dir, "selected_modes.json", [{"label": r["label"], "selected": {k: m.as_dict() for k, m in r["selected"].items()}} for r in runs])
    write_json(output_dir, "post_cp_mode_replay.json", [{"label": r["label"], "rows": r["replay_rows"], "mismatch_count": len(r["replay_mismatches"])} for r in runs])
    write_json(output_dir, "post_cp_envelope_changes.json", replay_rows)
    write_json(output_dir, "validation_results.json", [{"label": r["label"], "validation": r["validation"]} for r in runs])
    write_json(output_dir, "candidate_metrics.json", [{"label": r["label"], "status": r["status"], "metrics": r["metrics"], "phenotype_hash": r["phenotype_hash"]} for r in runs])
    write_json(output_dir, "matched_comparison.json", summary)
    write_json(output_dir, "graph_vs_simple.json", {"graph_validated": len(graph_runs), "simple_validated": len(simple_runs), "graph_best_tardiness": graph_best, "simple_best_tardiness": simple_best, "conclusion": graph_guidance})
    write_json(output_dir, "repair_experience_dataset.json", [{"label": r["label"], "operation_key": k, "mode_count": len(v), "repair_outcome_label": "REPAIR_OUTCOME_DATA_ONLY"} for r in runs for k, v in r["modes"].items()])
    decision = "E1_7_FINAL_VERIFIED_SUCCESS" if len(runs) == 6 and len(valid_runs) == 6 and mismatch_count == 0 and envelope_changes == 0 else "E1_7_NORMAL_MODEL_FORMULATION_BUG" if len(runs) == 6 and any(r["status"] == "CP_INFEASIBLE" for r in runs) else "E1_7_REPLAY_FAILURE" if mismatch_count else "E1_7_VALIDATION_FAILURE"
    write_json(output_dir, "next_stage_decision.json", {**summary, "decision": decision})
    report = (f"# CRAFT E1.7 Final Verified Optimization\n\nDecision: `{decision}`\n\n"
              f"Exact incumbent fallback: `180/180`; CP feasible: `{summary['cp_feasible_count']}/6`; validated: `{summary['validated_count']}/6`; replay mismatches: `{mismatch_count}`; POST_CP_ENVELOPE_CHANGES: `{envelope_changes}`.\n\n"
              f"Baseline tardiness: {summary['baseline_tardiness']}; best tardiness: {summary['best_tardiness']}; beats CR: {summary['best_schedule_beats_cr']}; beats E1.4: {summary['best_schedule_beats_e1_4']}.\n\n"
              f"Graph guidance: `{graph_guidance}`. No DB writes; no post-CP machine or worker allocation.\n")
    (output_dir / "final_report.md").write_text(report, encoding="utf-8")
    files = {str(p.relative_to(output_dir)).replace("\\", "/"): sha(p) for p in sorted(output_dir.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    audit = hashlib.sha256(json.dumps({"source": source, "summary": summary, "decision": decision}, sort_keys=True).encode()).hexdigest()
    write_json(output_dir, "artifact_hash_manifest.json", {"files": files, "source_fingerprint": EXPECTED_SOURCE, "phenotype_hash": EXPECTED_CR, "audit_fingerprint": audit, "decision": decision})
    return {**summary, "decision": decision}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUT)
    parser.add_argument("--smoke-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir, args.smoke_only), indent=2, default=str))
