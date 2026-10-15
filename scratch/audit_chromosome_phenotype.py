"""Read-only current-production genotype-to-phenotype correspondence probe."""
import json
import random
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine
from database.queries import (
    fetch_active_orders_and_routings,
    fetch_committed_machine_state,
    fetch_concurrent_operator_capacity,
    fetch_machine_state,
)
from ga.chromosome import creator
from ga.heuristics import baseline_chromosomes
from ga.operators import repair_line_and_machine_count, repair_precedence
from ga.scheduler import GAScheduler
import ga.fitness as fitness_module


OUT = Path("outputs/integration_audit/chromosome_phenotype.json")


def hamming(a, b):
    return sum(x != y for x, y in zip(a, b))


def main():
    random.seed(42)
    now = datetime.now()
    routings = fetch_active_orders_and_routings()
    order_frame = (
        routings.groupby("production_order_id", as_index=False)["release_date"].min()
        .sort_values(["release_date", "production_order_id"])
    )
    selected_orders = set(order_frame["production_order_id"].head(12))
    window = routings[routings["production_order_id"].isin(selected_orders)].copy()
    machines = fetch_machine_state()
    lines = pd.read_sql(
        text("SELECT line_id, department_id, line_number, "
             "target_efficiency_percentage, is_active FROM Production_Lines "
             "WHERE is_active=1"),
        get_engine(),
    )
    committed_free, committed_last = fetch_committed_machine_state(now)
    preds = {
        f"{r.production_order_id}_{r.operation_id}": {"delay": 0.30, "priority": 0.50}
        for r in window.itertuples(index=False)
    }
    scheduler = GAScheduler(
        window, machines, lines, preds, current_time=now,
        present_operator_count=fetch_concurrent_operator_capacity(),
        initial_machine_free=committed_free,
        initial_machine_last_order=committed_last,
        scored_order_ids={str(x) for x in selected_orders},
    )
    base = creator.Individual(baseline_chromosomes(
        scheduler.factory, scheduler.op_details, scheduler.order_deadlines_minutes,
        scheduler.seq_lookup,
    )["EDF"])
    n = scheduler.num_ops
    assert len(base) == 3 * n

    original_select = fitness_module.select_machines_and_start
    machine_line = scheduler.machine_line_map

    def score_and_trace(ind):
        calls = []
        def traced(pool_ids, machine_free, machine_last_order, order_id,
                   machine_count, ready, changeover, **kwargs):
            start, chosen = original_select(
                pool_ids, machine_free, machine_last_order, order_id,
                machine_count, ready, changeover, **kwargs)
            calls.append({
                "start_minutes": float(start),
                "machine_ids": [int(x) for x in chosen],
                "line_ids": sorted({int(machine_line[x]) for x in chosen}),
            })
            return start, chosen
        fitness_module.select_machines_and_start = traced
        try:
            fit, details = fitness_module.evaluate_schedule(
                ind, return_details=True, **scheduler._eval_ctx)
        finally:
            fitness_module.select_machines_and_start = original_select
        os_part = list(ind[:n])
        for i, call in enumerate(calls):
            call["operation_key"] = os_part[i]
            call["line_gene"] = int(ind[n + i])
            call["machine_count_gene"] = int(ind[2 * n + i])
        return float(fit[0]), details, calls

    base_fit, base_details, base_trace = score_and_trace(base)

    # OS perturbation: swap positions occupied by different orders, then run
    # exactly the same precedence and LA/MC repair production mutation uses.
    os_candidate = creator.Individual(list(base))
    os_part = list(os_candidate[:n])
    la_part = list(os_candidate[n:2*n])
    mc_part = list(os_candidate[2*n:])
    pair = None
    for i in range(n):
        for j in range(i + 1, min(n, i + 25)):
            if os_part[i].split("_")[0] != os_part[j].split("_")[0]:
                pair = (i, j)
                break
        if pair:
            break
    if pair is None:
        raise RuntimeError("could not find cross-order OS swap")
    i, j = pair
    os_part[i], os_part[j] = os_part[j], os_part[i]
    os_part = repair_precedence(os_part, scheduler.seq_lookup)
    os_part, la_part, mc_part = repair_line_and_machine_count(
        os_part, la_part, mc_part, scheduler.factory.eligibility_map,
        scheduler.factory.machines_per_line_map,
        40, scheduler.op_work, scheduler.factory.optype_for_op,
        scheduler.protected_machine_type,
    )
    os_candidate[:] = os_part + la_part + mc_part
    os_fit, os_details, os_trace = score_and_trace(os_candidate)

    # LA perturbation: move one operation to another physically eligible line.
    la_candidate = creator.Individual(list(base))
    la_index = None
    for k, op in enumerate(base[:n]):
        alternatives = [
            line for line in scheduler.factory.eligibility_map[op]
            if int(line) != int(base[n+k])
        ]
        if alternatives:
            la_index = k
            la_candidate[n+k] = alternatives[0]
            cap = scheduler.factory._max_machines_for(op, alternatives[0])
            la_candidate[2*n+k] = min(int(la_candidate[2*n+k]), cap)
            break
    if la_index is None:
        raise RuntimeError("could not find LA alternative")
    la_fit, la_details, la_trace = score_and_trace(la_candidate)

    # MC perturbation: change count within the exact physical line cap.
    mc_candidate = creator.Individual(list(base))
    mc_index = None
    for k, op in enumerate(base[:n]):
        cap = scheduler.factory._max_machines_for(op, base[n+k])
        current = int(base[2*n+k])
        if cap > 1:
            replacement = 1 if current != 1 else cap
            if replacement != current:
                mc_index = k
                mc_candidate[2*n+k] = replacement
                break
    if mc_index is None:
        raise RuntimeError("could not find MC alternative")
    mc_fit, mc_details, mc_trace = score_and_trace(mc_candidate)

    def trace_differences(left, right):
        changed = []
        for idx, (a, b) in enumerate(zip(left, right)):
            if a != b:
                changed.append({
                    "position": idx,
                    "base": a,
                    "candidate": b,
                })
        return changed

    def result(name, candidate, fit, details, trace, target):
        return {
            "name": name,
            "target": target,
            "fitness": fit,
            "fitness_delta": fit - base_fit,
            "gene_hamming": {
                "OS": hamming(base[:n], candidate[:n]),
                "LA": hamming(base[n:2*n], candidate[n:2*n]),
                "MC": hamming(base[2*n:], candidate[2*n:]),
            },
            "phenotype_trace_changed_operations": len(trace_differences(base_trace, trace)),
            "phenotype_trace_differences": trace_differences(base_trace, trace)[:12],
            "makespan_minutes": details["makespan_minutes"],
            "orders_late": details["orders_late"],
            "pool_short_operations": details["pool_short_operations"],
        }

    payload = {
        "measurement_scope": {
            "database": "current configured database",
            "timestamp": now.isoformat(),
            "orders": len(selected_orders),
            "operations": n,
            "committed_machines": len(committed_free),
            "constructor": "EDF chromosome through current GAScheduler",
            "predictions": "constant delay=0.30 priority=0.50 to isolate genes",
        },
        "base": {
            "fitness": base_fit,
            "makespan_minutes": base_details["makespan_minutes"],
            "orders_late": base_details["orders_late"],
            "pool_short_operations": base_details["pool_short_operations"],
        },
        "perturbations": [
            result("OS", os_candidate, os_fit, os_details, os_trace,
                   {"swapped_positions": [i, j]}),
            result("LA", la_candidate, la_fit, la_details, la_trace,
                   {"position": la_index, "operation": base[la_index],
                    "from": int(base[n+la_index]), "to": int(la_candidate[n+la_index])}),
            result("MC", mc_candidate, mc_fit, mc_details, mc_trace,
                   {"position": mc_index, "operation": base[mc_index],
                    "from": int(base[2*n+mc_index]), "to": int(mc_candidate[2*n+mc_index])}),
        ],
        "invariants": {
            "all_candidates_length_3n": all(
                len(c) == 3*n for c in (base, os_candidate, la_candidate, mc_candidate)),
            "all_os_permutations_preserved": all(
                sorted(c[:n]) == sorted(base[:n])
                for c in (os_candidate, la_candidate, mc_candidate)),
            "all_machine_identity_lines_match_LA": all(
                not call["machine_ids"] or call["line_ids"] == [call["line_gene"]]
                for trace in (base_trace, os_trace, la_trace, mc_trace)
                for call in trace
            ),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
