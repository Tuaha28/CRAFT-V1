"""
Fresh, raw GA-vs-baselines comparison table, requested explicitly because
no single run in this session produced one with all six metrics
(fitness, makespan, on-time rate, throughput, mean tardiness, labour
utilization) for GA alongside FIFO/EDF/CriticalRatio together --
`run_baselines()`'s own log lines cover five of the six repeatedly
throughout this session's scripts, but never paired with a GA row in one
place, and throughput was never computed at all (not a native field of
evaluate_schedule's return_details).

Frozen snapshot "20260825" (database/ga_snapshot.py), same standard as
every other comparison this session. Two cases: window 1 (dense-demand,
consistently ~10% on-time under baselines throughout this session) and
window 2 (slack-abundant, consistently ~50%+ on-time under baselines
throughout this session) -- the same two windows used throughout this
investigation for the dense/slack contrast. GA: full optimize() + polish,
standard production settings (pop=50, gen=50, seed=42, polish max_evals=
400), matching every other GA run in this session.

Throughput (orders/day) is DERIVED, not a native evaluate_schedule field:
orders_with_deadline / makespan_days, computed identically for every
strategy from the same evaluate_schedule(return_details=True) call.

Does NOT modify ga/scheduler.py or ga/fitness.py.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database.ga_snapshot as ga_snapshot
SNAPSHOT_NAME = "20260825"
ga_snapshot.activate_snapshot(SNAPSHOT_NAME)

import pandas as pd
import torch
from datetime import datetime

from database.db import get_engine
from database.queries import fetch_concurrent_operator_capacity
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from ga.scheduler import GAScheduler, make_key
from ga.fitness import evaluate_schedule
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR

COMMIT_SIZE = 300
POP_SIZE = 50
GENERATIONS = 50
POLISH_EVALS = 400
SEED = 42

print("=" * 70)
print(f"SETUP -- frozen snapshot '{SNAPSHOT_NAME}'")
print("=" * 70)
ORIGIN = ga_snapshot.get_snapshot_origin(SNAPSHOT_NAME)
print(f"Frozen origin: {ORIGIN}")

builder = FactoryGraphBuilder()
graph, _ = builder.build_graph()
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())
df_machines = builder.df_machines

model = MultiTaskFactoryGNN(metadata=graph.metadata(), hidden_channels=GNN_HIDDEN_CHANNELS)
model.load_state_dict(torch.load(os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")))
model.eval()
with torch.no_grad():
    out = model(graph.x_dict, graph.edge_index_dict, graph['operation'].type_idx)
delay = torch.sigmoid(out['delay']).squeeze(-1)
preds_all = {k: {'delay': delay[i].item(), 'priority': out['priority'][i].item()}
             for i, k in enumerate(builder.op_map.keys())}
capacity = fetch_concurrent_operator_capacity()

order_release = builder.df_routings.groupby('production_order_id')['release_date'].min()
all_orders_sorted = order_release.reset_index().sort_values(['release_date', 'production_order_id'])
all_ids = all_orders_sorted['production_order_id'].tolist()


def window_data(pos, size):
    ids = set(all_ids[pos:pos + size])
    sub = builder.df_routings[builder.df_routings['production_order_id'].isin(ids)]
    keys = {make_key(r['production_order_id'], r['operation_id']) for _, r in sub.iterrows()}
    preds = {k: v for k, v in preds_all.items() if k in keys}
    return sub, preds


window1_sub, window1_preds = window_data(0, COMMIT_SIZE)
window2_sub, window2_preds = window_data(COMMIT_SIZE, COMMIT_SIZE)

CASES = [
    ("window1_dense", window1_sub, window1_preds),
    ("window2_slack", window2_sub, window2_preds),
]

all_results = {}

for case_name, sub, preds_window in CASES:
    print()
    print("=" * 70)
    print(f"CASE: {case_name}")
    print("=" * 70)
    sched = GAScheduler(sub, df_machines, df_lines, preds_window,
                         current_time=ORIGIN, present_operator_count=capacity)

    baselines = sched.run_baselines()

    t0 = datetime.now()
    sols, logbook = sched.optimize(pop_size=POP_SIZE, generations=GENERATIONS, n_solutions=1, random_seed=SEED)
    polished, pstats = sched.polish(sols[0][0], max_evals=POLISH_EVALS, random_seed=SEED)
    ga_elapsed = (datetime.now() - t0).total_seconds()
    _f, ga_det = evaluate_schedule(
        list(polished), sched.num_ops, sched.op_details, sched.gnn_predictions,
        all_line_ids=sched.active_line_ids, order_deadlines=sched.order_deadlines_minutes,
        order_release_minutes=sched.order_release_minutes,
        machine_pools=sched.factory.machine_ids_per_line_map,
        machine_line_map=sched.machine_line_map,
        usable_machine_count=sched.usable_machine_count,
        present_operator_count=sched.present_operator_count,
        return_details=True,
        op_optype=sched.factory.optype_for_op,
        protected_machine_type=sched.protected_machine_type,
    )
    print(f"GA done in {ga_elapsed:.0f}s")

    rows = {}
    for name in ('FIFO', 'EDF', 'CriticalRatio'):
        det = baselines[name]
        rows[name] = det
    rows['GA'] = ga_det

    for name, det in rows.items():
        throughput = det['orders_with_deadline'] / det['makespan_days'] if det['makespan_days'] else 0.0
        det['throughput_orders_per_day'] = throughput

    all_results[case_name] = {
        'orders': sub['production_order_id'].nunique(),
        'origin': ORIGIN,
        'rows': rows,
    }

print()
print("=" * 70)
print("RAW TABLE -- frozen snapshot '20260825', GA seed=42 (pop=50, gen=50, polish=400)")
print("=" * 70)

for case_name, data in all_results.items():
    print()
    print(f"--- {case_name} ({data['orders']} orders, origin {data['origin']}) ---")
    header = f"{'strategy':<15}{'fitness':>10}{'makespan(d)':>13}{'on-time%':>10}{'throughput/d':>14}{'mean_tardy(h)':>15}{'labour%':>10}"
    print(header)
    for name in ('GA', 'FIFO', 'EDF', 'CriticalRatio'):
        det = data['rows'][name]
        print(f"{name:<15}{det['fitness']:>10.2f}{det['makespan_days']:>13.2f}"
              f"{det['on_time_rate']*100:>9.1f}%{det['throughput_orders_per_day']:>14.2f}"
              f"{det['mean_tardiness_hours']:>15.2f}{det['labor_utilization_rate']*100:>9.1f}%")

print("\nDONE")
