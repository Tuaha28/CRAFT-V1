"""
DOMAIN_AWARE_CROSSOVER_PROPOSAL.md Section 7, step 3 (final step): full GA
comparison under the same 3-frozen-snapshot standard used throughout this
investigation (window 1/seed 42, window 1/seed 123, window 2/seed 42,
snapshot "20260825"). Three operators per case: baseline (custom_crossover,
unchanged), TCOX (triple_coupled_crossover), and shared_cutpoint
(shared_cutpoint_crossover) -- swapped in via toolbox.register("mate", ...)
right after GAScheduler construction, same pattern as
scratch/ga_tcox_mechanism_check.py.

Section 10's mechanism check found TCOX's crossover-only harm-rate
reduction is real but landscape-dependent -- substantial on window 2
(9.77pp), negligible on window 1 under both seeds (0.12pp, 1.86pp).
Expectation going in, stated before running: a real final-fitness win on
window 2, little-to-no measurable win on window 1 would be CONSISTENT
with that finding, not a contradiction of it -- this script exists to
check whether that pattern holds at the fitness level too, not to find a
uniform win.

Does NOT modify ga/scheduler.py or ga/operators.py.
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
from ga.operators import triple_coupled_crossover, shared_cutpoint_crossover
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR

COMMIT_SIZE = 300
POP_SIZE = 50
GENERATIONS = 50
POLISH_EVALS = 400

print("=" * 70)
print(f"SETUP -- frozen snapshot '{SNAPSHOT_NAME}', shared across all cases/variants")
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

VARIANTS = {
    'baseline': None,
    'tcox': triple_coupled_crossover,
    'shared_cutpoint': shared_cutpoint_crossover,
}


def run_arm(case_name, sub, preds_window, seed, variant_name):
    print()
    print("=" * 70)
    print(f"ARM: {case_name} / {variant_name} (seed={seed})")
    print("=" * 70)
    sched = GAScheduler(sub, df_machines, df_lines, preds_window,
                         current_time=ORIGIN, present_operator_count=capacity)

    operator_fn = VARIANTS[variant_name]
    if operator_fn is not None:
        sched.toolbox.register(
            "mate", operator_fn, num_ops=sched.num_ops,
            eligibility_map=sched.factory.eligibility_map,
            machines_per_line_map=sched.factory.machines_per_line_map,
            max_mc_cap=40,
            op_work=sched.op_work,
            op_optype=sched.factory.optype_for_op,
            protected_machine_type=sched.protected_machine_type,
        )

    t0 = datetime.now()
    sols, logbook = sched.optimize(pop_size=POP_SIZE, generations=GENERATIONS, n_solutions=1, random_seed=seed)
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
        'case': case_name, 'variant': variant_name, 'seed': seed,
        'gen0_min': logbook[0]['min'], 'evolution_min': pre_polish_fitness,
        'final_fitness': polished.fitness.values[0], 'on_time_rate': det['on_time_rate'],
        'makespan_days': det['makespan_days'],
        'mean_tardiness_hours': det.get('mean_tardiness_hours', 0), 'elapsed_s': elapsed,
    }
    print(f"[{case_name}/{variant_name}] done in {elapsed:.0f}s")
    print(f"  gen-0 min: {result['gen0_min']:.2f}   evolution-only min: {result['evolution_min']:.2f}")
    print(f"  final (post end-polish): {result['final_fitness']:.2f} "
          f"(end-polish this pass: {pstats['improvement_pct']:+.3f}%)")
    print(f"  on-time: {det['on_time_rate']*100:.1f}%  makespan: {det['makespan_days']:.2f}d  "
          f"mean-tardiness: {result['mean_tardiness_hours']:.1f}h")
    return result


CASES = [
    ('window1_seed42', window1_sub, window1_preds, 42),
    ('window1_seed123', window1_sub, window1_preds, 123),
    ('window2_seed42', window2_sub, window2_preds, 42),
]

results = []
for case_name, sub, preds, seed in CASES:
    for variant_name in VARIANTS:
        results.append(run_arm(case_name, sub, preds, seed, variant_name))

print()
print("=" * 70)
print("SUMMARY -- final fitness, all cases x all 3 operators")
print("=" * 70)
print(f"{'case':<20}{'variant':<18}{'evo-only':>12}{'final':>12}{'on-time':>10}{'makespan':>11}{'runtime':>9}")
for r in results:
    print(f"{r['case']:<20}{r['variant']:<18}{r['evolution_min']:>12.2f}{r['final_fitness']:>12.2f}"
          f"{r['on_time_rate']*100:>9.1f}%{r['makespan_days']:>10.2f}d{r['elapsed_s']:>8.0f}s")

print("\n--- Per-case deltas vs. baseline ---")
for case_name, *_ in CASES:
    base = next(r for r in results if r['case'] == case_name and r['variant'] == 'baseline')
    print(f"\n{case_name} (baseline final: {base['final_fitness']:.2f}):")
    for variant_name in ('tcox', 'shared_cutpoint'):
        arm = next(r for r in results if r['case'] == case_name and r['variant'] == variant_name)
        pct = 100.0 * (base['final_fitness'] - arm['final_fitness']) / base['final_fitness']
        print(f"  {variant_name:<18} final={arm['final_fitness']:.2f}  delta={pct:+.3f}%")

print("\nDONE")
