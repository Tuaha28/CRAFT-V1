"""CRAFT E1.5: bounded realization-consistent line+k repair.

This runner is intentionally a thin, isolated layer over the frozen E1.4
model/transaction.  It adds only deterministic execution-envelope evidence
and timing-specific feedback cuts; E1.4 artifacts are never modified.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

from crafter.e0_5_semantic_reconciliation import _safe
from crafter.phenotype import exported_rows, validate
from crafter.real_data_adapter import SNAPSHOT_NAME, build_real_window_fixture
from .transaction import frozen_incumbent

from .analysis import build_incumbent_index
from .line_k_model import build_line_k_model, solve_line_k
from .line_k_transaction import realize_line_k_transaction
from .neighborhoods import Neighborhood
from .run_e1_1 import CR_PATH, EXPECTED_CR, EXPECTED_SOURCE
from .run_e1_4 import _verify_manifest, _simple_select, _graph_select

E14_DIR = Path("outputs/crafter/e1_4_line_k_local_repair_retry5")
OUTPUT_DIR = Path("outputs/crafter/e1_5_realization_consistent_repair")


def _write_json(directory, name, value):
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_safe(value), indent=2, sort_keys=True, default=str) + "\n"
    path = directory / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(payload); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    return hashlib.sha256(payload.encode()).hexdigest()


def _write_text(directory, name, value):
    directory.mkdir(parents=True, exist_ok=True)
    payload = value if value.endswith("\n") else value + "\n"
    path = directory / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(payload); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    return hashlib.sha256(payload.encode()).hexdigest()


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _neighborhoods(raw):
    return [Neighborhood(**item) for item in raw]


def _failure_analysis():
    rows = json.loads((E14_DIR / "realization_attempts.json").read_text())
    result = []
    for run in rows:
        for attempt in run.get("attempts", []):
            if attempt.get("accepted") or attempt.get("stage") == "validated":
                continue
            machine = attempt.get("machine_failures", [])
            worker = attempt.get("worker_failures", [])
            witnesses = list(machine) + list(worker)
            if not witnesses:
                witnesses = [{"reason": attempt.get("stage", "other")}]
            for witness in witnesses:
                reason = str(witness.get("reason", "other"))
                if "outside machine boundary" in reason:
                    category = "FROZEN_MACHINE_BOUNDARY_CONFLICT"
                elif "local transition" in reason:
                    category = "LOCAL_MACHINE_SEQUENCE_CONFLICT"
                elif "extension" in reason:
                    category = "WORKER_EXTENSION_MACHINE_OVERLAP"
                elif "worker" in reason or "allocation" in reason:
                    category = "WORKER_CREW_UNAVAILABLE"
                else:
                    category = "OTHER"
                result.append({"label": run.get("label"), "attempt": attempt.get("attempt"),
                               "category": category, "operation_key": witness.get("operation_key"),
                               "left": witness.get("left"), "right": witness.get("right"),
                               "machine_id": witness.get("machine_id"),
                               "required_gap": witness.get("required_gap"),
                               "available_gap": witness.get("available_gap"),
                               "reason": reason, "stage": attempt.get("stage"),
                               "cp_start_seconds": witness.get("cp_start_seconds"),
                               "cp_end_seconds": witness.get("cp_end_seconds"),
                               "worker_realized_end_seconds": witness.get("worker_realized_end_seconds"),
                               "extension_seconds": witness.get("extension_seconds")})
    counts = {}
    for row in result:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    return {"categories": counts, "witnesses": result,
            "source": "outputs/crafter/e1_4_line_k_local_repair_retry5/realization_attempts.json",
            "note": "E1.4 did not persist CP assignment timing for every failed witness; null timing fields are provenance, not inferred values."}


def _option_fp(key, item):
    return (str(key), int(item.get("line_id", -1)), int(item.get("machine_count", -1)),
            int(item.get("start_seconds", -1)), int(item.get("end_seconds", -1)))


def _cuts_from_failure(diag, selected, released, attempt):
    cuts = []
    # Worker extension and boundary witnesses are timing-specific.  The next
    # solve may still use the same line+k mode at another event-aligned start.
    for key, delta in (diag.get("envelope_deltas") or {}).items():
        if float(delta.get("extension_seconds", 0)) > 0 and key in selected:
            cuts.append({"type": "WORKER_ENVELOPE_TIGHTENING",
                         "operations": [key], "line": selected[key].get("line_id"),
                         "k": selected[key].get("machine_count"),
                         "fingerprint": list(_option_fp(key, selected[key])),
                         "attempt": attempt, "evidence": delta,
                         "reason": "exact worker calendar extended machine envelope"})
    for witness in diag.get("machine_failures", []) or []:
        key = witness.get("operation_key")
        keys = []
        if key in selected:
            keys.append(key)
        if witness.get("reason") == "local transition":
            for candidate in (witness.get("left"), witness.get("right")):
                if candidate in selected and candidate not in keys:
                    keys.append(candidate)
        for candidate in keys:
            cuts.append({"type": "TIMING_OPTION_NO_GOOD",
                         "operations": [candidate],
                         "line": selected[candidate].get("line_id"),
                         "k": selected[candidate].get("machine_count"),
                         "fingerprint": list(_option_fp(candidate, selected[candidate])),
                         "attempt": attempt, "evidence": witness,
                         "reason": "demonstrated machine boundary/sequence conflict"})
    for witness in diag.get("worker_failures", []) or []:
        key = witness.get("operation_key")
        if key in selected:
            cuts.append({"type": "WORKER_TIMING_NO_GOOD", "operations": [key],
                         "line": selected[key].get("line_id"),
                         "k": selected[key].get("machine_count"),
                         "fingerprint": list(_option_fp(key, selected[key])),
                         "attempt": attempt, "evidence": witness,
                         "reason": "worker realization failed for this exact timing"})
    unique, seen = [], set()
    for cut in cuts:
        fp = tuple(cut["fingerprint"])
        if fp in seen:
            continue
        seen.add(fp); unique.append(cut)
    return unique


def _run_arm(fixture, baseline, index, neighborhood, released, domains,
             output_dir, label, *, seconds_limit=30.0, max_attempts=3,
             exact_machine_sets=None):
    forbidden = set(); attempts = []; cp_results = []; cut_rows = []
    final_diag = final_candidate = None
    for attempt in range(1, max_attempts + 1):
        try:
            artifacts = build_line_k_model(
                fixture.problem, fixture.scheduler.op_details, fixture.operator_index,
                index, neighborhood, released, domains, forbidden_options=forbidden,
                exact_machine_sets=exact_machine_sets)
        except Exception as exc:
            final_diag = {"accepted": False, "stage": "model_build", "error": repr(exc),
                          "rollback_hash": baseline.phenotype_hash}
            attempts.append({"attempt": attempt, **final_diag}); break
        preview_records = [{"operation_key": option.operation_key,
                           "option_id": option.option_id,
                           "line_id": option.line_id,
                           "k": option.machine_count,
                           "start_seconds": option.start_seconds,
                           "productive_start_seconds": option.productive_start_seconds,
                           "preview_end_seconds": option.end_seconds,
                           "productive_seconds": option.productive_seconds,
                           "setup_seconds": option.setup_seconds,
                           "preview_worker_ids": list(option.preview_worker_ids),
                           "preview_segments": list(option.preview_segments),
                           "candidate_machine_ids": list(option.candidate_machine_ids),
                           "incumbent": option.incumbent,
                           "attempt": attempt, "label": label}
                          for key in sorted(artifacts.options)
                          for option in artifacts.options[key]]
        stats = {"attempt": attempt, **asdict(artifacts.size),
                 "forbidden_option_count": len(forbidden)}
        solve, selected = solve_line_k(artifacts, seed=42, seconds_limit=seconds_limit,
                                       log_path=output_dir / "logs" / f"{label}_attempt{attempt}.log")
        solve.update({"attempt": attempt, "label": label, "model_preflight": stats})
        cp_results.append(solve)
        if selected is None:
            final_diag = {"accepted": False, "stage": "local_cp", "reason": solve["status"],
                          "rollback_hash": baseline.phenotype_hash}
            attempts.append({"attempt": attempt, **final_diag}); break
        diag, candidate = realize_line_k_transaction(
            fixture.problem, fixture.operator_index, fixture.scheduler.op_details,
            index, neighborhood.operation_keys, selected, baseline)
        diag.update({"attempt": attempt, "label": label})
        diag["execution_envelope_previews"] = preview_records
        attempts.append(diag); final_diag, final_candidate = diag, candidate
        if diag.get("accepted") or diag.get("validator_valid"):
            break
        if attempt >= max_attempts:
            break
        cuts = _cuts_from_failure(diag, selected, released, attempt)
        if not cuts:
            break
        changed = False
        for cut in cuts:
            fp = tuple(cut["fingerprint"])
            if fp not in forbidden:
                forbidden.add(fp); changed = True; cut_rows.append(cut)
        if not changed:
            break
    return {"label": label, "released_keys": sorted(released), "attempts": attempts,
            "cp_results": cp_results, "final": final_diag, "candidate": final_candidate,
            "feedback_cuts": cut_rows, "forbidden_option_count": len(forbidden),
            "execution_envelope_previews": [p for a in attempts for p in a.get("execution_envelope_previews", [])]}


def run(output_dir=OUTPUT_DIR, *, smoke_only=False):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    if not E14_DIR.exists():
        raise RuntimeError("E1.4 reference directory missing")
    fixture = build_real_window_fixture(order_limit=50)
    payload = json.loads(CR_PATH.read_text(encoding="utf-8"))
    rows = payload["decoder_details"]["phenotype_operations"]
    baseline = validate(fixture.problem, frozen_incumbent(fixture.problem, rows), fixture.operator_index)
    source = {"snapshot": fixture.source_metadata["snapshot_name"],
              "expected_source_fingerprint": EXPECTED_SOURCE,
              "actual_source_fingerprint": fixture.source_fingerprint,
              "expected_phenotype_hash": EXPECTED_CR, "actual_phenotype_hash": baseline.phenotype_hash,
              "operations": len(fixture.problem.operations), "orders": len(fixture.problem.orders),
              "baseline_status": baseline.status.value,
              "baseline_valid": bool(baseline.validation_report.get("valid")), "database_writes": 0,
              "e14_manifest": _verify_manifest(E14_DIR)}
    if not (source["snapshot"] == SNAPSHOT_NAME and source["actual_source_fingerprint"] == EXPECTED_SOURCE
            and source["actual_phenotype_hash"] == EXPECTED_CR and source["operations"] == 431
            and source["baseline_valid"] and source["e14_manifest"]["pass"]):
        raise RuntimeError("E1.5 frozen source/E1.4 artifact mismatch")
    e14_failures = _failure_analysis()
    index = build_incumbent_index(fixture.problem, rows, fixture.scheduler.op_details)
    neighborhoods = _neighborhoods(json.loads((E14_DIR/"neighborhoods.json").read_text()))
    line_k_rows = json.loads((Path("outputs/crafter/e1_3_freedom_audit")/"line_k_opportunities.json").read_text())
    domain_payload = json.loads((E14_DIR/"candidate_domains.json").read_text())
    domains = {key: tuple(value["modes"]) for key, value in domain_payload.items()}
    simple = {str(k): v for k,v in json.loads((E14_DIR/"simple_selector.json").read_text()).items()}
    graph_sel = {str(k): v for k,v in json.loads((E14_DIR/"graph_selector.json").read_text()).items()}
    _write_json(output_dir, "source_identity.json", source)
    _write_json(output_dir, "e14_failure_analysis.json", e14_failures)
    _write_json(output_dir, "neighborhoods.json", [n.as_dict() for n in neighborhoods])
    _write_json(output_dir, "simple_selector.json", simple)
    _write_json(output_dir, "graph_selector.json", graph_sel)
    _write_json(output_dir, "candidate_domains.json", domain_payload)
    preview_rows = []

    # Required smoke: exact E1.4 best GRAPH rank0 first.
    runs = []
    smoke = _run_arm(fixture, baseline, index, neighborhoods[0],
                     set(graph_sel[str(neighborhoods[0].seed)]), domains,
                     output_dir, f"graph_rank0_seed{neighborhoods[0].seed}")
    runs.append(smoke)
    smoke_final = smoke.get("final") or {}
    smoke_ok = bool(smoke_final.get("validator_valid"))
    stop_after_smoke = not smoke_ok and not smoke_final.get("accepted")
    if smoke_only:
        stop_after_smoke = True
    if not stop_after_smoke:
        runs = []
        for n in neighborhoods:
            for arm, selections in (("simple", simple), ("graph", graph_sel)):
                label = f"{arm}_rank{neighborhoods.index(n)}_seed{n.seed}"
                runs.append(_run_arm(fixture, baseline, index, n,
                                     set(selections[str(n.seed)]), domains, output_dir, label))
    # Flatten diagnostics and preserve every attempted preview/realization datum.
    cp_all = [item for run_result in runs for item in run_result.get("cp_results", [])]
    attempt_all = [{"label": r["label"], **a} for r in runs for a in r.get("attempts", [])]
    cuts = [c for r in runs for c in r.get("feedback_cuts", [])]
    preview_rows = [p for r in runs for p in r.get("execution_envelope_previews", [])]
    machine = [{"label": r["label"], "attempt": a.get("attempt"),
                "success": a.get("machine_verification_success"),
                "failures": a.get("machine_failures", [])} for r in runs for a in r.get("attempts", [])]
    worker = [{"label": r["label"], "attempt": a.get("attempt"),
               "success": a.get("worker_feasible"), "failures": a.get("worker_failures", []),
               "envelope_deltas": a.get("envelope_deltas", {})} for r in runs for a in r.get("attempts", [])]
    metrics = []
    validation = []
    experiences = []
    for r in runs:
        f = r.get("final") or {}
        metrics.append({"label": r["label"], "before": f.get("before"), "after": f.get("after"),
                        "accepted": f.get("accepted"), "improvement_seconds": f.get("improvement_seconds"),
                        "stage": f.get("stage"), "phenotype_hash": f.get("phenotype_hash"),
                        "rollback_hash": f.get("rollback_hash"), "envelope_deltas": f.get("envelope_deltas", {})})
        validation.append({"label": r["label"], "status": f.get("validator_status"),
                           "valid": f.get("validator_valid"), "report": f.get("validator_report")})
        for key in r.get("released_keys", []):
            experiences.append({"label": r["label"], "operation_key": key,
                                "repair_outcome_label": "REPAIR_OUTCOME_DATA_ONLY",
                                "cp_status": (r.get("cp_results", [{}])[-1].get("status")),
                                "validator_valid": f.get("validator_valid"),
                                "tardiness_delta_seconds": f.get("improvement_seconds"),
                                "feedback_cuts": len(r.get("feedback_cuts", []))})
    # Envelope accuracy is measured for all successful worker rechecks.
    errors = [float(d.get("extension_seconds", 0)) for a in attempt_all for d in (a.get("envelope_deltas") or {}).values()]
    abs_errors = sorted(abs(v) for v in errors)
    p90 = abs_errors[min(len(abs_errors)-1, int(0.9*len(abs_errors)))] if abs_errors else 0
    envelope = {"count": len(errors), "median_absolute_error_seconds": abs_errors[len(abs_errors)//2] if abs_errors else 0,
                "p90_absolute_error_seconds": p90, "maximum_error_seconds": max(errors, default=0),
                "underestimated_count": sum(v>0 for v in errors), "exact_count": sum(v==0 for v in errors),
                "overestimated_count": sum(v<0 for v in errors), "errors": errors,
                "comparison_note": "E1.4 persisted no per-operation envelope deltas; this run is the authoritative E1.5 measurement."}
    _write_json(output_dir, "cp_model_stats.json", cp_all)
    _write_json(output_dir, "cp_solver_results.json", cp_all)
    _write_json(output_dir, "realization_attempts.json", attempt_all)
    _write_json(output_dir, "feedback_cuts.json", cuts)
    _write_json(output_dir, "execution_envelope_previews.json", preview_rows)
    _write_json(output_dir, "machine_realization.json", machine)
    _write_json(output_dir, "worker_realization.json", worker)
    _write_json(output_dir, "envelope_accuracy.json", envelope)
    _write_json(output_dir, "validation_results.json", validation)
    _write_json(output_dir, "candidate_metrics.json", metrics)
    _write_json(output_dir, "repair_experience_dataset.json", experiences)
    valid = [m for m in metrics if m.get("after") and m.get("stage") in ("validated",)]
    improved = [m for m in valid if float(m.get("improvement_seconds") or 0) > 0]
    success_count = len(valid)
    if success_count >= 5 and improved:
        decision = "E1_5_REALIZATION_RELIABILITY_IMPROVED"
    elif success_count > 0 and not improved:
        decision = "E1_5_QUALITY_REGRESSION" if success_count >= 5 else "E1_5_NO_RELIABILITY_GAIN"
    elif cp_all and all(x.get("status") not in ("OPTIMAL", "FEASIBLE") for x in cp_all):
        decision = "E1_5_SEMANTIC_FAILURE"
    else:
        decision = "E1_5_NO_RELIABILITY_GAIN"
    best = min((m for m in valid if m.get("after")), key=lambda m: m["after"]["total_tardiness_seconds"], default=None)
    comparison = {"decision": decision, "baseline_tardiness_seconds": 24521169,
                  "e14_best_tardiness_seconds": 24177486, "realization_success_count": success_count,
                  "transaction_count": len(runs), "success_rate": success_count/max(1,len(runs)),
                  "success_on_attempt": {str(i): sum(1 for r in runs if any(a.get("attempt")==i and a.get("validator_valid") for a in r.get("attempts", []))) for i in (1,2,3)},
                  "best": best, "best_schedule_beats_cr": bool(best and best["after"]["total_tardiness_seconds"] < 24521169),
                  "best_schedule_beats_e1_4": bool(best and best["after"]["total_tardiness_seconds"] < 24177486)}
    _write_json(output_dir, "matched_comparison.json", comparison)
    _write_json(output_dir, "next_stage_decision.json", comparison)
    report = ("# CRAFT E1.5 Realization-Consistent Repair\n\n"+
              f"- Decision: `{decision}`\n- Smoke passed: `{smoke_ok}`\n- Transactions: `{len(runs)}`\n"+
              f"- Realization/validation success: `{success_count}/{len(runs)}`\n"+
              f"- Best schedule beats CR: `{comparison['best_schedule_beats_cr']}`\n"+
              f"- Best schedule beats E1.4: `{comparison['best_schedule_beats_e1_4']}`\n\n"+
              "E1.4 remained immutable. No production integration, database write, global CP solve, or GNN training was performed.\n")
    _write_text(output_dir, "final_report.md", report)
    files = {}
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "artifact_hash_manifest.json":
            files[str(path.relative_to(output_dir)).replace("\\", "/")] = _sha(path)
    audit = hashlib.sha256(json.dumps({"source": source, "decision": decision,
                                       "metrics": metrics, "cuts": cuts}, sort_keys=True, default=str).encode()).hexdigest()
    _write_json(output_dir, "artifact_hash_manifest.json", {"source_fingerprint": source["actual_source_fingerprint"],
        "phenotype_hash": baseline.phenotype_hash, "audit_fingerprint": audit,
        "decision": decision, "files": files})
    return {"decision": decision, "smoke_passed": smoke_ok, "transactions": len(runs),
            "success_count": success_count, "audit_fingerprint": audit, "output_dir": str(output_dir)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--smoke-only", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.output_dir, smoke_only=args.smoke_only), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
