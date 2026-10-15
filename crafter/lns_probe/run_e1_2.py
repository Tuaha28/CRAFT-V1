"""Run CRAFT E1.2: sparse graph guidance versus simple machine release."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import time

import psutil

from crafter.e0_5_semantic_reconciliation import _safe
from crafter.phenotype import exported_rows, validate
from crafter.real_data_adapter import SNAPSHOT_NAME, build_real_window_fixture
from .analysis import build_incumbent_index
from .neighborhoods import guided_neighborhood
from .resource_graph import (
    build_resource_state_graph, causal_waiting, score_freedoms,
)
from .run_e1_1 import CR_PATH, EXPECTED_CR, EXPECTED_SOURCE, _max_tardiness
from .selective_model import (
    build_machine_candidate_pools, build_selective_machine_model,
    solve_selective,
)
from .selective_transaction import realize_selective_transaction
from .transaction import frozen_incumbent


OUTPUT_DIR = Path("outputs/crafter/lns_e1_2_graph_freedom")
E11_NEIGHBORHOODS = Path("outputs/crafter/lns_e1_1/selected_neighborhoods.json")
SEEDS = (42, 101, 202)
RELEASE_BUDGET = 5


def _write_json(directory, name, value):
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_safe(value), indent=2, sort_keys=True, default=str)+"\n"
    path = directory/name
    temporary = path.with_suffix(path.suffix+".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)
    return hashlib.sha256(payload.encode()).hexdigest()


def _write_text(directory, name, payload):
    directory.mkdir(parents=True, exist_ok=True)
    if not payload.endswith("\n"):
        payload += "\n"
    path = directory/name
    temporary = path.with_suffix(path.suffix+".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)
    return hashlib.sha256(payload.encode()).hexdigest()


def _phases(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    presolve = re.search(r"Starting presolve at ([0-9.]+)s", text)
    search = re.search(r"Starting search at ([0-9.]+)s", text)
    return {
        "presolve_start_seconds": float(presolve.group(1)) if presolve else None,
        "search_start_seconds": float(search.group(1)) if search else None,
    }


def _select(scores, details, count):
    legitimate = [key for key, row in details.items()
                  if row["alternative_machine_ids"]]
    legitimate.sort(key=lambda key: (-scores[key]["score"], key))
    return tuple(legitimate[:count])


def _max_tardy_metrics(problem, phenotype):
    return {
        "max_tardiness_seconds": _max_tardiness(problem, phenotype),
        **dict(phenotype.comparison_metrics),
    }


def _graph_schema():
    return {
        "schema_version": "craft_resource_state_graph_v1",
        "node_types": {
            "ORDER": ["factory", "release", "deadline", "completion",
                      "tardiness", "priority", "operation_count"],
            "OPERATION": ["route_position", "line", "k", "timing",
                          "productive_work", "setup", "machines", "workers",
                          "eligibility_summary", "skill", "tardiness_impact"],
            "MACHINE": ["factory", "line", "capability", "usability",
                        "realized_workload", "commitments"],
            "WORKER": ["factory", "skills", "realized_productive_work"],
        },
        "edge_types": ["ORDER_CONTAINS_OPERATION", "ROUTING_PRECEDENCE",
                       "REALIZED_MACHINE_SEQUENCE", "CURRENT_MACHINE_ASSIGNMENT",
                       "CURRENT_WORKER_ASSIGNMENT"],
        "contention_cliques": False,
        "alternative_eligibility_storage": "indexed lookup; not graph edges",
        "executed_freedom_classes": {
            "F0": "outside neighborhood frozen", "F1": "timing/sequence",
            "F2": "timing/sequence plus shortlisted exact machine identity"},
        "future_unexecuted_labels": ["F3_worker", "F4_machine_worker",
                                     "F5_machine_count", "F6_line"],
    }


def run(output_dir=OUTPUT_DIR):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    fixture = build_real_window_fixture(order_limit=50)
    payload = json.loads(CR_PATH.read_text(encoding="utf-8"))
    rows = payload["decoder_details"]["phenotype_operations"]
    source = {
        "snapshot": fixture.source_metadata["snapshot_name"],
        "expected_fingerprint": EXPECTED_SOURCE,
        "actual_fingerprint": fixture.source_fingerprint,
        "orders": len(fixture.problem.orders),
        "operations": len(fixture.problem.operations), "database_writes": 0,
    }
    if not (source["snapshot"] == SNAPSHOT_NAME and
            source["actual_fingerprint"] == EXPECTED_SOURCE and
            source["orders"] == 50 and source["operations"] == len(rows) == 431):
        raise RuntimeError("source identity mismatch")
    baseline = validate(fixture.problem,
        frozen_incumbent(fixture.problem, rows), fixture.operator_index)
    if not (payload["phenotype_hash"] == baseline.phenotype_hash == EXPECTED_CR
            and baseline.validation_report["valid"]):
        raise RuntimeError("CR phenotype identity mismatch")
    starting = {
        "expected_hash": EXPECTED_CR,
        "artifact_hash": payload["phenotype_hash"],
        "revalidated_hash": baseline.phenotype_hash,
        "status": baseline.status.value,
        "validator_valid": baseline.validation_report["valid"],
        "metrics": _max_tardy_metrics(fixture.problem, baseline),
    }
    index = build_incumbent_index(
        fixture.problem, rows, fixture.scheduler.op_details)
    neighborhoods = [guided_neighborhood(index, 30, seed=seed, rank=rank)
                     for rank, seed in enumerate(SEEDS)]
    if E11_NEIGHBORHOODS.exists():
        stored = json.loads(E11_NEIGHBORHOODS.read_text(encoding="utf-8"))
        if stored != [_safe(item.as_dict()) for item in neighborhoods]:
            raise RuntimeError("E1.1 neighborhood identity mismatch")

    graph = build_resource_state_graph(
        fixture.problem, index, fixture.scheduler.op_details,
        fixture.operator_index)
    causal = causal_waiting(fixture.problem, index, fixture.scheduler.op_details)
    graph["statistics"]["serialized_bytes"] = len(json.dumps(
        _safe(graph), sort_keys=True, default=str).encode("utf-8"))
    _write_json(output_dir, "source_identity.json", source)
    _write_json(output_dir, "starting_cr_identity.json", starting)
    _write_json(output_dir, "selected_neighborhoods.json",
                [item.as_dict() for item in neighborhoods])
    _write_json(output_dir, "graph_schema.json", _graph_schema())
    _write_json(output_dir, "graph_statistics.json", graph["statistics"])
    _write_json(output_dir, "resource_state_graph.json", graph)
    _write_json(output_dir, "causal_waiting_attribution.json", causal)

    simple_scores_all, graph_scores_all = {}, {}
    simple_freedoms, graph_freedoms, pools_artifact = {}, {}, {}
    preflight, cp_results, machine_results = [], [], []
    worker_results, validator_results, before_after = [], [], []
    experience, accepted, rejected = [], [], []
    process = psutil.Process(os.getpid())
    attempt_records = []
    for rank, neighborhood in enumerate(neighborhoods):
        neighborhood_id = f"rank{rank}_seed{neighborhood.seed}"
        pools, pool_details = build_machine_candidate_pools(
            fixture.problem, index, neighborhood, alternative_limit=5)
        pools_artifact[neighborhood_id] = pool_details
        simple_scores, graph_scores = score_freedoms(
            fixture.problem, index, causal, neighborhood, pools)
        actual_budget = min(RELEASE_BUDGET, sum(
            bool(row["alternative_machine_ids"])
            for row in pool_details.values()))
        simple_free = _select(simple_scores, pool_details, actual_budget)
        graph_free = _select(graph_scores, pool_details, len(simple_free))
        if len(simple_free) != len(graph_free):
            raise AssertionError("matched release budget violated")
        simple_scores_all[neighborhood_id] = simple_scores
        graph_scores_all[neighborhood_id] = graph_scores
        simple_freedoms[neighborhood_id] = {
            "budget": len(simple_free), "F2": list(simple_free),
            "F1": sorted(set(neighborhood.operation_keys)-set(simple_free))}
        graph_freedoms[neighborhood_id] = {
            "budget": len(graph_free), "F2": list(graph_free),
            "F1": sorted(set(neighborhood.operation_keys)-set(graph_free))}

        for arm, freed in (("simple", simple_free), ("graph", graph_free)):
            attempt_id = f"{neighborhood_id}_{arm}"
            built_at = time.perf_counter()
            try:
                artifacts = build_selective_machine_model(
                    fixture.problem, fixture.scheduler.op_details,
                    fixture.operator_index, index, neighborhood, freed, pools)
            except Exception as exc:
                record = {"attempt_id": attempt_id, "arm": arm,
                          "stage": "model_build", "reason": repr(exc)}
                rejected.append(record); attempt_records.append(record)
                continue
            pre = {"attempt_id": attempt_id, "arm": arm,
                   "neighborhood": neighborhood.as_dict(),
                   "released_operations": list(freed), **asdict(artifacts.size)}
            preflight.append(pre)
            if (artifacts.size.variables > 5000 or
                    artifacts.size.constraints > 15000 or
                    artifacts.size.build_time_seconds > 5.0):
                record = {"attempt_id": attempt_id, "arm": arm,
                    "stage": "preflight", "reason": "local model size gate",
                    "preflight": pre}
                rejected.append(record); attempt_records.append(record)
                continue
            log_path = output_dir/"logs"/f"{attempt_id}.log"
            solve, selected = solve_selective(
                artifacts, seed=neighborhood.seed, seconds_limit=5.0,
                log_path=log_path)
            solve.update(_phases(log_path))
            solve.update({"attempt_id": attempt_id, "arm": arm,
                          "peak_rss_mb": process.memory_info().rss/1024**2,
                          "elapsed_build_and_solve_seconds":
                              time.perf_counter()-built_at})
            cp_results.append(solve)
            if selected is None:
                record = {"attempt_id": attempt_id, "arm": arm,
                          "stage": "local_cp", "reason": solve["status"]}
                rejected.append(record); attempt_records.append(record)
                continue
            transaction, candidate = realize_selective_transaction(
                fixture.problem, fixture.operator_index, index,
                neighborhood, selected, baseline)
            transaction.update({"attempt_id": attempt_id, "arm": arm,
                                "released_operations": list(freed)})
            attempt_records.append(transaction)
            machine_results.append({
                "attempt_id": attempt_id, "arm": arm,
                "success": transaction.get("machine_verification_success"),
                "machine_changes": transaction.get("machine_change_count"),
                "witnesses": transaction.get("witnesses", [])})
            worker_results.append({
                "attempt_id": attempt_id, "arm": arm,
                "success": transaction.get("worker_feasible"),
                "worker_ids_unchanged": transaction.get("worker_ids_unchanged"),
                "failure_stage": None if transaction.get("worker_feasible") else
                    transaction.get("stage")})
            validator_results.append({
                "attempt_id": attempt_id, "arm": arm,
                "status": transaction.get("validator_status"),
                "valid": transaction.get("validator_valid"),
                "report": transaction.get("validator_report")})
            before_after.append({
                "attempt_id": attempt_id, "arm": arm,
                "before": transaction.get("before"),
                "after": transaction.get("after"),
                "improvement_seconds": transaction.get("improvement_seconds"),
                "changed_operations": transaction.get("changed_operation_count"),
                "machine_changes": transaction.get("machine_change_count"),
                "timing_churn_seconds": transaction.get("timing_churn_seconds"),
                "phenotype_hash": transaction.get("phenotype_hash")})
            if transaction.get("accepted"):
                accepted.append(transaction)
                _write_json(output_dir/"accepted_phenotypes", f"{attempt_id}.json", {
                    "phenotype_hash": candidate.phenotype_hash,
                    "metrics": _max_tardy_metrics(fixture.problem, candidate),
                    "operations": exported_rows(fixture.problem, candidate),
                    "validator": candidate.validation_report})
            else:
                rejected.append(transaction)
            for key in neighborhood.operation_keys:
                score = (graph_scores if arm == "graph" else simple_scores)[key]
                experience.append({
                    "attempt_id": attempt_id, "arm": arm,
                    "neighborhood_rank": rank, "seed": neighborhood.seed,
                    "operation_key": key, "factory_id": neighborhood.factory_id,
                    "features": score,
                    "assigned_freedom": "F2" if key in freed else "F1",
                    "machine_candidate_pool": list(pools[key]),
                    "cp_status": solve["status"],
                    "machine_verification_success": transaction.get(
                        "machine_verification_success"),
                    "worker_success": transaction.get("worker_feasible"),
                    "validator_success": transaction.get("validator_valid"),
                    "tardiness_delta_seconds": -transaction.get(
                        "improvement_seconds", 0),
                    "machine_change_count": transaction.get(
                        "machine_change_count"),
                    "timing_churn_seconds": transaction.get(
                        "timing_churn_seconds"),
                })

    _write_json(output_dir, "delivery_impact_scores.json", graph_scores_all)
    _write_json(output_dir, "simple_machine_release_scores.json", simple_scores_all)
    _write_json(output_dir, "graph_freedom_scores.json", graph_scores_all)
    _write_json(output_dir, "selected_freedoms_simple.json", simple_freedoms)
    _write_json(output_dir, "selected_freedoms_graph.json", graph_freedoms)
    _write_json(output_dir, "machine_candidate_pools.json", pools_artifact)
    _write_json(output_dir, "local_model_preflight.json", preflight)
    _write_json(output_dir, "cp_results.json", cp_results)
    _write_json(output_dir, "machine_verification_results.json", machine_results)
    _write_json(output_dir, "worker_results.json", worker_results)
    _write_json(output_dir, "validator_results.json", validator_results)
    _write_json(output_dir, "before_after_metrics.json", before_after)

    valid_by_arm = {arm: [row for row in attempt_records
        if row.get("arm") == arm and row.get("validator_valid")]
        for arm in ("simple", "graph")}
    accepted_by_arm = {arm: [row for row in accepted if row["arm"] == arm]
                       for arm in ("simple", "graph")}
    improvement_by_arm = {arm: sum(max(0, row.get("improvement_seconds", 0))
        for row in valid_by_arm[arm]) for arm in ("simple", "graph")}
    total_released = sum(row["budget"] for row in simple_freedoms.values())
    if total_released == 0:
        # On this frozen window every mutable operation already occupies all
        # usable machines matching its fixed line/capability.  With line and k
        # fixed, physical-ID freedom is therefore structurally empty and the
        # selector comparison cannot answer the graph hypothesis.
        comparison_signal = "INCONCLUSIVE"
        graph_earned = "INCONCLUSIVE"
    elif not valid_by_arm["simple"] and not valid_by_arm["graph"]:
        comparison_signal = "INCONCLUSIVE"
        graph_earned = "INCONCLUSIVE"
    elif improvement_by_arm["graph"] > improvement_by_arm["simple"]:
        comparison_signal = "GRAPH_WINS"; graph_earned = "YES"
    elif improvement_by_arm["simple"] > improvement_by_arm["graph"]:
        comparison_signal = "SIMPLE_WINS"; graph_earned = "NO"
    else:
        comparison_signal = "NO_MEASURABLE_DIFFERENCE"; graph_earned = "NO"
    if total_released == 0:
        next_stage = "PROCEED_TO_DETERMINISTIC_MULTI_RESOURCE_FREEDOM"
    elif (sum(row.get("success") is True for row in machine_results)
            < len(machine_results)):
        next_stage = "FIX_LOCAL_RESOURCE_MODEL_FIRST"
    elif comparison_signal in ("SIMPLE_WINS", "NO_MEASURABLE_DIFFERENCE"):
        next_stage = "DROP_GRAPH_USE_SIMPLE_SELECTIVE_LNS"
    elif comparison_signal == "GRAPH_WINS" and accepted_by_arm["graph"]:
        next_stage = "PROCEED_TO_DETERMINISTIC_MULTI_RESOURCE_FREEDOM"
    else:
        next_stage = "COLLECT_MORE_FREEDOM_EXPERIENCE_BEFORE_GNN"
    matched = {
        "signal": comparison_signal, "graph_earned_its_place": graph_earned,
        "same_neighborhoods": True, "equal_release_budget": True,
        "requested_release_budget_per_neighborhood": RELEASE_BUDGET,
        "executed_release_budget_total_per_arm": total_released,
        "structural_limitation": (
            "No operation in the three stored neighborhoods had an additional "
            "usable same-factory/same-line/capability machine after preserving "
            "fixed k; each incumbent already used the complete usable pool."
            if total_released == 0 else None),
        "attempts_per_arm": {arm: sum(row.get("arm") == arm for row in cp_results)
                             for arm in ("simple", "graph")},
        "validator_feasible_per_arm": {arm: len(valid_by_arm[arm])
                                        for arm in valid_by_arm},
        "accepted_per_arm": {arm: len(accepted_by_arm[arm])
                             for arm in accepted_by_arm},
        "validated_improvement_seconds_sum": improvement_by_arm,
        "next_stage": next_stage,
    }
    _write_json(output_dir, "matched_comparison.json", matched)
    _write_json(output_dir, "accepted_candidates.json", accepted)
    _write_json(output_dir, "rejected_candidates.json", rejected)
    lines = [json.dumps(_safe(row), sort_keys=True, default=str)
             for row in experience]
    _write_text(output_dir, "freedom_experience_dataset.jsonl", "\n".join(lines))

    report = (
        "# CRAFT E1.2 resource-state graph and selective machine freedom\n\n"
        f"- Source: `{fixture.source_fingerprint}`\n"
        f"- Starting phenotype: `{baseline.phenotype_hash}`\n"
        f"- Graph nodes: {sum(graph['statistics']['nodes_by_type'].values())}\n"
        f"- Graph edges: {sum(graph['statistics']['edges_by_type'].values())}\n"
        f"- Attempts solved: {len(cp_results)}/6\n"
        f"- Machine verification: {sum(row['success'] is True for row in machine_results)}/{len(machine_results)}\n"
        f"- Anchored workers feasible: {sum(row['success'] is True for row in worker_results)}/{len(worker_results)}\n"
        f"- Validator clean: {sum(row['valid'] is True for row in validator_results)}/{len(validator_results)}\n"
        f"- Accepted improvements: {len(accepted)}\n"
        f"- Graph vs simple: `{comparison_signal}`\n"
        f"- Graph earned its place: `{graph_earned}`\n"
        f"- Recommended next stage: `{next_stage}`\n")
    _write_text(output_dir, "final_report.md", report)
    files = {}
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "artifact_hash_manifest.json":
            files[str(path.relative_to(output_dir)).replace("\\", "/")] = (
                hashlib.sha256(path.read_bytes()).hexdigest())
    _write_json(output_dir, "artifact_hash_manifest.json", {
        "source_fingerprint": fixture.source_fingerprint,
        "starting_hash": baseline.phenotype_hash,
        "comparison_signal": comparison_signal,
        "graph_earned_its_place": graph_earned,
        "next_stage": next_stage, "files": files,
    })
    return {"signal": comparison_signal, "graph_earned": graph_earned,
            "next_stage": next_stage, "accepted": len(accepted),
            "output_dir": str(output_dir)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.output_dir), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
