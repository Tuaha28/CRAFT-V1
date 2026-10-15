"""
GA improvement work, Step 2: the main experiment. Same window (window 1),
same seed, full production settings, ONE process -- compares:

  BASELINE: current production pipeline exactly (evolution with
            memetic_interval=None, then the existing end-of-run
            sched.polish() pass, max_evals=400).
  MEMETIC:  evolution with memetic_interval=5, memetic_evals=30 (local
            search fires ~10 times across the 50-generation run, injecting
            improved individuals back as parents), THEN the SAME
            end-of-run polish pass on top -- so this measures memetic
            integration as an ADDITION to the existing pipeline, not a
            replacement, matching what production would actually ship.

Real fitness/on-time/makespan compared via evaluate_schedule's own
return_details=True, the same objective and metrics every other
measurement in this project uses.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
from datetime import datetime

import numpy as np
import pandas as pd
import torch

from database.db import get_engine
from database.queries import fetch_active_orders_and_routings, fetch_machine_state, fetch_concurrent_operator_capacity
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
MEMETIC_INTERVAL = 5
MEMETIC_EVALS = 30

print("=" * 70)
print("SETUP -- window 1, same data both arms")
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
window1_ids = set(all_orders_sorted['production_order_id'].head(COMMIT_SIZE).tolist())
sub = builder.df_routings[builder.df_routings['production_order_id'].isin(window1_ids)]
keys = {make_key(r['production_order_id'], r['operation_id']) for _, r in sub.iterrows()}
preds_window = {k: v for k, v in preds_all.items() if k in keys}
ORIGIN = datetime.now()

print(f"MEMETIC_INTERVAL={MEMETIC_INTERVAL}  MEMETIC_EVALS={MEMETIC_EVALS}  "
      f"POLISH_EVALS={POLISH_EVALS}  SEED={SEED}")


def run_arm(name, memetic_interval):
    print()
    print("=" * 70)
    print(f"ARM: {name} (memetic_interval={memetic_interval})")
    print("=" * 70)
    sched = GAScheduler(sub, df_machines, df_lines, preds_window,
                         current_time=ORIGIN, present_operator_count=capacity)
    t0 = datetime.now()
    sols, logbook = sched.optimize(pop_size=POP_SIZE, generations=GENERATIONS, n_solutions=1,
                                    random_seed=SEED, memetic_interval=memetic_interval,
                                    memetic_evals=MEMETIC_EVALS)
    pre_polish_fitness = sols[0][1]
    polished, pstats = sched.polish(sols[0][0], max_evals=POLISH_EVALS, random_seed=SEED)
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
    print(f"[{name}] done in {elapsed:.0f}s")
    print(f"  gen-0 min: {logbook[0]['min']:.2f}   pre-polish (post-evolution) min: {pre_polish_fitness:.2f}")
    print(f"  post end-of-run-polish fitness: {polished.fitness.values[0]:.2f} "
          f"(local search this pass: {pstats['improvement_pct']:+.3f}%)")
    print(f"  on-time: {det['on_time_rate']*100:.1f}%  makespan: {det['makespan_days']:.2f}d  "
          f"mean-tardiness: {det.get('mean_tardiness_hours', 0):.1f}h  "
          f"labour-util: {det.get('labor_utilization_rate', 0)*100:.2f}%")
    return {
        'gen0_min': logbook[0]['min'],
        'evolution_min': pre_polish_fitness,
        'final_fitness': polished.fitness.values[0],
        'on_time_rate': det['on_time_rate'],
        'makespan_days': det['makespan_days'],
        'mean_tardiness_hours': det.get('mean_tardiness_hours', 0),
        'labor_utilization_rate': det.get('labor_utilization_rate', 0),
        'elapsed_s': elapsed,
    }


baseline = run_arm('BASELINE (end-only polish)', None)
memetic = run_arm('MEMETIC (periodic + end polish)', MEMETIC_INTERVAL)

print()
print("=" * 70)
print("COMPARISON -- same window, same seed")
print("=" * 70)
print(f"{'metric':<28}{'baseline':>14}{'memetic':>14}{'delta':>14}")
for key, label, fmt in [
    ('evolution_min', 'evolution-only fitness', '{:.2f}'),
    ('final_fitness', 'final fitness (post-polish)', '{:.2f}'),
    ('on_time_rate', 'on-time rate', '{:.4f}'),
    ('makespan_days', 'makespan (days)', '{:.3f}'),
    ('mean_tardiness_hours', 'mean tardiness (h)', '{:.2f}'),
    ('labor_utilization_rate', 'labour utilisation', '{:.4f}'),
    ('elapsed_s', 'runtime (s)', '{:.0f}'),
]:
    b, m = baseline[key], memetic[key]
    delta = m - b
    print(f"{label:<28}{fmt.format(b):>14}{fmt.format(m):>14}{fmt.format(delta):>+14}")

fitness_improvement_pct = 100.0 * (baseline['final_fitness'] - memetic['final_fitness']) / baseline['final_fitness']
print(f"\nFinal fitness improvement from memetic integration: {fitness_improvement_pct:+.3f}%")

print("\nDONE")
