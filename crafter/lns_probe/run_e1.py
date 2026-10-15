"""Run the isolated CRAFT-LNS E1 bounded-neighborhood experiment."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

import psutil

from crafter.e0_5_semantic_reconciliation import _safe
from crafter.phenotype import exported_rows, validate
from crafter.real_data_adapter import SNAPSHOT_NAME, build_real_window_fixture

from .analysis import build_incumbent_index, causal_attribution
from .local_model import build_tier1_local_model, solve_local
from .neighborhoods import (
    guided_neighborhood, random_neighborhood, transactional_footprint,
)
from .transaction import frozen_incumbent, realize_transaction


EXPECTED_SOURCE = "7386844498503f37cdfa08877248c51966699e31b6762f6e0c3effc572cd78ee"
EXPECTED_CR_HASH = "56cc2b12f69f242a380f6678385c06a70341a560ebac3619408d5700b7938e92"
CR_PATH = Path("outputs/crafter/benchmark_50_v1/baseline_criticalratio_result.json")
OUTPUT_DIR = Path("outputs/crafter/lns_e1")


def _write_json(directory, name, value):
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_safe(value), indent=2, sort_keys=True, default=str)+"\n"
    path = directory/name
    temporary = path.with_suffix(path.suffix+".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return hashlib.sha256(payload.encode()).hexdigest()


def _write_text(directory, name, text):
    directory.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    path = directory/name
    temporary = path.with_suffix(path.suffix+".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return hashlib.sha256(text.encode()).hexdigest()


def _max_tardiness(problem, phenotype):
    final = {}
    for item in phenotype.operations:
        op = problem.operation_by_key[item.assignment.operation_key]
        old = final.get(item.assignment.order_id)
        if old is None or op.sequence > old[0]:
            final[item.assignment.order_id] = (op.sequence,
                                                item.assignment.end_seconds)
    values = []
    for order_id, (_sequence, end) in final.items():
        due = problem.order_by_id[order_id].deadline_seconds
        if due is not None:
            values.append(max(0, end-max(0, due)))
    return max(values, default=0)


def _presolve_observations(path):
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    presolve_start = re.search(r"Starting presolve at ([0-9.]+)s", text)
    search_start = re.search(r"Starting search at ([0-9.]+)s", text)
    presolve_value = float(presolve_start.group(1)) if presolve_start else None
    search_value = float(search_start.group(1)) if search_start else None
    return {
        "presolve_log_seen": "Presolve summary" in text,
        "search_log_seen": "Starting search" in text,
        "presolve_start_seconds": presolve_value,
        "search_start_seconds": search_value,
        "presolve_elapsed_proxy_seconds": (
            search_value-presolve_value
            if search_value is not None and presolve_value is not None else None),
    }


def _attempt(fixture, index, baseline, neighborhood, output_dir, label):
    process = psutil.Process(os.getpid())
    rss_before = process.memory_info().rss/1024**2
    footprint = transactional_footprint(fixture.problem, index, neighborhood)
    attempt = {
        "attempt_id": label, "neighborhood": neighborhood.as_dict(),
        "footprint": footprint, "tier": 1,
    }
    if footprint["closure_status"] != "ACCEPTED":
        attempt.update({"status": footprint["closure_status"],
                        "accepted": False})
        return attempt, None
    try:
        artifacts = build_tier1_local_model(
            fixture.problem, fixture.scheduler.op_details, index, neighborhood)
    except Exception as exc:
        attempt.update({"status": "MODEL_BUILD_FAILED", "accepted": False,
                        "error": repr(exc)})
        return attempt, None
    attempt["preflight"] = asdict(artifacts.size)
    unsafe = (
        artifacts.size.variables > 7500 or
        artifacts.size.constraints > 20000 or
        artifacts.size.optional_intervals >= 10829 or
        artifacts.size.build_time_seconds > 5.0)
    if unsafe:
        attempt.update({"status": "LOCAL_PREFLIGHT_REJECTED",
                        "accepted": False})
        return attempt, None
    log_path = output_dir/"logs"/f"{label}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    solve, assignment = solve_local(
        artifacts, seed=neighborhood.seed, time_limit=10.0,
        log_path=log_path)
    solve.update(_presolve_observations(log_path))
    solve["peak_rss_observed_mb"] = process.memory_info().rss/1024**2
    solve["rss_delta_observed_mb"] = max(
        0.0, solve["peak_rss_observed_mb"]-rss_before)
    attempt["local_cp"] = solve
    if assignment is None:
        attempt.update({"status": "LOCAL_CP_NO_INCUMBENT", "accepted": False})
        return attempt, None
    transaction, candidate = realize_transaction(
        fixture.problem, fixture.operator_index, fixture.scheduler.op_details,
        index, neighborhood, assignment, baseline)
    attempt["transaction"] = transaction
    attempt["accepted"] = bool(transaction.get("accepted"))
    attempt["status"] = (
        "COMMITTED_EXPERIMENTAL" if attempt["accepted"] else
        f"ROLLBACK_{str(transaction.get('stage', 'unknown')).upper()}")
    before = dict(baseline.comparison_metrics)
    after = transaction.get("after") or before
    improvement = before["total_tardiness_seconds"]-after[
        "total_tardiness_seconds"]
    attempt["efficiency"] = {
        "tardiness_improvement_seconds": improvement,
        "improvement_per_solver_second": (
            improvement/max(solve["wall_time_seconds"], 1e-9)),
    }
    if candidate is not None:
        attempt["schedule_quality"] = {
            "before": {**before, "max_tardiness_seconds":
                       _max_tardiness(fixture.problem, baseline)},
            "after": {**after, "max_tardiness_seconds":
                      _max_tardiness(fixture.problem, candidate)},
        }
        if attempt["accepted"]:
            _write_json(output_dir/"accepted_phenotypes", f"{label}.json", {
                "status": candidate.status.value,
                "phenotype_hash": candidate.phenotype_hash,
                "comparison_metrics": dict(candidate.comparison_metrics),
                "operations": exported_rows(fixture.problem, candidate),
                "validator_report": candidate.validation_report,
            })
    return attempt, candidate


def _aggregate(results):
    cp_with_incumbent = [row for row in results
                         if row.get("local_cp", {}).get("solution_count", 0) > 0]
    materialized = [row for row in results
                    if row.get("transaction", {}).get("machine", {}).get("success")]
    worker = [row for row in results
              if row.get("transaction", {}).get("worker", {}).get("success")]
    validated = [row for row in results
                 if row.get("transaction", {}).get("validator_valid")]
    accepted = [row for row in results if row.get("accepted")]
    return {
        "attempts": len(results), "cp_incumbents": len(cp_with_incumbent),
        "cp_solve_success_rate": len(cp_with_incumbent)/len(results) if results else 0,
        "machine_materialization_successes": len(materialized),
        "machine_materialization_rate": len(materialized)/len(cp_with_incumbent)
        if cp_with_incumbent else 0,
        "worker_allocation_successes": len(worker),
        "worker_allocation_rate": len(worker)/len(materialized) if materialized else 0,
        "validator_passes": len(validated),
        "validator_pass_rate": len(validated)/len(worker) if worker else 0,
        "accepted": len(accepted),
        "accepted_rate": len(accepted)/len(results) if results else 0,
        "total_tardiness_improvement_seconds": sum(
            row.get("efficiency", {}).get("tardiness_improvement_seconds", 0)
            for row in accepted),
    }


def run(output_dir=OUTPUT_DIR):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    fixture = build_real_window_fixture(order_limit=50)
    cr_payload = json.loads(CR_PATH.read_text(encoding="utf-8"))
    rows = cr_payload["decoder_details"]["phenotype_operations"]
    source_identity = {
        "snapshot": fixture.source_metadata["snapshot_name"],
        "expected_source_fingerprint": EXPECTED_SOURCE,
        "actual_source_fingerprint": fixture.source_fingerprint,
        "orders": len(fixture.problem.orders),
        "operations": len(fixture.problem.operations),
        "selected_order_ids": list(fixture.selected_order_ids),
        "database_writes": 0,
    }
    if not (source_identity["snapshot"] == SNAPSHOT_NAME and
            fixture.source_fingerprint == EXPECTED_SOURCE and
            len(fixture.problem.orders) == 50 and
            len(fixture.problem.operations) == len(rows) == 431):
        raise RuntimeError("frozen source identity mismatch")
    baseline = validate(
        fixture.problem, frozen_incumbent(fixture.problem, rows),
        fixture.operator_index)
    starting_identity = {
        "artifact": str(CR_PATH), "status": cr_payload["status"],
        "expected_phenotype_hash": EXPECTED_CR_HASH,
        "artifact_phenotype_hash": cr_payload["phenotype_hash"],
        "revalidated_phenotype_hash": baseline.phenotype_hash,
        "validator_valid": baseline.validation_report["valid"],
        "comparison_metrics": dict(baseline.comparison_metrics),
    }
    if not (starting_identity["artifact_phenotype_hash"] == EXPECTED_CR_HASH ==
            starting_identity["revalidated_phenotype_hash"] and
            baseline.validation_report["valid"]):
        raise RuntimeError("frozen CR identity/validator mismatch")

    index = build_incumbent_index(
        fixture.problem, rows, fixture.scheduler.op_details)
    attribution = causal_attribution(fixture.problem, index)
    analysis = {
        "routing_arcs": len(index.route_successor),
        "realized_machine_sequences": len(index.machine_sequences),
        "realized_machine_arcs": sum(len(value) for value in
                                     index.machine_successor.values()),
        "indexed_operations": len(index.by_key),
        "late_orders": len(attribution["orders"]),
        "potential_contention_cliques_created": 0,
    }
    _write_json(output_dir, "source_identity.json", source_identity)
    _write_json(output_dir, "starting_cr_identity.json", starting_identity)
    _write_json(output_dir, "incumbent_analysis.json", analysis)
    _write_json(output_dir, "causal_attribution_report.json", attribution)

    guided, randoms, footprints, e1a = [], [], [], []
    accepted, rejected, validator_rows = [], [], []
    for size in (30, 50, 70):
        g = guided_neighborhood(index, size, seed=42, rank=0)
        r = random_neighborhood(
            index, size, factory_id=g.factory_id, seed=4200+size,
            preferred_window=(g.start_seconds, g.end_seconds))
        guided.append(g.as_dict())
        randoms.append(r.as_dict())
        for neighborhood in (g, r):
            label = f"e1a_{neighborhood.strategy}_{size}"
            result, candidate = _attempt(
                fixture, index, baseline, neighborhood, output_dir, label)
            e1a.append(result)
            footprints.append(result["footprint"])
            (accepted if result.get("accepted") else rejected).append(result)
            if result.get("transaction"):
                validator_rows.append({
                    "attempt_id": label,
                    "validator_valid": result["transaction"].get("validator_valid"),
                    "stage": result["transaction"].get("stage"),
                    "accepted": result.get("accepted"),
                })
    _write_json(output_dir, "guided_neighborhoods.json", guided)
    _write_json(output_dir, "random_neighborhoods.json", randoms)
    _write_json(output_dir, "neighborhood_footprints.json", footprints)
    _write_json(output_dir, "local_model_preflight.json", [
        {"attempt_id": row["attempt_id"], "preflight": row.get("preflight"),
         "status": row["status"]} for row in e1a])
    _write_json(output_dir, "e1_a_scalability_results.json", {
        "results": e1a, "aggregate": _aggregate(e1a)})

    viable = [row for row in e1a if row.get("local_cp", {}).get(
        "solution_count", 0) > 0]
    e1b = []
    if viable:
        viable_sizes = sorted({row["neighborhood"]["target_size"] for row in viable})
        size_stats = {}
        for size in viable_sizes:
            rows_at_size = [row for row in viable
                            if row["neighborhood"]["target_size"] == size]
            size_stats[size] = sum(row["local_cp"]["wall_time_seconds"]
                                   for row in rows_at_size)/len(rows_at_size)
        best_size = min(viable_sizes, key=lambda size: (size_stats[size], size))
        for sample, seed in enumerate((101, 202, 303), start=1):
            g = guided_neighborhood(index, best_size, seed=seed, rank=sample)
            r = random_neighborhood(
                index, best_size, factory_id=g.factory_id, seed=seed,
                preferred_window=(g.start_seconds, g.end_seconds))
            for neighborhood in (g, r):
                label = f"e1b_{neighborhood.strategy}_{best_size}_s{seed}"
                result, candidate = _attempt(
                    fixture, index, baseline, neighborhood, output_dir, label)
                e1b.append(result)
                (accepted if result.get("accepted") else rejected).append(result)
                if result.get("transaction"):
                    validator_rows.append({
                        "attempt_id": label,
                        "validator_valid": result["transaction"].get(
                            "validator_valid"),
                        "stage": result["transaction"].get("stage"),
                        "accepted": result.get("accepted"),
                    })
        _write_json(output_dir, "e1_b_selection_results.json", {
            "selected_size": best_size, "selection_basis": size_stats,
            "results": e1b, "aggregate": _aggregate(e1b),
            "guided": _aggregate([row for row in e1b
                                  if row["neighborhood"]["strategy"] == "guided"]),
            "random": _aggregate([row for row in e1b
                                  if row["neighborhood"]["strategy"] == "random"]),
        })
    _write_json(output_dir, "accepted_transactions.json", accepted)
    _write_json(output_dir, "rejected_transactions.json", rejected)
    _write_json(output_dir, "validator_results.json", validator_rows)
    all_results = e1a+e1b
    _write_json(output_dir, "before_after_schedule_metrics.json", {
        "baseline": {**dict(baseline.comparison_metrics),
                     "max_tardiness_seconds": _max_tardiness(
                         fixture.problem, baseline)},
        "attempts": [
            {"attempt_id": row["attempt_id"],
             "accepted": row.get("accepted"),
             "quality": row.get("schedule_quality"),
             "efficiency": row.get("efficiency")}
            for row in all_results],
    })

    e1a_summary = _aggregate(e1a)
    e1b_summary = _aggregate(e1b)
    any_improved = bool(accepted)
    guidance = "INCONCLUSIVE"
    if e1b:
        g = _aggregate([row for row in e1b
                        if row["neighborhood"]["strategy"] == "guided"])
        r = _aggregate([row for row in e1b
                        if row["neighborhood"]["strategy"] == "random"])
        if g["total_tardiness_improvement_seconds"] > r[
                "total_tardiness_improvement_seconds"]:
            guidance = "YES"
        elif g["total_tardiness_improvement_seconds"] < r[
                "total_tardiness_improvement_seconds"]:
            guidance = "NO"
    if not viable:
        recommendation = "MODIFY_LOCAL_CP_FORMULATION"
    elif any_improved and guidance == "YES":
        recommendation = "PROCEED_WITH_GUIDED_LNS_LOCAL_CP"
    elif any_improved:
        recommendation = "PROCEED_WITH_SIMPLE_LNS_LOCAL_CP_NO_GRAPH"
    else:
        recommendation = "MODIFY_LOCAL_CP_FORMULATION"
    report = (
        "# CRAFT-LNS E1 report\n\n"
        f"- Source fingerprint: `{fixture.source_fingerprint}`\n"
        f"- CR phenotype hash: `{baseline.phenotype_hash}`\n"
        f"- E1-A: {json.dumps(e1a_summary, sort_keys=True)}\n"
        f"- E1-B: {json.dumps(e1b_summary, sort_keys=True)}\n"
        f"- Guided signal: **{guidance}**\n"
        f"- Recommendation: **{recommendation}**\n\n"
        "All accepted transactions passed exact machine realization, exact worker "
        "allocation, and the unchanged independent validator. Rejected attempts "
        "were rolled back to the byte-identical frozen CR phenotype.\n")
    _write_text(output_dir, "final_report.md", report)
    files = {}
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "artifact_hash_manifest.json":
            files[str(path.relative_to(output_dir)).replace("\\", "/")] = (
                hashlib.sha256(path.read_bytes()).hexdigest())
    _write_json(output_dir, "artifact_hash_manifest.json", {
        "source_fingerprint": fixture.source_fingerprint,
        "starting_phenotype_hash": baseline.phenotype_hash,
        "recommendation": recommendation, "guided_signal": guidance,
        "files": files,
    })
    return {"recommendation": recommendation, "guided_signal": guidance,
            "e1_a": e1a_summary, "e1_b": e1b_summary,
            "output_dir": str(output_dir)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.output_dir), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
