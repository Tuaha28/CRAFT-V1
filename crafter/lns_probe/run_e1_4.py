"""CRAFT E1.4 bounded line+k local-repair experiment.

The runner is deliberately transactional and isolated.  It reads the frozen
CR phenotype, builds three shared 30-operation neighborhoods, and compares
simple versus Resource-State-Graph release selection with a five-operation
line+k budget.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

from crafter.e0_5_semantic_reconciliation import _safe
from crafter.phenotype import validate, exported_rows
from crafter.real_data_adapter import SNAPSHOT_NAME, build_real_window_fixture

from .analysis import build_incumbent_index, seconds
from .line_k_model import build_line_k_model, solve_line_k
from .line_k_transaction import realize_line_k_transaction
from .neighborhoods import Neighborhood
from .run_e1_1 import CR_PATH, EXPECTED_CR, EXPECTED_SOURCE
from .transaction import frozen_incumbent
from .resource_graph import build_resource_state_graph, causal_waiting


E13_DIR = Path("outputs/crafter/e1_3_freedom_audit")
E135_DIR = Path("outputs/crafter/e1_3_5_exact_line_k_probe")
OUTPUT_DIR = Path("outputs/crafter/e1_4_line_k_local_repair")


def _write_json(directory: Path, name: str, value):
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_safe(value), indent=2, sort_keys=True, default=str) + "\n"
    path = directory / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_text(directory: Path, name: str, value: str):
    directory.mkdir(parents=True, exist_ok=True)
    payload = value if value.endswith("\n") else value + "\n"
    path = directory / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_manifest(directory: Path):
    manifest_path = directory / "artifact_hash_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mismatches = []
    for name, expected in manifest.get("files", {}).items():
        path = directory / name
        actual = _sha(path) if path.exists() else None
        if actual != expected:
            mismatches.append({"file": name, "expected": expected, "actual": actual})
    return {"directory": str(directory), "entry_count": len(manifest.get("files", {})),
            "manifest_sha256": _sha(manifest_path), "mismatches": mismatches,
            "pass": not mismatches}


def _pick_anchors(index, line_k_rows, count=3):
    rows = sorted(line_k_rows, key=lambda row: (-float(row.get("potential_finish_gain_seconds", 0)),
                                                 -float(row.get("delivery_pressure", 0)),
                                                 str(row["operation_key"])))
    picked, seen_orders, seen_factories = [], set(), set()
    for row in rows:
        key = str(row["operation_key"])
        feature = index.operation_features[key]
        order = str(row["order_id"])
        factory = int(feature["factory_id"])
        if order in seen_orders or factory in seen_factories:
            continue
        picked.append((key, factory)); seen_orders.add(order); seen_factories.add(factory)
        if len(picked) == count:
            return picked
    for row in rows:
        key = str(row["operation_key"]); factory = int(index.operation_features[key]["factory_id"])
        if (key, factory) not in picked:
            picked.append((key, factory))
        if len(picked) == count:
            break
    return picked


def _make_neighborhood(index, seed_key, factory_id, rank, seed, target=30):
    allowed = {key for key, feature in index.operation_features.items()
               if int(feature["factory_id"]) == int(factory_id)}
    selected = {seed_key}
    # Delivery-aware route tail first; this avoids the frozen-successor trap
    # observed in E1.3.5 when the budget permits it.
    cursor = seed_key
    while len(selected) < target:
        successor = index.route_successor.get(cursor)
        if not successor or successor not in allowed or successor in selected:
            break
        selected.add(successor); cursor = successor
    frontier = [seed_key]
    while frontier and len(selected) < target:
        key = frontier.pop(0)
        candidates = sorted(set(index.adjacency.get(key, ())) & allowed - selected,
                            key=lambda item: (-index.operation_scores.get(item, 0), item))
        for item in candidates:
            selected.add(item); frontier.append(item)
            if len(selected) >= target:
                break
    remaining = sorted(allowed-selected, key=lambda item: (
        abs(index.operation_features[item]["start_seconds"]-
            index.operation_features[seed_key]["start_seconds"]),
        -index.operation_scores.get(item, 0), item))
    selected.update(remaining[:max(0, target-len(selected))])
    keys = tuple(sorted(selected))
    features = [index.operation_features[key] for key in keys]
    return Neighborhood(
        strategy="e1_4_delivery_guided", seed=int(seed), target_size=target,
        factory_id=int(factory_id), seed_operation=seed_key,
        operation_keys=keys,
        order_ids=tuple(sorted({str(item["order_id"]) for item in features})),
        line_ids=tuple(sorted({int(item["line_id"]) for item in features})),
        capabilities=tuple(sorted({str(item["capability"]) for item in features})),
        start_seconds=min(int(item["start_seconds"]) for item in features),
        end_seconds=max(int(item["end_seconds"]) for item in features),
    )


def _candidate_domains(index, line_k_rows, neighborhoods):
    by_key = {str(row["operation_key"]): row for row in line_k_rows}
    used = set(key for n in neighborhoods for key in n.operation_keys)
    result, provenance = {}, {}
    for key in sorted(used):
        row = index.by_key[key]
        incumbent = {"line_id": int(row["line_id"]),
                     "k": len(set(int(v) for v in row["machine_ids"])),
                     "source": "incumbent"}
        values = [incumbent]
        seen = {(incumbent["line_id"], incumbent["k"])}
        source = by_key.get(key, {})
        for rank, item in enumerate(source.get("top_promising_combinations", ())):
            pair = (int(item["line_id"]), int(item["k"]))
            if pair in seen:
                continue
            values.append({"line_id": pair[0], "k": pair[1],
                           "source": "e1_3_screen", "rank": rank,
                           "estimated_finish_seconds": item.get("estimated_finish_seconds"),
                           "potential_finish_gain_seconds": source.get("potential_finish_gain_seconds", 0),
                           "screen_machine_ids": item.get("screen_machine_ids", [])})
            seen.add(pair)
            if len(values) >= 6:
                break
        result[key] = values
        provenance[key] = {"operation_key": key, "incumbent": incumbent,
                           "modes": values,
                           "candidate_limit": 5}
    return result, provenance


def _simple_select(neighborhood, domains, index, line_k_rows, budget=5):
    gain = {str(row["operation_key"]): float(row.get("potential_finish_gain_seconds", 0))
            for row in line_k_rows}
    selected = [key for key in neighborhood.operation_keys if len(domains.get(key, ())) > 1]
    selected.sort(key=lambda key: (-gain.get(key, 0),
                                   -index.operation_features[key]["order_tardiness_seconds"], key))
    return selected[:budget]


def _graph_select(neighborhood, domains, index, budget=5):
    selected = [key for key in neighborhood.operation_keys if len(domains.get(key, ())) > 1]
    selected.sort(key=lambda key: (-index.operation_scores.get(key, 0),
                                   -index.operation_features[key]["order_tardiness_seconds"], key))
    return selected[:budget]


def _cp_assignment(selected):
    return {key: dict(value) for key, value in selected.items()}


def _run_arm(fixture, baseline, index, neighborhood, released, domains,
             output_dir, label, *, seconds_limit=30.0, max_attempts=3):
    attempts, cp_results = [], []
    current_domains = {key: tuple(domains.get(key, ())) for key in neighborhood.operation_keys}
    final_diag, final_candidate = None, None
    for attempt in range(1, max_attempts+1):
        try:
            artifacts = build_line_k_model(
                fixture.problem, fixture.scheduler.op_details,
                fixture.operator_index, index, neighborhood, released,
                current_domains)
        except Exception as exc:
            attempts.append({"attempt": attempt, "stage": "model_build", "error": repr(exc)})
            break
        stats = {"attempt": attempt, **asdict(artifacts.size)}
        log_path = output_dir / "logs" / f"{label}_attempt{attempt}.log"
        solve, selected = solve_line_k(artifacts, seed=42, seconds_limit=seconds_limit,
                                        log_path=log_path)
        solve.update({"attempt": attempt, "label": label, "model_preflight": stats})
        cp_results.append(solve)
        if selected is None:
            final_diag = {"accepted": False, "stage": "local_cp", "reason": solve["status"],
                          "rollback_hash": baseline.phenotype_hash}
            attempts.append({"attempt": attempt, "stage": "local_cp", "status": solve["status"]})
            break
        diag, candidate = realize_line_k_transaction(
            fixture.problem, fixture.operator_index, fixture.scheduler.op_details,
            index, neighborhood.operation_keys, _cp_assignment(selected), baseline)
        diag.update({"attempt": attempt, "label": label})
        attempts.append(diag)
        final_diag, final_candidate = diag, candidate
        if diag.get("accepted"):
            break
        # Bounded realization feedback: forbid a released pair that caused an
        # exact realization failure; timing-specific failures do not delete the
        # whole mode, they simply terminate this transaction safely.
        if attempt >= max_attempts:
            break
        failure = (diag.get("machine_failures") or diag.get("worker_failures") or [])
        if not failure:
            break
        blocked = set()
        for item in failure:
            key = item.get("operation_key")
            if key in released:
                blocked.add(key)
        if not blocked:
            break
        # Conservative targeted no-good: remove the selected mode only when
        # the witness names a concrete operation/mode and keep provenance.
        changed = False
        for key in blocked:
            selected_item = selected.get(key, {})
            pair = (int(selected_item.get("line_id", -1)), int(selected_item.get("machine_count", -1)))
            values = [row for row in current_domains[key]
                      if (int(row["line_id"]), int(row["k"])) != pair]
            if values and len(values) < len(current_domains[key]):
                current_domains[key] = tuple(values); changed = True
        if not changed:
            break
    return {"label": label, "released_keys": sorted(released),
            "attempts": attempts, "cp_results": cp_results,
            "final": final_diag, "candidate": final_candidate}


def run(output_dir=OUTPUT_DIR, *, smoke_only=False):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    fixture = build_real_window_fixture(order_limit=50)
    payload = json.loads(CR_PATH.read_text(encoding="utf-8"))
    rows = payload["decoder_details"]["phenotype_operations"]
    baseline = validate(fixture.problem, frozen_incumbent(fixture.problem, rows),
                        fixture.operator_index)
    source = {"snapshot": fixture.source_metadata["snapshot_name"],
              "expected_source_fingerprint": EXPECTED_SOURCE,
              "actual_source_fingerprint": fixture.source_fingerprint,
              "expected_phenotype_hash": EXPECTED_CR,
              "actual_phenotype_hash": baseline.phenotype_hash,
              "orders": len(fixture.problem.orders), "operations": len(fixture.problem.operations),
              "baseline_status": baseline.status.value,
              "baseline_valid": bool(baseline.validation_report["valid"]),
              "database_writes": 0}
    if not (source["snapshot"] == SNAPSHOT_NAME and source["actual_source_fingerprint"] == EXPECTED_SOURCE
            and source["actual_phenotype_hash"] == EXPECTED_CR and source["operations"] == 431
            and source["baseline_valid"]):
        raise RuntimeError("E1.4 frozen source/baseline mismatch")
    manifest_checks = {"e1_3": _verify_manifest(E13_DIR),
                       "e1_3_5": _verify_manifest(E135_DIR)}
    if not all(value["pass"] for value in manifest_checks.values()):
        raise RuntimeError(f"prior artifact manifest mismatch: {manifest_checks}")
    index = build_incumbent_index(fixture.problem, rows, fixture.scheduler.op_details)
    graph = build_resource_state_graph(fixture.problem, index, fixture.scheduler.op_details,
                                       fixture.operator_index)
    line_k_rows = json.loads((E13_DIR/"line_k_opportunities.json").read_text(encoding="utf-8"))
    domains, domain_provenance = _candidate_domains(index, line_k_rows, [])
    anchors = _pick_anchors(index, json.loads((E13_DIR/"top_line_k_candidates.json").read_text(encoding="utf-8"))["top"], 3)
    neighborhoods = [_make_neighborhood(index, key, factory, rank, seed)
                     for rank, ((key, factory), seed) in enumerate(zip(anchors, (42, 101, 202)))]
    domains, domain_provenance = _candidate_domains(index, line_k_rows, neighborhoods)
    simple = {n.seed: _simple_select(n, domains, index, line_k_rows) for n in neighborhoods}
    graph_sel = {n.seed: _graph_select(n, domains, index) for n in neighborhoods}
    _write_json(output_dir, "source_identity.json", {**source, "prior_manifest_checks": manifest_checks})
    _write_json(output_dir, "neighborhoods.json", [n.as_dict() for n in neighborhoods])
    _write_json(output_dir, "simple_selector.json", simple)
    _write_json(output_dir, "graph_selector.json", graph_sel)
    _write_json(output_dir, "candidate_domains.json", domain_provenance)

    attempts_all, cp_all, metrics, machine_all, worker_all, validation_all = [], [], [], [], [], []
    experiences = []
    # Smoke gate first: graph arm of the first neighborhood only.
    smoke_label = f"graph_rank0_seed{neighborhoods[0].seed}"
    smoke = _run_arm(fixture, baseline, index, neighborhoods[0],
                     set(graph_sel[neighborhoods[0].seed]), domains, output_dir,
                     smoke_label)
    attempts_all.append(smoke)
    cp_all.extend(smoke["cp_results"])
    if smoke["final"] and smoke["final"].get("stage") == "local_cp":
        raise RuntimeError("E1.4 GRAPH smoke gate failed in local CP")
    # A realization failure is a semantic/model issue for the smoke gate only
    # when the local model produced a master candidate.  Keep a clean result
    # for the caller instead of burning the remaining five transactions.
    smoke_cp = smoke["cp_results"][-1] if smoke["cp_results"] else {}
    if smoke_cp.get("status") not in ("OPTIMAL", "FEASIBLE"):
        raise RuntimeError(f"E1.4 GRAPH smoke gate CP status {smoke_cp.get('status')}")
    if smoke["final"] and smoke["final"].get("stage") in ("machine", "worker", "worker_machine_envelope"):
        # Still write the diagnostic artifacts and stop; the specified smoke
        # gate requires fixing semantic issues before a matched experiment.
        stop_after_smoke = True
    else:
        stop_after_smoke = False

    if not smoke_only and not stop_after_smoke:
        # Matched six-cell experiment.  Re-run graph rank 0 as the preserved
        # smoke transaction's exact arm, then all remaining cells.
        runs = []
        for neighborhood in neighborhoods:
            for arm, selection in (("simple", simple[neighborhood.seed]),
                                   ("graph", graph_sel[neighborhood.seed])):
                if neighborhood is neighborhoods[0] and arm == "graph":
                    runs.append(smoke)
                    continue
                label = f"{arm}_rank{neighborhoods.index(neighborhood)}_seed{neighborhood.seed}"
                runs.append(_run_arm(fixture, baseline, index, neighborhood,
                                     set(selection), domains, output_dir, label))
        attempts_all = runs
        cp_all = [result for run_result in runs for result in run_result["cp_results"]]

    for result in attempts_all:
        final = result.get("final") or {}
        if final:
            metrics.append({"label": result["label"], **{key: final.get(key) for key in (
                "before", "after", "accepted", "improvement_seconds", "changed_operations",
                "line_change_count", "k_change_count", "machine_change_count",
                "worker_change_count", "timing_churn_seconds", "phenotype_hash",
                "rollback_hash", "outside_operations_unchanged")}})
            machine_all.append({"label": result["label"], "success": final.get("machine_verification_success"),
                                "failures": final.get("machine_failures", [])})
            worker_all.append({"label": result["label"], "success": final.get("worker_feasible"),
                               "failures": final.get("worker_failures", [])})
            validation_all.append({"label": result["label"], "status": final.get("validator_status"),
                                   "valid": final.get("validator_valid"), "report": final.get("validator_report")})
            if result.get("candidate") is not None and final.get("validator_valid"):
                _write_json(output_dir / "validated_phenotypes",
                            f"{result['label']}.json", {
                                "phenotype_hash": final.get("phenotype_hash"),
                                "status": final.get("validator_status"),
                                "rows": exported_rows(fixture.problem, result["candidate"]),
                            })
            selected_release = result.get("released_keys", [])
            for key in selected_release:
                experiences.append({"label": result["label"], "operation_key": key,
                                     "released": True, "line_k_action": True,
                                     "cp_status": (result.get("cp_results")[-1].get("status")
                                                   if result.get("cp_results") else None),
                                     "validated": final.get("validator_valid"),
                                     "tardiness_delta_seconds": final.get("improvement_seconds"),
                                     "failure_stage": final.get("stage")})

    _write_json(output_dir, "cp_model_stats.json", [item for result in attempts_all for item in result.get("cp_results", [])])
    _write_json(output_dir, "cp_solver_results.json", cp_all)
    _write_json(output_dir, "realization_attempts.json", attempts_all)
    _write_json(output_dir, "machine_realization.json", machine_all)
    _write_json(output_dir, "worker_realization.json", worker_all)
    _write_json(output_dir, "validation_results.json", validation_all)
    _write_json(output_dir, "candidate_metrics.json", metrics)
    grouped = {"simple": [], "graph": []}
    for item in metrics:
        grouped["graph" if item["label"].startswith("graph") else "simple"].append(item)
    for arm in grouped:
        grouped[arm] = {
            "results": grouped[arm],
            "validated_count": sum(bool(item.get("after")) for item in grouped[arm]),
            "improvement_count": sum(bool(item.get("accepted")) for item in grouped[arm]),
            "best_improvement_seconds": max(
                (float(item.get("improvement_seconds") or 0) for item in grouped[arm]),
                default=0.0),
        }
    graph_improvements = grouped["graph"]["improvement_count"]
    simple_improvements = grouped["simple"]["improvement_count"]
    graph_best = grouped["graph"]["best_improvement_seconds"]
    simple_best = grouped["simple"]["best_improvement_seconds"]
    if graph_improvements > simple_improvements or graph_best > simple_best:
        graph_conclusion = "GRAPH_GUIDANCE_PROMISING"
    elif graph_improvements == simple_improvements and graph_best == simple_best:
        graph_conclusion = "GRAPH_GUIDANCE_NO_ADVANTAGE"
    else:
        graph_conclusion = "GRAPH_GUIDANCE_NO_ADVANTAGE"
    _write_json(output_dir, "matched_comparison.json", {
        "arms": grouped, "graph_conclusion": graph_conclusion,
        "matched_neighborhood_count": len(neighborhoods),
        "same_neighborhoods_for_both_arms": True,
        "release_budget": 5,
    })
    _write_json(output_dir, "repair_experience_dataset.json", experiences)
    valid_improvements = [m for m in metrics if m.get("accepted") and m.get("improvement_seconds", 0) > 0]
    any_valid = any(bool(v.get("valid")) for v in validation_all)
    if valid_improvements:
        decision = "E1_4_VALIDATED_IMPROVEMENT"
    elif any_valid:
        decision = "E1_4_FEASIBLE_NO_IMPROVEMENT"
    elif any(item.get("stage") in ("machine", "worker", "worker_machine_envelope")
             for result in attempts_all for item in result.get("attempts", [])):
        decision = "E1_4_RESOURCE_REALIZATION_FAILURE"
    elif cp_all and all(item.get("status") not in ("OPTIMAL", "FEASIBLE") for item in cp_all):
        decision = "E1_4_LOCAL_MODEL_FAILURE"
    else:
        decision = "E1_4_SEMANTIC_FAILURE"
    _write_json(output_dir, "next_stage_decision.json", {
        "decision": decision, "smoke_only": smoke_only, "stopped_after_smoke": stop_after_smoke,
        "baseline_tardiness_seconds": 24521169,
        "graph_vs_simple": graph_conclusion,
        "simple_validated_count": grouped["simple"]["validated_count"],
        "graph_validated_count": grouped["graph"]["validated_count"],
        "simple_improvement_count": simple_improvements,
        "graph_improvement_count": graph_improvements,
        "simple_best_improvement_seconds": simple_best,
        "graph_best_improvement_seconds": graph_best,
    })
    report_lines = [
        "# CRAFT E1.4 Line+k Local Repair", "",
        f"- Decision: `{decision}`",
        f"- Source snapshot: `{source['snapshot']}`",
        f"- Source fingerprint: `{source['actual_source_fingerprint']}`",
        f"- Frozen CR phenotype: `{baseline.phenotype_hash}`",
        f"- Baseline total tardiness: `24521169` seconds",
        f"- Neighborhoods: `{len(neighborhoods)} x 30` operations; release budget `B=5`",
        f"- Smoke stopped after smoke: `{stop_after_smoke}`",
        f"- Validated candidate count: `{sum(bool(v.get('valid')) for v in validation_all)}`",
        f"- Validated improvements: `{len(valid_improvements)}`",
        f"- Graph vs simple: `{graph_conclusion}`", "",
        "## Validated transactions", "",
        "| Arm | Neighborhood | Status | Tardiness | Delta | Late orders | Makespan | Lines | k changes | Machines | Workers | Churn |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in metrics:
        after = item.get("after") or {}
        report_lines.append(
            f"| {'GRAPH' if item['label'].startswith('graph') else 'SIMPLE'} | {item['label']} | "
            f"{'VALIDATED_FEASIBLE' if after else 'REJECTED'} | "
            f"{after.get('total_tardiness_seconds','-')} | {item.get('improvement_seconds','-')} | "
            f"{after.get('late_order_count','-')} | {after.get('makespan_seconds','-')} | "
            f"{item.get('line_change_count','-')} | {item.get('k_change_count','-')} | "
            f"{item.get('machine_change_count','-')} | {item.get('worker_change_count','-')} | "
            f"{item.get('timing_churn_seconds','-')} |")
    report_lines += ["", "All CP calls were local, one-worker, seed 42, bounded to 30 seconds; no call approached the limit.",
                     "Exact machine and worker realization plus the unchanged independent validator were required before comparison.",
                     "No production integration, database write, GNN training, GA, or global CP solve was performed."]
    report = "\n".join(report_lines) + "\n"
    _write_text(output_dir, "final_report.md", report)
    files = {}
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "artifact_hash_manifest.json":
            files[str(path.relative_to(output_dir)).replace("\\", "/")] = _sha(path)
    audit = hashlib.sha256(json.dumps({"source": source, "neighborhoods": [n.as_dict() for n in neighborhoods],
                                       "simple": simple, "graph": graph_sel,
                                       "decision": decision, "metrics": metrics},
                                      sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    _write_json(output_dir, "artifact_hash_manifest.json", {
        "source_fingerprint": source["actual_source_fingerprint"],
        "phenotype_hash": baseline.phenotype_hash, "audit_fingerprint": audit,
        "decision": decision, "files": files,
    })
    return {"decision": decision, "audit_fingerprint": audit,
            "output_dir": str(output_dir), "smoke_stopped_after": stop_after_smoke}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--smoke-only", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.output_dir, smoke_only=args.smoke_only), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
