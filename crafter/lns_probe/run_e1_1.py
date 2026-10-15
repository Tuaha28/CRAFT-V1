"""Run the isolated E1.1 resource-anchored end-to-end repair gate."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re

import psutil

from crafter.e0_5_semantic_reconciliation import _safe
from crafter.phenotype import exported_rows, validate
from crafter.real_data_adapter import SNAPSHOT_NAME, build_real_window_fixture
from .analysis import build_incumbent_index, seconds
from .anchored_model import build_resource_anchored_model, solve_anchored
from .anchored_transaction import realize_anchored_transaction
from .neighborhoods import guided_neighborhood
from .transaction import frozen_incumbent


EXPECTED_SOURCE = "7386844498503f37cdfa08877248c51966699e31b6762f6e0c3effc572cd78ee"
EXPECTED_CR = "56cc2b12f69f242a380f6678385c06a70341a560ebac3619408d5700b7938e92"
CR_PATH = Path("outputs/crafter/benchmark_50_v1/baseline_criticalratio_result.json")
OUTPUT_DIR = Path("outputs/crafter/lns_e1_1")


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


def _max_tardiness(problem, phenotype):
    completion = {}
    for item in phenotype.operations:
        op = problem.operation_by_key[item.assignment.operation_key]
        old = completion.get(item.assignment.order_id)
        if old is None or op.sequence > old[0]:
            completion[item.assignment.order_id] = (
                op.sequence, item.assignment.end_seconds)
    return max((max(0, end-max(0, problem.order_by_id[order].deadline_seconds))
                for order, (_seq, end) in completion.items()
                if problem.order_by_id[order].deadline_seconds is not None),
               default=0)


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
        "operations": len(fixture.problem.operations),
        "database_writes": 0,
    }
    if not (source["snapshot"] == SNAPSHOT_NAME and
            source["actual_fingerprint"] == EXPECTED_SOURCE and
            source["orders"] == 50 and source["operations"] == len(rows) == 431):
        raise RuntimeError("source identity mismatch")
    baseline = validate(
        fixture.problem, frozen_incumbent(fixture.problem, rows),
        fixture.operator_index)
    starting = {
        "expected_hash": EXPECTED_CR,
        "artifact_hash": payload["phenotype_hash"],
        "revalidated_hash": baseline.phenotype_hash,
        "status": baseline.status.value,
        "validator_valid": baseline.validation_report["valid"],
        "metrics": dict(baseline.comparison_metrics),
    }
    if not (payload["phenotype_hash"] == baseline.phenotype_hash == EXPECTED_CR
            and baseline.validation_report["valid"]):
        raise RuntimeError("CR identity mismatch")
    index = build_incumbent_index(
        fixture.problem, rows, fixture.scheduler.op_details)
    neighborhoods = [
        guided_neighborhood(index, 30, seed=seed, rank=rank)
        for rank, seed in enumerate((42, 101, 202))
    ]
    _write_json(output_dir, "source_identity.json", source)
    _write_json(output_dir, "starting_cr_identity.json", starting)
    _write_json(output_dir, "selected_neighborhoods.json",
                [item.as_dict() for item in neighborhoods])

    preflight, cp_results = [], []
    machine_results, worker_results, validator_results = [], [], []
    accepted, rejected, failures, metrics = [], [], [], []
    machine_boundaries, worker_boundaries = {}, {}
    process = psutil.Process(os.getpid())
    for attempt_no, neighborhood in enumerate(neighborhoods, start=1):
        label = f"guided_30_rank{attempt_no-1}_seed{neighborhood.seed}"
        try:
            artifacts = build_resource_anchored_model(
                fixture.problem, fixture.scheduler.op_details,
                fixture.operator_index, index, neighborhood)
        except Exception as exc:
            failure = {"attempt_id": label, "stage": "model_build",
                       "reason": repr(exc)}
            failures.append(failure); rejected.append(failure)
            continue
        pre = {"attempt_id": label, **asdict(artifacts.size)}
        preflight.append(pre)
        machine_boundaries[label] = artifacts.machine_boundaries
        worker_boundaries[label] = artifacts.worker_boundaries
        if (artifacts.size.variables > 5000 or
                artifacts.size.constraints > 15000 or
                artifacts.size.build_time_seconds > 5.0):
            failure = {"attempt_id": label, "stage": "preflight",
                       "reason": "resource-anchored model unexpectedly large",
                       "preflight": pre}
            failures.append(failure); rejected.append(failure)
            continue
        log_path = output_dir/"logs"/f"{label}.log"
        solve, selected = solve_anchored(
            artifacts, seed=neighborhood.seed, seconds_limit=5.0,
            log_path=log_path)
        solve.update(_phases(log_path))
        solve["peak_rss_mb"] = process.memory_info().rss/1024**2
        solve["attempt_id"] = label
        cp_results.append(solve)
        if selected is None:
            failure = {"attempt_id": label, "stage": "local_cp",
                       "reason": solve["status"]}
            failures.append(failure); rejected.append(failure)
            continue
        transaction, candidate = realize_anchored_transaction(
            fixture.problem, fixture.operator_index, neighborhood,
            selected, baseline)
        transaction["attempt_id"] = label
        machine_results.append({
            "attempt_id": label,
            "status": ("MACHINE_ANCHORED_FEASIBLE" if
                       transaction.get("machine_anchored_feasible") else
                       "MACHINE_ANCHORED_INFEASIBLE"),
            "machine_ids_unchanged": transaction.get("machine_ids_unchanged"),
            "failure": transaction.get("changeover_violations"),
        })
        worker_results.append({
            "attempt_id": label,
            "status": ("WORKER_ANCHORED_FEASIBLE" if
                       transaction.get("worker_anchored_feasible") else
                       "WORKER_ANCHORED_INFEASIBLE"),
            "worker_ids_unchanged": transaction.get("worker_ids_unchanged"),
        })
        validator_results.append({
            "attempt_id": label,
            "status": transaction.get("validator_status"),
            "valid": transaction.get("validator_valid"),
            "report": transaction.get("validator_report"),
        })
        metrics.append({
            "attempt_id": label, "before": transaction.get("before"),
            "after": transaction.get("after"),
            "improvement_seconds": transaction.get("improvement_seconds"),
            "changed_operations": transaction.get("changed_operation_count"),
            "timing_churn_seconds": transaction.get("timing_churn_seconds"),
            "max_tardiness_before": _max_tardiness(fixture.problem, baseline),
            "max_tardiness_after": (
                _max_tardiness(fixture.problem, candidate)
                if candidate is not None else None),
        })
        if transaction.get("accepted"):
            accepted.append(transaction)
            _write_json(output_dir/"accepted_phenotypes", f"{label}.json", {
                "phenotype_hash": candidate.phenotype_hash,
                "metrics": dict(candidate.comparison_metrics),
                "operations": exported_rows(fixture.problem, candidate),
                "validator": candidate.validation_report,
            })
        else:
            rejected.append(transaction)
            if not transaction.get("validator_valid"):
                failures.append(transaction)

    any_valid = any(row.get("valid") for row in validator_results)
    if accepted:
        status = "E1_1_END_TO_END_REPAIR_PASS"
        e12 = "D"
    elif any_valid:
        status = "E1_1_FEASIBLE_BUT_NO_IMPROVEMENT"
        e12 = "A"
    else:
        status = "E1_1_RESOURCE_ANCHORED_REPAIR_FAIL"
        e12 = "D"
    _write_json(output_dir, "anchored_machine_boundaries.json", machine_boundaries)
    _write_json(output_dir, "anchored_worker_boundaries.json", worker_boundaries)
    _write_json(output_dir, "local_model_preflight.json", preflight)
    _write_json(output_dir, "local_cp_results.json", cp_results)
    _write_json(output_dir, "machine_anchor_results.json", machine_results)
    _write_json(output_dir, "worker_anchor_results.json", worker_results)
    _write_json(output_dir, "full_validator_results.json", validator_results)
    _write_json(output_dir, "before_after_metrics.json", metrics)
    _write_json(output_dir, "accepted_candidates.json", accepted)
    _write_json(output_dir, "rejected_candidates.json", rejected)
    _write_json(output_dir, "failure_witnesses.json", failures)
    report = (
        "# E1.1 resource-anchored repair gate\n\n"
        f"- Status: `{status}`\n"
        f"- Attempts: {len(neighborhoods)}\n"
        f"- CP incumbents: {sum(row['solution_count'] > 0 for row in cp_results)}\n"
        f"- Machine-anchored feasible: {sum(row['status'] == 'MACHINE_ANCHORED_FEASIBLE' for row in machine_results)}\n"
        f"- Worker-anchored feasible: {sum(row['status'] == 'WORKER_ANCHORED_FEASIBLE' for row in worker_results)}\n"
        f"- Validator clean: {sum(bool(row['valid']) for row in validator_results)}\n"
        f"- Accepted improvements: {len(accepted)}\n"
        f"- E1.2 choice: {e12}\n")
    _write_text(output_dir, "final_report.md", report)
    files = {}
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "artifact_hash_manifest.json":
            files[str(path.relative_to(output_dir)).replace("\\", "/")] = (
                hashlib.sha256(path.read_bytes()).hexdigest())
    _write_json(output_dir, "artifact_hash_manifest.json", {
        "status": status, "e1_2_choice": e12,
        "source_fingerprint": fixture.source_fingerprint,
        "starting_hash": baseline.phenotype_hash, "files": files,
    })
    return {"status": status, "e1_2_choice": e12,
            "attempts": len(neighborhoods), "accepted": len(accepted),
            "output_dir": str(output_dir)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.output_dir), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
