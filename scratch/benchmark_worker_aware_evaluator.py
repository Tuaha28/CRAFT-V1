"""Benchmark the old worker-blind and authoritative worker-aware evaluator."""
import cProfile
import io
import json
import os
import pstats
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

SNAPSHOT = "authoritative_20260911"
import database.ga_snapshot as ga_snapshot
ga_snapshot.activate_snapshot(SNAPSHOT)
ORIGIN = ga_snapshot.get_snapshot_origin(SNAPSHOT)

import pandas as pd
from agent.decision_engine import AIDecisionEngine
from database.db import get_engine
from database.queries import (
    fetch_active_orders_and_routings,
    fetch_assignable_operators,
    fetch_committed_machine_state,
    fetch_committed_operator_reservations,
    fetch_concurrent_operator_capacity,
    fetch_line_factory_map,
    fetch_machine_state,
)
from ga.heuristics import baseline_chromosomes
from ga.scheduler import GAScheduler


def timed(fn, repeats=2):
    values = []
    result = None
    for _ in range(repeats):
        start = time.perf_counter()
        result = fn()
        values.append(time.perf_counter() - start)
    return values, result


routings = fetch_active_orders_and_routings()
machines = fetch_machine_state()
lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, "
    "target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active=1",
    get_engine(),
)
order_release = (
    routings.groupby("production_order_id")["release_date"].min()
    .reset_index().sort_values(["release_date", "production_order_id"])
)
window_ids = set(order_release["production_order_id"].tolist()[:300])
window = routings[routings["production_order_id"].isin(window_ids)]
predictions = {
    f"{row.production_order_id}_{row.operation_id}":
        {"delay": 0.3, "priority": 0.5}
    for row in window.itertuples()
}
machine_free, machine_last = fetch_committed_machine_state(ORIGIN)
operator_reservations = fetch_committed_operator_reservations(ORIGIN)
operator_index = AIDecisionEngine._build_operator_index(
    fetch_assignable_operators(), lines, fetch_line_factory_map()
)
scheduler = GAScheduler(
    window, machines, lines, predictions,
    current_time=ORIGIN,
    present_operator_count=fetch_concurrent_operator_capacity(),
    initial_machine_free=machine_free,
    initial_machine_last_order=machine_last,
)
chromosome = baseline_chromosomes(
    scheduler.factory, scheduler.op_details,
    scheduler.order_deadlines_minutes, scheduler.seq_lookup,
)["EDF"]

blind_ctx = dict(scheduler._eval_ctx)
blind_ctx["operator_index"] = None
blind_ctx["initial_operator_reservations"] = None
aware_ctx = dict(scheduler._eval_ctx)
aware_ctx["operator_index"] = operator_index
aware_ctx["initial_operator_reservations"] = operator_reservations

blind_times, blind = timed(
    lambda: __import__("ga.fitness", fromlist=["evaluate_schedule"]).evaluate_schedule(
        chromosome, return_details=True, **blind_ctx
    )
)
aware_cold_start = time.perf_counter()
aware_cold = __import__("ga.fitness", fromlist=["evaluate_schedule"]).evaluate_schedule(
    chromosome, return_details=True, **aware_ctx
)
aware_cold_seconds = time.perf_counter() - aware_cold_start
aware_times, aware = timed(
    lambda: __import__("ga.fitness", fromlist=["evaluate_schedule"]).evaluate_schedule(
        chromosome, return_details=True, **aware_ctx
    )
)

profiler = cProfile.Profile()
profiler.enable()
__import__("ga.fitness", fromlist=["evaluate_schedule"]).evaluate_schedule(
    chromosome, return_details=True, **aware_ctx
)
profiler.disable()
stream = io.StringIO()
pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(20)

result = {
    "snapshot": SNAPSHOT,
    "origin": ORIGIN.isoformat(),
    "orders": len(window_ids),
    "operations": scheduler.num_ops,
    "blind_seconds": blind_times,
    "worker_aware_cold_seconds": aware_cold_seconds,
    "worker_aware_warm_seconds": aware_times,
    "warm_multiplier_vs_blind": (
        (sum(aware_times) / len(aware_times)) /
        (sum(blind_times) / len(blind_times))
    ),
    "blind_fitness": blind[0][0],
    "worker_aware_fitness": aware[0][0],
    "blind_makespan_minutes": blind[1]["makespan_minutes"],
    "worker_aware_makespan_minutes": aware[1].get("makespan_minutes"),
    "worker_aware_phenotype_hash": aware[1].get("phenotype_hash"),
    "worker_aware_invalid": aware[1].get("invalid"),
    "worker_aware_infeasibility": aware[1].get("infeasibility"),
    "profile_top20": stream.getvalue(),
}
out = Path("outputs/integration_audit/worker_aware_evaluator_benchmark.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps({k: v for k, v in result.items() if k != "profile_top20"}, indent=2))
print(out)
