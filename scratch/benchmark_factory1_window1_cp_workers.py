"""Replay one byte-identical Factory-1 Window-1 CP model by worker count."""
from __future__ import annotations

import hashlib
import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
from ortools.sat.python import cp_model

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "outputs" / "crafter" / "factory1_cp_parallelism_audit_v1"
CHECKPOINT = OUT / "checkpoint"
REPORT = OUT / "cp_worker_benchmark.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _checkpoint():
    manifest = json.loads((CHECKPOINT / "checkpoint_manifest.json").read_text(
        encoding="utf-8"))
    rows = list(manifest.get("checkpoints") or ())
    if not rows:
        raise RuntimeError("no checkpointed CP model")
    # The final checkpoint is the largest, fourth-round domain.
    row = max(rows, key=lambda item: (
        int(item.get("regime_count", 0)),
        int(item.get("resource_configuration_count", 0)),
        int(item.get("checkpoint_index", 0)),
    ))
    proto = (CHECKPOINT / row["model_proto"]).read_bytes()
    if _sha(proto) != row["model_sha256"]:
        raise RuntimeError("checkpoint model hash mismatch")
    return row, proto


def _model(proto: bytes):
    model = cp_model.CpModel()
    model.Proto().parse_text_format(proto.decode("utf-8"))
    loaded = str(model.Proto()).encode("utf-8")
    if loaded != proto:
        raise RuntimeError("parsed CP model is not byte-identical")
    return model


def _solve(proto: bytes, metadata: dict, workers: int) -> dict:
    model = _model(proto)
    solver = cp_model.CpSolver()
    saved = metadata.get("solver_parameters") or {}
    solver.parameters.num_search_workers = int(workers)
    solver.parameters.random_seed = int(saved.get("random_seed", 42))
    solver.parameters.cp_model_presolve = bool(saved.get("cp_model_presolve", True))
    solver.parameters.use_optimization_hints = bool(
        saved.get("use_optimization_hints", True))
    solver.parameters.max_time_in_seconds = float(
        saved.get("max_time_in_seconds", 60.0))
    solver.parameters.log_search_progress = True
    solver.parameters.log_to_stdout = False
    log_lines: list[str] = []
    solver.log_callback = log_lines.append

    process = psutil.Process()
    peak = {"rss": int(process.memory_info().rss)}
    stop = threading.Event()

    def sample():
        while not stop.wait(0.01):
            try:
                peak["rss"] = max(peak["rss"], int(process.memory_info().rss))
            except psutil.Error:
                return

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    cpu_before = process.cpu_times()
    wall_started = time.perf_counter()
    code = solver.Solve(model)
    wall_seconds = time.perf_counter() - wall_started
    cpu_after = process.cpu_times()
    stop.set()
    sampler.join(timeout=1.0)
    cpu_seconds = ((cpu_after.user + cpu_after.system) -
                   (cpu_before.user + cpu_before.system))
    text = "\n".join(log_lines)
    worker_match = re.search(r"Starting search at .* with (\d+) workers", text)
    subsolvers = re.findall(r"(\d+) (?:full problem )?subsolvers", text)
    result = {
        "worker_count": int(workers),
        "actual_search_workers_from_log": (
            int(worker_match.group(1)) if worker_match else None),
        "subsolver_counts_from_log": [int(value) for value in subsolvers],
        "cp_status": solver.StatusName(code),
        "candidate_exists": code in (cp_model.FEASIBLE, cp_model.OPTIMAL),
        "wall_time_seconds": wall_seconds,
        "solver_wall_time_seconds": solver.WallTime(),
        "cpu_time_seconds": cpu_seconds,
        "average_process_cpu_percent": (
            100.0 * cpu_seconds / wall_seconds if wall_seconds else None),
        "peak_rss_mb": peak["rss"] / (1024.0 * 1024.0),
        "branches": solver.NumBranches(),
        "conflicts": solver.NumConflicts(),
        "deterministic_time": getattr(
            solver.ResponseProto(), "deterministic_time", None),
        "objective": solver.ObjectiveValue(),
        "best_bound": solver.BestObjectiveBound(),
        "time_limit_seconds": float(solver.parameters.max_time_in_seconds),
        "random_seed": int(solver.parameters.random_seed),
        "model_sha256": _sha(proto),
        "log_excerpt": [line for line in log_lines
                        if ("Starting search" in line or "subsolvers" in line)][:20],
    }
    (OUT / f"cp_search_workers_{workers}.log").write_text(
        text + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=int)
    parser.add_argument("--result")
    args = parser.parse_args()
    metadata, proto = _checkpoint()
    if args.worker is not None:
        result = _solve(proto, metadata, int(args.worker))
        if args.result:
            Path(args.result).write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
        else:
            print(json.dumps(result, sort_keys=True), flush=True)
        return
    results = []
    for workers in (1, 2, 4):
        result_path = OUT / f"isolated_result_workers_{workers}.json"
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()),
             "--worker", str(workers), "--result", str(result_path)],
            check=True,
        )
        results.append(json.loads(result_path.read_text(encoding="utf-8")))
    one = results[0]["wall_time_seconds"]
    for result in results:
        result["speedup_vs_1_worker"] = (
            one / result["wall_time_seconds"]
            if result["wall_time_seconds"] else None)
    payload = {
        "hardware": {
            "physical_cpu_cores": psutil.cpu_count(logical=False),
            "logical_cpu_threads": psutil.cpu_count(logical=True),
            "available_ram_gb": psutil.virtual_memory().available / (1024.0 ** 3),
            "total_ram_gb": psutil.virtual_memory().total / (1024.0 ** 3),
        },
        "ortools_version": __import__("ortools").__version__,
        "checkpoint": metadata,
        "model_sha256": _sha(proto),
        "results": results,
        "database_writes": 0,
    }
    REPORT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(json.dumps(payload, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
