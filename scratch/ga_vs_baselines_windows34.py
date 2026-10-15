"""
Follow-up to scratch/ga_vs_baselines_raw_table.py: windows 3 and 4,
correctly identified this time against GA_AUDIT.md's own rolling-horizon
table (Section "GA vs. baselines"), which classifies windows 1-2 as
"dense, demand-dense early windows" (margins +3.7%, +5.5%) and windows
3-4 as "slack, sparse tail windows" (margins +0.9%, +0.1%, makespans
~119d and ~327d) -- window 2 (orders 301-600) is NOT slack, contrary to
this session's earlier mislabeling.

Same COMMIT_SIZE=300 slicing convention used throughout this session:
window 3 = orders[600:900], window 4 = orders[900:1200] (whatever
remains -- ~129 orders, the tail of the order book, matching GA_AUDIT.md's
window 4 makespan of ~327 days, the seasonal-concentration finding).

METHODOLOGY NOTE, stated explicitly: run STANDALONE (empty initial
machine state), same as this session's windows 1-2 were -- NOT a true
sequential rolling horizon with machine state carried forward from
windows 1-3 (GA_AUDIT.md's original 4-window table may have used real
carry-forward via ga/rolling_horizon.py; that is not reproduced here).
This keeps all four windows reported in this comparison methodologically
consistent WITH EACH OTHER (all standalone), which is what a direct
side-by-side comparison needs -- it does not claim to exactly reproduce
GA_AUDIT.md's original absolute magnitudes, the same live-database-drift
caveat already noted for windows 1-2.

Frozen snapshot "20260825", GA seed=42, pop=50/gen=50/polish=400 evals --
identical settings to windows 1-2's already-reported numbers.
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
print(f"total active orders: {len(all_ids)}")


def window_data(pos, size):
    ids = set(all_ids[pos:pos + size])
    sub = builder.df_routings[builder.df_routings['production_order_id'].isin(ids)]
    keys = {make_key(r['production_order_id'], r['operation_id']) for _, r in sub.iterrows()}
    preds = {k: v for k, v in preds_all.items() if k in keys}
    return sub, preds


window3_sub, window3_preds = window_data(COMMIT_SIZE * 2, COMMIT_SIZE)
window4_sub, window4_preds = window_data(COMMIT_SIZE * 3, COMMIT_SIZE)

CASES = [
    ("window3_slack", window3_sub, window3_preds),
    ("window4_slack", window4_sub, window4_preds),
]

all_results = {}

for case_name, sub, preds_window in CASES:
    n_orders = sub['production_order_id'].nunique()
    print()
    print("=" * 70)
    print(f"CASE: {case_name} ({n_orders} orders)")
    print("=" * 70)
    if n_orders == 0:
        print("  no orders in this range -- skipping")
        continue

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
        rows[name] = baselines[name]
    rows['GA'] = ga_det

    for name, det in rows.items():
        throughput = det['orders_with_deadline'] / det['makespan_days'] if det['makespan_days'] else 0.0
        det['throughput_orders_per_day'] = throughput

    all_results[case_name] = {'orders': n_orders, 'origin': ORIGIN, 'rows': rows}

print()
print("=" * 70)
print("RAW TABLE -- windows 3-4, frozen snapshot '20260825', GA seed=42")
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

    best_baseline_fit = min(data['rows'][n]['fitness'] for n in ('FIFO', 'EDF', 'CriticalRatio'))
    ga_fit = data['rows']['GA']['fitness']
    margin = 100.0 * (best_baseline_fit - ga_fit) / best_baseline_fit
    print(f"  GA fitness margin vs best baseline: {margin:+.2f}%")

print("\nDONE")
