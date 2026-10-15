"""
GA improvement work, follow-up to the memetic validation reversal
(GA_AGENT_POSTMORTEM.md Section 7c): window 1, seed 123 is the specific
real case where 'worst'-replacement, interval=5 memetic integration made
things -3.66% WORSE than the non-memetic baseline (5 genuine improvements
through generation 25, then the population stalled in a worse basin for
the remaining 25 generations). Tests two cheap, targeted variants against
THIS SAME failure case, not a redesign:

  Arm 2: memetic_replace='random' (still interval=5) -- does injecting
         into a random non-elite slot instead of always the worst avoid
         the diversity collapse while still capturing the same early
         real gains?
  Arm 3: memetic_replace='worst', interval=15 (~3 injections across 50
         generations instead of 10) -- does injecting less often avoid
         letting the near-clone family dominate selection?

Arm 1 (baseline, no memetic) and the original 'worst'/interval=5 result
are RE-RUN fresh in this same process too, for a directly comparable,
one-process measurement rather than reusing the earlier run's numbers.
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
SEED = 123  # the specific failure case
MEMETIC_EVALS = 30

print("=" * 70)
print("SETUP -- window 1, seed 123 (the known -3.66% failure case)")
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


def run_arm(name, memetic_interval, memetic_replace='worst'):
    print()
    print("=" * 70)
    print(f"ARM: {name}")
    print("=" * 70)
    sched = GAScheduler(sub, df_machines, df_lines, preds_window,
                         current_time=ORIGIN, present_operator_count=capacity)
    t0 = datetime.now()
    kwargs = dict(pop_size=POP_SIZE, generations=GENERATIONS, n_solutions=1, random_seed=SEED)
    if memetic_interval:
        kwargs.update(memetic_interval=memetic_interval, memetic_evals=MEMETIC_EVALS,
                       memetic_replace=memetic_replace)
    sols, logbook = sched.optimize(**kwargs)
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
    result = {
        'name': name, 'gen0_min': logbook[0]['min'], 'evolution_min': pre_polish_fitness,
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
results.append(run_arm('1_baseline_no_memetic', memetic_interval=None))
results.append(run_arm('2_worst_interval5_ORIGINAL', memetic_interval=5, memetic_replace='worst'))
results.append(run_arm('3_RANDOM_interval5', memetic_interval=5, memetic_replace='random'))
results.append(run_arm('4_worst_interval15_LESS_FREQUENT', memetic_interval=15, memetic_replace='worst'))

print()
print("=" * 70)
print("SUMMARY -- window 1, seed 123")
print("=" * 70)
print(f"{'arm':<32}{'evo-only':>12}{'final':>12}{'vs baseline':>14}{'runtime':>9}")
baseline_final = results[0]['final_fitness']
for r in results:
    delta_pct = 100.0 * (baseline_final - r['final_fitness']) / baseline_final
    print(f"{r['name']:<32}{r['evolution_min']:>12.2f}{r['final_fitness']:>12.2f}"
          f"{delta_pct:>+13.3f}%{r['elapsed_s']:>8.0f}s")

print("\nDONE")
