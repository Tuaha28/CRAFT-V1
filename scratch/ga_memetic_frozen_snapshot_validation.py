"""
GA improvement work, re-run under a FROZEN order-book snapshot
(database/ga_snapshot.py, snapshot "20260825") instead of the live
database -- fixes the confound Section 7e found: "window 1, seed 123"
run on different days is a different optimization problem, because the
live order book and the datetime.now() origin both drift with real time.
This script freezes both, so every arm below is measured against the
exact same underlying data and the exact same "now".

Re-runs, on equal footing for the first time, all three of the original
memetic-integration comparisons (GA_AGENT_POSTMORTEM.md Sections 7b/7c):
  Test 1: window 1, seed 42   -- the original "+0.515% win"
  Test 2: window 1, seed 123  -- the "-3.66% reversal"
  Test 3: window 2, seed 42   -- the "0.000% no-effect" window-generalization check
...plus the two gentler-injection variants from Section 7e (random-target,
longer-interval) on window 1/seed 123, so that follow-up gets re-measured
under the same frozen conditions too.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must happen before any other project import -- database.db/utils.config
# read DB_NAME from the environment at import time.
import database.ga_snapshot as ga_snapshot
SNAPSHOT_NAME = "20260825"
ga_snapshot.activate_snapshot(SNAPSHOT_NAME)

import pandas as pd
import torch

from database.db import get_engine
from database.queries import fetch_concurrent_operator_capacity
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from ga.scheduler import GAScheduler, make_key
from ga.fitness import evaluate_schedule
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR
from datetime import datetime

COMMIT_SIZE = 300
POP_SIZE = 50
GENERATIONS = 50
POLISH_EVALS = 400
MEMETIC_EVALS = 30

print("=" * 70)
print(f"SETUP -- frozen snapshot '{SNAPSHOT_NAME}', shared across all arms")
print("=" * 70)

ORIGIN = ga_snapshot.get_snapshot_origin(SNAPSHOT_NAME)
print(f"Frozen origin (current_time for every arm): {ORIGIN}")

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
print(f"window 1: {window1_sub['production_order_id'].nunique()} orders")
print(f"window 2: {window2_sub['production_order_id'].nunique()} orders")


def run_arm(name, sub, preds_window, seed, memetic_interval=None, memetic_replace='worst'):
    print()
    print("=" * 70)
    print(f"ARM: {name} (seed={seed}, memetic_interval={memetic_interval}, replace={memetic_replace})")
    print("=" * 70)
    sched = GAScheduler(sub, df_machines, df_lines, preds_window,
                         current_time=ORIGIN, present_operator_count=capacity)
    t0 = datetime.now()
    kwargs = dict(pop_size=POP_SIZE, generations=GENERATIONS, n_solutions=1, random_seed=seed)
    if memetic_interval:
        kwargs.update(memetic_interval=memetic_interval, memetic_evals=MEMETIC_EVALS,
                       memetic_replace=memetic_replace)
    sols, logbook = sched.optimize(**kwargs)
    pre_polish_fitness = sols[0][1]
    polished, pstats = sched.polish(sols[0][0], max_evals=POLISH_EVALS, random_seed=seed)
    elapsed = (datetime.now() - t0).total_seconds()

    _f, det = evaluate_schedule(
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
    result = {
        'name': name, 'seed': seed, 'memetic_interval': memetic_interval,
        'memetic_replace': memetic_replace if memetic_interval else '-',
        'gen0_min': logbook[0]['min'], 'evolution_min': pre_polish_fitness,
        'final_fitness': polished.fitness.values[0], 'on_time_rate': det['on_time_rate'],
        'makespan_days': det['makespan_days'],
        'mean_tardiness_hours': det.get('mean_tardiness_hours', 0), 'elapsed_s': elapsed,
    }
    print(f"[{name}] done in {elapsed:.0f}s")
    print(f"  gen-0 min: {result['gen0_min']:.2f}   evolution-only min: {result['evolution_min']:.2f}")
    print(f"  final (post end-polish): {result['final_fitness']:.2f} "
          f"(end-polish this pass: {pstats['improvement_pct']:+.3f}%)")
    print(f"  on-time: {det['on_time_rate']*100:.1f}%  makespan: {det['makespan_days']:.2f}d  "
          f"mean-tardiness: {result['mean_tardiness_hours']:.1f}h")
    return result


results = []

print("\n" + "#" * 70)
print("# TEST 1: ORIGINAL RESULT -- window 1, seed 42")
print("#" * 70)
results.append(run_arm('1_baseline_w1_seed42', window1_sub, window1_preds, 42))
results.append(run_arm('1_memetic_worst_w1_seed42', window1_sub, window1_preds, 42, 5, 'worst'))

print("\n" + "#" * 70)
print("# TEST 2: CROSS-SEED -- window 1, seed 123 (+ gentler-injection variants)")
print("#" * 70)
results.append(run_arm('2_baseline_w1_seed123', window1_sub, window1_preds, 123))
results.append(run_arm('2_memetic_worst_w1_seed123', window1_sub, window1_preds, 123, 5, 'worst'))
results.append(run_arm('2_memetic_random_w1_seed123', window1_sub, window1_preds, 123, 5, 'random'))
results.append(run_arm('2_memetic_worst_interval15_w1_seed123', window1_sub, window1_preds, 123, 15, 'worst'))

print("\n" + "#" * 70)
print("# TEST 3: WINDOW GENERALIZATION -- window 2, seed 42")
print("#" * 70)
results.append(run_arm('3_baseline_w2_seed42', window2_sub, window2_preds, 42))
results.append(run_arm('3_memetic_worst_w2_seed42', window2_sub, window2_preds, 42, 5, 'worst'))

print()
print("=" * 70)
print("SUMMARY -- all arms, frozen snapshot, one shared origin")
print("=" * 70)
print(f"{'arm':<40}{'evo-only':>12}{'final':>12}{'runtime':>9}")
for r in results:
    print(f"{r['name']:<40}{r['evolution_min']:>12.2f}{r['final_fitness']:>12.2f}{r['elapsed_s']:>8.0f}s")

print("\n--- Head-to-head deltas (vs. that test's own baseline) ---")


def delta(base_name, arm_name):
    base = next(r for r in results if r['name'] == base_name)
    arm = next(r for r in results if r['name'] == arm_name)
    pct = 100.0 * (base['final_fitness'] - arm['final_fitness']) / base['final_fitness']
    print(f"{arm_name:<40} baseline {base['final_fitness']:.2f}  arm {arm['final_fitness']:.2f}  {pct:+.3f}%")


delta('1_baseline_w1_seed42', '1_memetic_worst_w1_seed42')
delta('2_baseline_w1_seed123', '2_memetic_worst_w1_seed123')
delta('2_baseline_w1_seed123', '2_memetic_random_w1_seed123')
delta('2_baseline_w1_seed123', '2_memetic_worst_interval15_w1_seed123')
delta('3_baseline_w2_seed42', '3_memetic_worst_w2_seed42')

print("\nDONE")
