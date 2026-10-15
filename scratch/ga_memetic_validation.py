"""
GA improvement work, validation pass: cross-seed check (window 1, seed 123
vs. the original seed 42) and window-generalization check (window 2,
standalone/idle-floor, same convention window 1 was tested under, seed
42) for the memetic integration result (GA_AGENT_POSTMORTEM.md Section 7).
One variable changed at a time, matching this project's standing
discipline (same as the split-encoder's two-seed cross-check).

Test A: window 1, seed=123 -- is the +0.515% a seed-specific fluke?
Test B: window 2, seed=42  -- does the win generalize to a slacker window
        (52% on-time under the OLD pre-release-date-fix measurement,
        genuinely different demand density from window 1's 11.3%),
        tested standalone/idle-floor exactly like window 1 was, so the
        only real difference between the two windows' experiments is
        demand characteristics, not carried-forward machine state.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime

import pandas as pd
import torch

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
MEMETIC_INTERVAL = 5
MEMETIC_EVALS = 30

print("=" * 70)
print("SETUP -- shared across all arms")
print("=" * 70)

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

ORIGIN = datetime.now()


def run_arm(name, sub, preds_window, seed, memetic_interval):
    print()
    print("=" * 70)
    print(f"ARM: {name} (seed={seed}, memetic_interval={memetic_interval})")
    print("=" * 70)
    sched = GAScheduler(sub, df_machines, df_lines, preds_window,
                         current_time=ORIGIN, present_operator_count=capacity)
    t0 = datetime.now()
    sols, logbook = sched.optimize(pop_size=POP_SIZE, generations=GENERATIONS, n_solutions=1,
                                    random_seed=seed, memetic_interval=memetic_interval,
                                    memetic_evals=MEMETIC_EVALS)
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
        'gen0_min': logbook[0]['min'],
        'evolution_min': pre_polish_fitness,
        'final_fitness': polished.fitness.values[0],
        'on_time_rate': det['on_time_rate'],
        'makespan_days': det['makespan_days'],
        'mean_tardiness_hours': det.get('mean_tardiness_hours', 0),
        'labor_utilization_rate': det.get('labor_utilization_rate', 0),
        'elapsed_s': elapsed,
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
print("# TEST A: CROSS-SEED VALIDATION -- window 1, seed 123")
print("#" * 70)
results.append(run_arm('A_baseline_w1_seed123', window1_sub, window1_preds, 123, None))
results.append(run_arm('A_memetic_w1_seed123', window1_sub, window1_preds, 123, MEMETIC_INTERVAL))

print("\n" + "#" * 70)
print("# TEST B: WINDOW GENERALIZATION -- window 2, seed 42")
print("#" * 70)
results.append(run_arm('B_baseline_w2_seed42', window2_sub, window2_preds, 42, None))
results.append(run_arm('B_memetic_w2_seed42', window2_sub, window2_preds, 42, MEMETIC_INTERVAL))

print()
print("=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"{'arm':<26}{'evo-only':>12}{'final':>12}{'on-time':>10}{'makespan':>11}{'mean-tard':>11}{'runtime':>9}")
for r in results:
    print(f"{r['name']:<26}{r['evolution_min']:>12.2f}{r['final_fitness']:>12.2f}"
          f"{r['on_time_rate']*100:>9.1f}%{r['makespan_days']:>10.2f}d{r['mean_tardiness_hours']:>10.1f}h"
          f"{r['elapsed_s']:>8.0f}s")

print("\n--- TEST A comparison (window 1, seed 123) ---")
a_base = next(r for r in results if r['name'] == 'A_baseline_w1_seed123')
a_mem = next(r for r in results if r['name'] == 'A_memetic_w1_seed123')
a_delta_pct = 100.0 * (a_base['final_fitness'] - a_mem['final_fitness']) / a_base['final_fitness']
print(f"baseline final: {a_base['final_fitness']:.2f}  memetic final: {a_mem['final_fitness']:.2f}  "
      f"improvement: {a_delta_pct:+.3f}%")

print("\n--- TEST B comparison (window 2, seed 42) ---")
b_base = next(r for r in results if r['name'] == 'B_baseline_w2_seed42')
b_mem = next(r for r in results if r['name'] == 'B_memetic_w2_seed42')
b_delta_pct = 100.0 * (b_base['final_fitness'] - b_mem['final_fitness']) / b_base['final_fitness']
print(f"baseline final: {b_base['final_fitness']:.2f}  memetic final: {b_mem['final_fitness']:.2f}  "
      f"improvement: {b_delta_pct:+.3f}%")

print("\nDONE")
