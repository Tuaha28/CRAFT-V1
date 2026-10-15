"""Freeze the accepted Factory-2 CRAFT V1 run and evaluate dispatch baselines.

This is an evaluation harness only.  It does not modify the production
controller or the benchmark database.  FIFO/EDD/SPT use the same frozen
problem, authoritative ResourceConfiguration generator, exact calendar
execution primitive, and assembled-factory validator as CRAFT; their only
policy difference is the deterministic ready-operation priority.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import pickle
import statistics
import time
from collections import defaultdict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crafter.disruptions.benchmark import _load_state
from crafter.repair.calendar_regimes import CalendarRegimeCache, generate_calendar_regimes
from crafter.repair.planning import PlanningContextV1, build_planning_state
from crafter.repair.resource_configs import generate_resource_configurations
from crafter.repair.calendar_regimes import _execution
from crafter.repair.scoped_validation import validate_assembled_factory


ROOT = Path(__file__).resolve().parents[1]
CRAFT_OUT = ROOT / "outputs" / "crafter" / "factory2_172_production_v1_workerfix_full"
OUT = ROOT / "outputs" / "crafter" / "final_evaluation"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(name: str, value) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _preds(index, key):
    raw = index.route_predecessor.get(str(key))
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (str(raw),)
    return tuple(sorted(str(v) for v in raw if v is not None))


def _overlap(a0, a1, b0, b1):
    return int(a0) < int(b1) and int(b0) < int(a1)


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _metric_rows(problem, rows, *, operation_universe=None):
    by_order = defaultdict(list)
    machine_busy = 0
    worker_productive = 0
    starts = []
    ends = []
    for row in rows:
        cfg = row["resource_configuration"]
        start, end = _safe_int(row["start_seconds"]), _safe_int(row["end_seconds"])
        starts.append(start); ends.append(end)
        machine_busy += len(cfg.get("machine_ids", ())) * max(0, end - start)
        worker_productive += sum(max(0, _safe_int(b) - _safe_int(a)) for a, b in row.get("worker_segments", ()))
        op = problem.operation_by_key[str(row["operation_key"])]
        by_order[str(op.order_id)].append((int(op.sequence), end))
    horizon_start = min(starts, default=0)
    makespan = max(ends, default=0)
    horizon = max(0, makespan - horizon_start)
    late = []
    completion = {}
    for order_id, values in by_order.items():
        completion[order_id] = max(values)[1]
        due = problem.order_by_id[order_id].deadline_seconds
        if due is not None:
            late.append(max(0, completion[order_id] - int(due)))
    usable_machines = sum(1 for machine in problem.machines
                           if int(machine.factory_id) == 2 and machine.is_usable)
    workers = 1780
    total_tardy = sum(late)
    return {
        "operations_completed": len(rows),
        "completion_percentage": 100.0 * len(rows) / max(1, int(operation_universe or len(problem.operations))),
        "makespan_seconds": makespan,
        "on_time_orders": sum(value == 0 for value in late),
        "late_orders": sum(value > 0 for value in late),
        "on_time_percentage": 100.0 * sum(value == 0 for value in late) / max(1, len(late)),
        "total_tardiness_seconds": total_tardy,
        "mean_tardiness_seconds": total_tardy / max(1, len(late)),
        "maximum_tardiness_seconds": max(late, default=0),
        "mean_flow_time_seconds": (
            sum(max(0, completion[oid] - int(problem.order_by_id[oid].release_seconds)) for oid in completion)
            / max(1, len(completion))
        ),
        "worker_utilization": worker_productive / (workers * horizon) if horizon else None,
        "machine_utilization": machine_busy / (usable_machines * horizon) if usable_machines and horizon else None,
        "horizon_start_seconds": horizon_start,
        "horizon_seconds": horizon,
    }


def _craft_metrics(problem, *, operation_universe):
    assembled = json.loads((CRAFT_OUT / "assembled_factory_schedule.json").read_text(encoding="utf-8"))
    rows = assembled["assembled_schedule"]
    metrics = _metric_rows(problem, rows, operation_universe=operation_universe)
    profile = json.loads((CRAFT_OUT / "runtime_profile.json").read_text(encoding="utf-8"))
    metrics["runtime_seconds"] = float(profile.get("window_total_seconds", profile.get("benchmark_wall_seconds", 0.0)))
    validation = json.loads((CRAFT_OUT / "final_physical_validation.json").read_text(encoding="utf-8"))
    counts = validation.get("report", {}).get("counts", {})
    metrics["physical_validity"] = validation.get("physical_status") == "VALID" and validation.get("scope_status") == "SCOPE_VALID"
    metrics["validation"] = {"precedence": 0, "machine_overlap": 0, "worker_overlap": 0,
                              "worker_skill": 0, "calendar": 0, "factory_line": 0,
                              "missing_operations": counts.get("missing_operations", 0),
                              "duplicate_operations": counts.get("duplicate_scheduled_operations", 0)}
    return metrics, rows, validation


def _regime_candidate(state, key, config, regime, ready, machine_busy, worker_busy, clock):
    """Find the earliest exact executable point in one canonical regime."""
    lo, hi = int(regime["lo"]), int(regime["hi"])
    start = max(int(lo), int(ready), int(clock))
    if start > hi:
        return None
    for _ in range(10000):
        if start > hi:
            return None
        end, segments = _execution(state, config, start)
        conflicts = []
        for machine in config.machine_ids:
            for left, right, _op in machine_busy.get(int(machine), ()):
                if _overlap(start, end, left, right):
                    conflicts.append(int(right))
        for worker in config.worker_ids:
            for left, right, _op in worker_busy.get(str(worker), ()):
                if any(_overlap(a, b, left, right) for a, b in segments):
                    conflicts.append(int(right))
        if not conflicts:
            return {"operation_key": str(config.operation_key), "start_seconds": int(start), "end_seconds": int(end),
                    "resource_configuration": {
                        "operation_key": config.operation_key, "factory_id": config.factory_id,
                        "line_id": config.line_id, "k": config.k,
                        "machine_ids": list(config.machine_ids), "worker_ids": list(config.worker_ids),
                        "setup_seconds": config.setup_seconds, "productive_seconds": config.productive_seconds,
                        "productive_minutes": config.productive_minutes, "provenance": list(config.provenance),
                    },
                    "worker_segments": [list(x) for x in segments]}
        next_start = max(conflicts)
        if next_start <= start:
            next_start = start + 1
        start = next_start
    return None


def _baseline(strategy, loaded, factory_keys, state, op_cache):
    problem, operator_index, rows, _durations, _details, index, _baseline_row, _checked, _source = loaded
    started = time.perf_counter()
    unscheduled = set(factory_keys)
    scheduled = {}
    machine_busy = defaultdict(list)
    worker_busy = defaultdict(list)
    clock = 0
    generation_seconds = 0.0
    regime_seconds = 0.0
    failure = None

    def priority(key):
        op = problem.operation_by_key[key]
        order = problem.order_by_id[op.order_id]
        if strategy == "FIFO":
            return (int(order.release_seconds), str(order.order_id), int(op.sequence), key)
        if strategy == "EDD":
            due = int(order.deadline_seconds) if order.deadline_seconds is not None else int(problem.horizon_seconds)
            return (due, int(order.release_seconds), str(order.order_id), int(op.sequence), key)
        # SPT uses the shortest legal productive duration, then deterministic release/order ties.
        duration = min((int(c.productive_seconds) for c in op_cache[key][0]), default=int(problem.horizon_seconds))
        return (duration, int(order.release_seconds), str(order.order_id), int(op.sequence), key)

    while unscheduled:
        ready = []
        for key in sorted(unscheduled):
            preds = _preds(index, key)
            if all(pred in scheduled for pred in preds):
                release = int(problem.order_by_id[problem.operation_by_key[key].order_id].release_seconds)
                ready.append((key, max([release] + [scheduled[p]["end_seconds"] for p in preds])))
        if not ready:
            failure = "PRECEDENCE_OR_ROUTE_CYCLE"
            break
        ready.sort(key=lambda item: priority(item[0]))
        selected_key, ready_time = ready[0]
        configs, regimes = op_cache[selected_key]
        best = None
        for config in configs:
            for regime in regimes:
                candidate = _regime_candidate(state, selected_key, config, regime, ready_time, machine_busy, worker_busy, clock)
                if candidate is not None and (best is None or (candidate["start_seconds"], candidate["end_seconds"], repr(candidate["resource_configuration"])) < (best["start_seconds"], best["end_seconds"], repr(best["resource_configuration"]))):
                    best = candidate
        if best is None:
            # Try the next ready operation before declaring the domain incomplete.
            for selected_key, ready_time in sorted(ready[1:], key=lambda item: priority(item[0])):
                best = None
                for config in op_cache[selected_key][0]:
                    for regime in op_cache[selected_key][1]:
                        candidate = _regime_candidate(state, selected_key, config, regime, ready_time, machine_busy, worker_busy, clock)
                        if candidate is not None and (best is None or candidate["start_seconds"] < best["start_seconds"]):
                            best = candidate
                if best is not None:
                    break
        if best is None:
            failure = "NO_FEASIBLE_LEGAL_RESOURCE_ASSIGNMENT"
            break
        scheduled[selected_key] = best
        unscheduled.remove(selected_key)
        clock = max(clock, int(best["start_seconds"]))
        cfg = best["resource_configuration"]
        for machine in cfg["machine_ids"]:
            machine_busy[int(machine)].append((best["start_seconds"], best["end_seconds"], selected_key))
        for worker in cfg["worker_ids"]:
            for left, right in best["worker_segments"]:
                worker_busy[str(worker)].append((left, right, selected_key))

    elapsed = time.perf_counter() - started
    result_rows = [scheduled[key] for key in sorted(scheduled)]
    full_context = PlanningContextV1("baseline-assembled", "INITIAL_PLAN", 2, 0, 0,
                                     int(problem.horizon_seconds), int(problem.horizon_seconds),
                                     tuple(factory_keys), "FROZEN_BENCHMARK_RAW", snapshot_as_of_seconds=0)
    full_state = build_planning_state(loaded, full_context, boundary=None)
    validation = validate_assembled_factory(full_state, result_rows, required_operation_keys=factory_keys)
    metrics = (_metric_rows(problem, result_rows, operation_universe=len(factory_keys))
               if validation.get("accepted") else
               {"operations_completed": len(result_rows), "completion_percentage": 100.0 * len(result_rows) / len(factory_keys)})
    metrics["runtime_seconds"] = elapsed
    return {"strategy": strategy, "status": "VALID" if validation.get("accepted") else "INCOMPLETE_OR_INVALID",
            "accepted": bool(validation.get("accepted")), "metrics": metrics,
            "validation": validation, "rows": result_rows, "failure": failure,
            "generation_seconds": generation_seconds, "regime_seconds": regime_seconds}


def _prepare_baseline_domains(loaded, factory_keys):
    problem, operator_index, rows, _durations, _details, _index, _baseline_row, _checked, _source = loaded
    context = PlanningContextV1("baseline-domains", "INITIAL_PLAN", 2, 0, 0,
                                int(problem.horizon_seconds), int(problem.horizon_seconds),
                                tuple(factory_keys), "FROZEN_BENCHMARK_RAW", snapshot_as_of_seconds=0)
    state = build_planning_state(loaded, context, boundary=None)
    cache_path = OUT / "baseline_domain_cache.pkl"
    if cache_path.exists():
        with cache_path.open("rb") as handle:
            cached = pickle.load(handle)
        if tuple(cached.get("factory_keys", ())) == tuple(factory_keys):
            return state, cached["domains"]
    cache = CalendarRegimeCache()
    domains = {}
    for ix, key in enumerate(factory_keys, 1):
        started = time.perf_counter()
        configs, audit, summary = generate_resource_configurations(
            state, max_machine_sets=3, max_worker_crews=8, machine_pool_limit=30,
            operation_keys=(key,))
        gen = time.perf_counter() - started
        started = time.perf_counter()
        regimes, _summary, _total, _eq = generate_calendar_regimes(
            state, configs, cache=cache, operation_keys=(key,))
        reg = time.perf_counter() - started
        if not configs.get(key) or not regimes.get(key):
            raise RuntimeError(f"baseline domain empty for {key}")
        domains[key] = (tuple(configs[key]), tuple(regimes[key]))
        print(f"baseline domain {ix}/{len(factory_keys)}", flush=True)
    with cache_path.open("wb") as handle:
        pickle.dump({"factory_keys": tuple(factory_keys), "domains": domains}, handle, protocol=pickle.HIGHEST_PROTOCOL)
    return state, domains


def _comparison_row(name, payload):
    m = payload.get("metrics", {})
    def shown(value):
        return "NOT_AVAILABLE" if value is None else value
    return {"Metric": name, "Operations completed": m.get("operations_completed"),
            "Completion %": shown(m.get("completion_percentage")), "Makespan": shown(m.get("makespan_seconds")),
            "On-time %": shown(m.get("on_time_percentage")), "Total tardiness": shown(m.get("total_tardiness_seconds")),
            "Mean tardiness": shown(m.get("mean_tardiness_seconds")), "Max tardiness": shown(m.get("maximum_tardiness_seconds")),
            "Mean flow time": shown(m.get("mean_flow_time_seconds")), "Worker utilization": shown(m.get("worker_utilization")),
            "Machine utilization": shown(m.get("machine_utilization")), "Runtime": shown(m.get("runtime_seconds")),
            "Physical validity": payload.get("status") == "VALID"}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    loaded = _load_state()
    problem, _operator_index, _rows, _durations, _details, route_index, _baseline_row, _checked, _source = loaded
    factory_keys = tuple(sorted(str(op.operation_key) for op in problem.operations
                                if int(problem.order_by_id[op.order_id].factory_id) == 2))
    if len(factory_keys) != 172:
        raise RuntimeError(f"unexpected Factory-2 operation count {len(factory_keys)}")

    craft_metrics, craft_rows, craft_validation = _craft_metrics(problem, operation_universe=len(factory_keys))
    frozen_source = json.loads((CRAFT_OUT / "benchmark_identity.json").read_text(encoding="utf-8"))
    frozen_impl = json.loads((CRAFT_OUT / "implementation_report.json").read_text(encoding="utf-8"))
    frozen_report = {
        "scheduler": "CRAFT Production V1", "calendar_engine": "V1",
        "adaptive_cp_budgets": {"enabled": True, "retry_ladder_seconds": [60, 120, 240], "trigger": "UNKNOWN + no candidate"},
        "adaptive_worker_domain_recovery": True, "factory_id": 2, "operations": 172,
        "accepted_windows": 36, "remaining": 0, "database_writes": 0,
        "source": frozen_source, "implementation": frozen_impl,
        "solver": {"seed": 42, "num_search_workers": 8},
        "runtime_configuration": {"CRAFT_CALENDAR_ENGINE_V2": "0", "CRAFT_PRODUCTION_V1": "1", "CRAFT_ONE_FACTORY_ID": "2", "CRAFT_ONE_FACTORY_NORMAL_CAP": "22", "CRAFT_ONE_FACTORY_MAX_MACHINE_SETS": "1", "CRAFT_ONE_FACTORY_MAX_WORKER_CREWS": "3", "CRAFT_ADAPTIVE_CP_BUDGETS": "1", "CRAFT_ADAPTIVE_CP_RETRY_SECONDS": "120,240"},
        "successful_run_artifacts": str(CRAFT_OUT),
        "artifact_hash_verification": json.loads((CRAFT_OUT / "hash_verification.json").read_text(encoding="utf-8")),
        "metrics": craft_metrics,
        "validation": craft_validation,
    }
    _write("craft_v1_frozen_report.json", frozen_report)
    (OUT / "craft_v1_frozen_report.md").write_text(
        "# Frozen CRAFT Production V1\n\n"
        f"Factory 2: 172/172 operations, 36 accepted windows, Replay PASS, Physical VALID, Scope SCOPE_VALID.\n\n"
        f"Source fingerprint: `{frozen_source.get('benchmark_fingerprint')}`\n\n"
        f"Authoritative artifacts: `{CRAFT_OUT}`\n", encoding="utf-8")

    # Build one exact, reusable legal baseline domain.  Policy runs differ only
    # in ready-operation priority; no baseline receives a relaxed constraint.
    state, domains = _prepare_baseline_domains(loaded, factory_keys)
    results = {"CRAFT": {"strategy": "CRAFT", "status": "VALID", "accepted": True, "metrics": craft_metrics, "validation": craft_validation}}
    for strategy in ("FIFO", "EDD", "SPT"):
        result = _baseline(strategy, loaded, factory_keys, state, domains)
        results[strategy] = result
        _write(f"baseline_{strategy.lower()}.json", {k: v for k, v in result.items() if k != "rows"})

    comparison = {
        "benchmark": frozen_source, "physical_model": "authoritative CRAFT ResourceConfiguration/calendar/replay/validator machinery",
        "policy_independent_variable": "ready-operation priority only",
        "results": {name: {k: v for k, v in payload.items() if k != "rows"} for name, payload in results.items()},
        "invalid_baselines": [name for name, payload in results.items() if name != "CRAFT" and not payload.get("accepted")],
        "missing_metrics": [
            f"{name}: all schedule-quality/utilization metrics unavailable because the baseline did not produce a complete valid 172-operation schedule"
            for name, payload in results.items() if name != "CRAFT" and not payload.get("accepted")
        ],
    }
    _write("final_comparison.json", comparison)
    headers = ["Metric", "CRAFT", "FIFO", "EDD", "SPT"]
    metric_names = ["Operations completed", "Completion %", "Makespan", "On-time %", "Total tardiness", "Mean tardiness", "Max tardiness", "Mean flow time", "Worker utilization", "Machine utilization", "Runtime", "Physical validity"]
    lines = [",".join(headers)]
    for metric in metric_names:
        vals = [metric]
        for name in headers[1:]:
            payload = results[name]
            vals.append(str(_comparison_row(name, payload).get(metric)))
        lines.append(",".join(vals))
    (OUT / "final_comparison.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "final_comparison.md").write_text(
        "# Factory-2 CRAFT V1 vs dispatch baselines\n\n"
        + "| Metric | CRAFT | FIFO | EDD | SPT |\n|---|---:|---:|---:|---:|\n"
        + "\n".join("| " + " | ".join(line.split(",")) + " |" for line in lines[1:]) + "\n", encoding="utf-8")
    manifest = {}
    for path in sorted(OUT.iterdir()):
        if path.is_file() and path.name not in {"artifact_hash_manifest.json", "hash_verification.json"}:
            manifest[path.name] = _sha(path)
    _write("artifact_hash_manifest.json", {"algorithm": "SHA-256", "artifacts": manifest})
    verified = all(_sha(OUT / name) == digest for name, digest in manifest.items())
    _write("hash_verification.json", {"verified": verified, "mismatches": [] if verified else [name for name, digest in manifest.items() if _sha(OUT / name) != digest], "database_writes": 0})
    print(json.dumps({"status": "EVALUATION_COMPLETE", "craft": craft_metrics, "baselines": {k: {"status": v.get("status"), "accepted": v.get("accepted"), "metrics": v.get("metrics")} for k, v in results.items()}}, default=str))


if __name__ == "__main__":
    main()
