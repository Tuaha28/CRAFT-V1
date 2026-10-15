"""CRAFT E1.6 exact bounded physical-machine option experiment."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

from crafter.e0_5_semantic_reconciliation import _safe
from crafter.phenotype import validate
from crafter.real_data_adapter import SNAPSHOT_NAME, build_real_window_fixture
from .analysis import build_incumbent_index
from .exact_machine_model import generate_machine_set_candidates
from .line_k_model import build_line_k_model
from .run_e1_1 import CR_PATH, EXPECTED_CR, EXPECTED_SOURCE
from .run_e1_4 import _verify_manifest
from .run_e1_5 import _neighborhoods, _failure_analysis, _run_arm
from .transaction import frozen_incumbent

E14 = Path("outputs/crafter/e1_4_line_k_local_repair_retry5")
E15 = Path("outputs/crafter/e1_5_realization_consistent_repair")
OUT = Path("outputs/crafter/e1_6_exact_machine_local_cp")


def write_json(directory, name, value):
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_safe(value), indent=2, sort_keys=True, default=str) + "\n"
    path = directory / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(payload); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prior():
    return {"e14_manifest": _verify_manifest(E14), "e15_manifest": _verify_manifest(E15),
            "e14_best_tardiness": 24177486, "e15_status": "E1_5_NO_RELIABILITY_GAIN",
            "e15_failure_taxonomy": json.loads((E15/"e14_failure_analysis.json").read_text())["categories"]}


def _serialize_sets(provenance):
    rows = []
    for label, operations in provenance.items():
      for key, modes in operations.items():
        for mode, value in modes.items():
            rows.append({"label": label, "operation_key": key,
                         "line_id": mode[0], "k": mode[1], **value})
    return rows


def run(output_dir=OUT, *, smoke_only=False):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    prior = _prior()
    if not (prior["e14_manifest"]["pass"] and prior["e15_manifest"]["pass"]):
        raise RuntimeError("prior artifact manifest mismatch")
    fixture = build_real_window_fixture(order_limit=50)
    rows = json.loads(CR_PATH.read_text())["decoder_details"]["phenotype_operations"]
    baseline = validate(fixture.problem, frozen_incumbent(fixture.problem, rows), fixture.operator_index)
    source = {"snapshot": fixture.source_metadata["snapshot_name"],
              "expected_source_fingerprint": EXPECTED_SOURCE,
              "actual_source_fingerprint": fixture.source_fingerprint,
              "expected_phenotype_hash": EXPECTED_CR, "actual_phenotype_hash": baseline.phenotype_hash,
              "orders": len(fixture.problem.orders), "operations": len(fixture.problem.operations),
              "baseline_status": baseline.status.value, "baseline_valid": bool(baseline.validation_report.get("valid")),
              "database_writes": 0}
    if not (source["snapshot"] == SNAPSHOT_NAME and source["actual_source_fingerprint"] == EXPECTED_SOURCE
            and source["actual_phenotype_hash"] == EXPECTED_CR and source["operations"] == 431
            and source["baseline_valid"]):
        raise RuntimeError("E1.6 frozen identity mismatch")
    index = build_incumbent_index(fixture.problem, rows, fixture.scheduler.op_details)
    neighborhoods = _neighborhoods(json.loads((E14/"neighborhoods.json").read_text()))
    simple = json.loads((E14/"simple_selector.json").read_text())
    graph = json.loads((E14/"graph_selector.json").read_text())
    domains = {k: tuple(v["modes"]) for k,v in json.loads((E14/"candidate_domains.json").read_text()).items()}
    write_json(output_dir, "source_identity.json", source)
    write_json(output_dir, "prior_result_verification.json", prior)
    write_json(output_dir, "neighborhoods.json", [n.as_dict() for n in neighborhoods])
    write_json(output_dir, "simple_selector.json", simple)
    write_json(output_dir, "graph_selector.json", graph)
    write_json(output_dir, "line_k_domains.json", json.loads((E14/"candidate_domains.json").read_text()))

    all_runs = []
    all_sets = {}
    all_provenance = {}
    # Build the bounded exact sets once per matched arm/neighborhood.  The
    # generator is independent of selector ranking and therefore fair.
    for n in neighborhoods:
        for arm, selector in (("simple", simple), ("graph", graph)):
            released = set(selector[str(n.seed)])
            sets, prov = generate_machine_set_candidates(
                fixture.problem, index, n, released,
                {key: domains[key] for key in n.operation_keys if key in domains}, max_sets=3)
            label = f"{arm}_rank{neighborhoods.index(n)}_seed{n.seed}"
            all_sets[label] = sets; all_provenance[label] = prov
            if smoke_only and label != f"graph_rank0_seed{n.seed}":
                continue
            result = _run_arm(fixture, baseline, index, n, released, domains, output_dir,
                              label, exact_machine_sets=sets)
            result["machine_set_provenance"] = prov
            all_runs.append(result)
            if smoke_only:
                break
        if smoke_only and all_runs:
            break
    write_json(output_dir, "machine_set_candidates.json", _serialize_sets({k:v for k,v in all_provenance.items()}))
    execution_options = [p for r in all_runs for p in r.get("execution_envelope_previews", [])]
    write_json(output_dir, "physical_execution_options.json", execution_options)
    write_json(output_dir, "worker_preview_envelopes.json", execution_options)
    # Boundary/conflict diagnostics are explicit summary records; exact CP
    # constraints remain in the model and are independently rechecked.
    boundaries = []
    for label, prov in all_provenance.items():
        for key, modes in prov.items():
            for mode, data in modes.items():
                boundaries.append({"label": label, "operation_key": key,
                                   "line_id": mode[0], "k": mode[1],
                                   "machine_sets": data.get("sets", []),
                                   "screened_out": data.get("removed")})
    write_json(output_dir, "frozen_machine_boundaries.json", boundaries)
    write_json(output_dir, "local_machine_conflicts.json", [
        {"label": r["label"], "attempt": x.get("attempt"),
         "exact_machine_conflict_constraints": x.get("model_preflight", {}).get("exact_machine_conflict_constraints", 0),
         "machine_option_count": len(r.get("machine_set_provenance", {}))}
        for r in all_runs for x in r.get("cp_results", [])])

    cp = [x for r in all_runs for x in r.get("cp_results", [])]
    attempts = [{"label": r["label"], **a} for r in all_runs for a in r.get("attempts", [])]
    cuts = [c for r in all_runs for c in r.get("feedback_cuts", [])]
    machine_checks = [{"label": r["label"], "attempt": a.get("attempt"),
                       "success": a.get("machine_verification_success"),
                       "failures": a.get("machine_failures", [])} for r in all_runs for a in r.get("attempts", [])]
    workers = [{"label": r["label"], "attempt": a.get("attempt"),
                "success": a.get("worker_feasible"), "failures": a.get("worker_failures", []),
                "envelope_deltas": a.get("envelope_deltas", {})} for r in all_runs for a in r.get("attempts", [])]
    validations = [{"label": r["label"], "status": (r.get("final") or {}).get("validator_status"),
                   "valid": (r.get("final") or {}).get("validator_valid"),
                   "report": (r.get("final") or {}).get("validator_report")} for r in all_runs]
    metrics = [{"label": r["label"], "before": (r.get("final") or {}).get("before"),
                "after": (r.get("final") or {}).get("after"), "accepted": (r.get("final") or {}).get("accepted"),
                "improvement_seconds": (r.get("final") or {}).get("improvement_seconds"),
                "stage": (r.get("final") or {}).get("stage"), "phenotype_hash": (r.get("final") or {}).get("phenotype_hash"),
                "machine_set_options": sum(len(data.get("sets", []))
                                            for modes in all_provenance.get(r["label"], {}).values()
                                            for data in modes.values())}
               for r in all_runs]
    write_json(output_dir, "cp_model_stats.json", cp)
    write_json(output_dir, "cp_solver_results.json", cp)
    write_json(output_dir, "realization_attempts.json", attempts)
    write_json(output_dir, "feedback_cuts.json", [c for r in all_runs for c in r.get("feedback_cuts", [])])
    write_json(output_dir, "worker_realization.json", workers)
    write_json(output_dir, "post_worker_machine_checks.json", machine_checks)
    write_json(output_dir, "validation_results.json", validations)
    write_json(output_dir, "candidate_metrics.json", metrics)
    experiences = [{"label": r["label"], "operation_key": key,
                    "candidate_machine_set_count": sum(len(v.get("sets", [])) for v in all_provenance.get(r["label"], {}).values()),
                    "repair_outcome_label": "REPAIR_OUTCOME_DATA_ONLY",
                    "validator_valid": (r.get("final") or {}).get("validator_valid"),
                    "tardiness_delta_seconds": (r.get("final") or {}).get("improvement_seconds")}
                   for r in all_runs for key in r.get("released_keys", [])]
    write_json(output_dir, "repair_experience_dataset.json", experiences)

    valid = [m for m in metrics if m.get("after") and m.get("stage") == "validated"]
    improved = [m for m in valid if float(m.get("improvement_seconds") or 0) > 0]
    option_count = sum(len(v) for by_mode in all_sets.values() for v in by_mode.values())
    explosion = any(x.get("model_preflight", {}).get("variables", 0) > 10000 for x in cp)
    if explosion: decision = "E1_6_MACHINE_OPTION_EXPLOSION"
    elif len(all_runs) == 6 and len(valid) >= 5 and improved: decision = "E1_6_EXACT_MACHINE_MODEL_SUCCESS"
    elif len(valid) >= 5: decision = "E1_6_QUALITY_REGRESSION"
    elif cp and all(x.get("status") not in ("OPTIMAL", "FEASIBLE") for x in cp): decision = "E1_6_MACHINE_OPTION_EXPLOSION"
    else: decision = "E1_6_NO_REALIZATION_GAIN"
    best = min(valid, key=lambda x: x["after"]["total_tardiness_seconds"], default=None)
    graph_valid = sum(1 for x in valid if x["label"].startswith("graph")); simple_valid = sum(1 for x in valid if x["label"].startswith("simple"))
    graph_best = max((float(x.get("improvement_seconds") or 0) for x in valid if x["label"].startswith("graph")), default=0)
    simple_best = max((float(x.get("improvement_seconds") or 0) for x in valid if x["label"].startswith("simple")), default=0)
    graph_conclusion = "GRAPH_GUIDANCE_PROMISING" if graph_valid > simple_valid or graph_best > simple_best else ("GRAPH_GUIDANCE_NO_ADVANTAGE" if graph_valid == simple_valid and graph_best == simple_best else "GRAPH_GUIDANCE_INCONCLUSIVE")
    summary = {"decision": decision, "transaction_count": len(all_runs), "validated_count": len(valid),
               "success_rate": len(valid)/max(1,len(all_runs)), "machine_set_option_count": option_count,
               "best": best, "best_schedule_beats_cr": bool(best and best["after"]["total_tardiness_seconds"] < 24521169),
               "best_schedule_beats_e1_4": bool(best and best["after"]["total_tardiness_seconds"] < 24177486),
               "graph_validated_count": graph_valid, "simple_validated_count": simple_valid,
               "graph_best_improvement": graph_best, "simple_best_improvement": simple_best,
               "graph_guidance": graph_conclusion, "smoke_only": smoke_only}
    write_json(output_dir, "matched_comparison.json", summary)
    write_json(output_dir, "next_stage_decision.json", summary)
    write_json(output_dir, "prior_result_verification.json", {**prior, "e1_6_machine_set_option_count": option_count})
    report = ("# CRAFT E1.6 Exact-Machine Local CP\n\n" +
              f"- Decision: `{decision}`\n- Transactions: `{len(all_runs)}`\n- Validated: `{len(valid)}/{len(all_runs)}`\n"+
              f"- Machine-set options: `{option_count}`\n- Best beats CR: `{summary['best_schedule_beats_cr']}`\n"+
              f"- Best beats E1.4: `{summary['best_schedule_beats_e1_4']}`\n- Graph: `{graph_conclusion}`\n\n"+
              "Exact physical machine sets were bounded constructive alternatives; workers and the independent validator remained authoritative.\n")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir/"final_report.md").write_text(report, encoding="utf-8")
    files = {str(p.relative_to(output_dir)).replace("\\", "/"): sha(p)
             for p in sorted(output_dir.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    audit = hashlib.sha256(json.dumps({"source": source, "summary": summary, "metrics": metrics}, sort_keys=True, default=str).encode()).hexdigest()
    write_json(output_dir, "artifact_hash_manifest.json", {"source_fingerprint": source["actual_source_fingerprint"],
        "phenotype_hash": baseline.phenotype_hash, "audit_fingerprint": audit, "decision": decision, "files": files})
    return {"decision": decision, "transactions": len(all_runs), "validated": len(valid),
            "audit_fingerprint": audit, "output_dir": str(output_dir)}


def main(argv=None):
    parser = argparse.ArgumentParser(); parser.add_argument("--output-dir", type=Path, default=OUT); parser.add_argument("--smoke-only", action="store_true")
    args = parser.parse_args(argv); print(json.dumps(run(args.output_dir, smoke_only=args.smoke_only), indent=2))


if __name__ == "__main__": main()
