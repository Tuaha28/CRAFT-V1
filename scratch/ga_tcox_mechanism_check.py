"""
DOMAIN_AWARE_CROSSOVER_PROPOSAL.md Section 7, step 2: direct mechanism
check. Re-runs the crossover-only-vs-mutation-only decomposition
diagnostic (scratch/ga_crossover_vs_mutation_diagnostic.py) with THREE
crossover operators swapped in via toolbox.register("mate", ...) --
'baseline' (today's custom_crossover, unchanged), 'tcox'
(triple_coupled_crossover), and 'shared_cutpoint' (shared_cutpoint_crossover,
the cheaper comparison arm) -- across the same 3 frozen-snapshot cases
used throughout this investigation. Reports crossover-only worse% side by
side for all three operators, per case: the direct test of whether TCOX
(and how much of it the cheaper arm alone) actually reduces the confirmed
83.60-90.51% crossover harm rate from Section 7i, before spending a full
GA run to find out indirectly.

Does NOT modify ga/scheduler.py or ga/operators.py. Mutation
(custom_mutate) is left untouched in every arm -- included here as an
implicit control: if mutation-only harm rates drift wildly between arms
despite mutation being unchanged, that would flag an RNG-consumption
artifact rather than a real crossover effect.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database.ga_snapshot as ga_snapshot
SNAPSHOT_NAME = "20260825"
ga_snapshot.activate_snapshot(SNAPSHOT_NAME)

import random
from datetime import datetime

import numpy as np
import pandas as pd
import torch
from deap import tools, creator

from database.db import get_engine
from database.queries import fetch_concurrent_operator_capacity
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from ga.scheduler import GAScheduler, make_key
from ga.heuristics import baseline_chromosomes
from ga.operators import triple_coupled_crossover, shared_cutpoint_crossover
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR

COMMIT_SIZE = 300
POP_SIZE = 50
GENERATIONS = 50
ELITE_FRAC = 0.04
IMMIGRANT_FRAC = 0.1
CXPB, MUTPB = 0.7, 0.2

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
print(f"window 1: {window1_sub['production_order_id'].nunique()} orders")
print(f"window 2: {window2_sub['production_order_id'].nunique()} orders")

VARIANTS = {
    'baseline': None,  # leave GAScheduler's own registration untouched
    'tcox': triple_coupled_crossover,
    'shared_cutpoint': shared_cutpoint_crossover,
}


def run_case(case_name, sub, preds_window, seed, variant_name):
    print()
    print("=" * 70)
    print(f"CASE: {case_name} / variant: {variant_name} (seed={seed})")
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

    random.seed(seed)
    np.random.seed(seed)

    pop = sched.toolbox.population(n=POP_SIZE)
    try:
        baselines = sched.run_baselines()
        best_name = min(baselines, key=lambda k: baselines[k]['fitness'])
        best_genes = baseline_chromosomes(
            sched.factory, sched.op_details, sched.order_deadlines_minutes, sched.seq_lookup
        )[best_name]
        pop[-1] = creator.Individual(best_genes)
    except Exception as e:
        print(f"heuristic seeding skipped: {e}")

    invalid_ind = [ind for ind in pop if not ind.fitness.valid]
    fitnesses = sched.toolbox.map(sched.toolbox.evaluate, invalid_ind)
    for ind, fit in zip(invalid_ind, fitnesses):
        ind.fitness.values = fit

    elite_count = max(1, int(POP_SIZE * ELITE_FRAC))
    immigrant_count = max(1, int(POP_SIZE * IMMIGRANT_FRAC))

    cx_only_deltas = []
    mut_only_deltas = []
    both_total_deltas = []

    t0 = datetime.now()
    for gen in range(1, GENERATIONS + 1):
        target_size = len(pop)
        elites = [sched.toolbox.clone(ind) for ind in tools.selBest(pop, elite_count)]

        offspring = sched.toolbox.select(pop, target_size - elite_count)
        offspring = [sched.toolbox.clone(ind) for ind in offspring]
        pre_fitness = [ind.fitness.values[0] for ind in offspring]

        n = len(offspring)
        crossed = [False] * n
        mutated = [False] * n

        for i in range(1, n, 2):
            if random.random() < CXPB:
                offspring[i - 1], offspring[i] = sched.toolbox.mate(offspring[i - 1], offspring[i])
                crossed[i - 1] = True
                crossed[i] = True
                del offspring[i - 1].fitness.values
                del offspring[i].fitness.values

        cx_touched_idx = [i for i in range(n) if crossed[i]]
        post_cx_fitness = {}
        if cx_touched_idx:
            fits = sched.toolbox.map(sched.toolbox.evaluate, [offspring[i] for i in cx_touched_idx])
            for i, fit in zip(cx_touched_idx, fits):
                offspring[i].fitness.values = fit
                post_cx_fitness[i] = fit[0]

        for i in range(n):
            if random.random() < MUTPB:
                offspring[i], = sched.toolbox.mutate(offspring[i])
                mutated[i] = True
                del offspring[i].fitness.values

        mut_touched_idx = [i for i in range(n) if not offspring[i].fitness.valid]
        if mut_touched_idx:
            fits = sched.toolbox.map(sched.toolbox.evaluate, [offspring[i] for i in mut_touched_idx])
            for i, fit in zip(mut_touched_idx, fits):
                offspring[i].fitness.values = fit

        for i in range(n):
            if crossed[i] and not mutated[i]:
                cx_only_deltas.append(post_cx_fitness[i] - pre_fitness[i])
            elif mutated[i] and not crossed[i]:
                mut_only_deltas.append(offspring[i].fitness.values[0] - pre_fitness[i])
            elif crossed[i] and mutated[i]:
                both_total_deltas.append(offspring[i].fitness.values[0] - pre_fitness[i])

        n_immigrants = min(immigrant_count, len(offspring))
        if n_immigrants > 0:
            offspring.sort(key=lambda ind: ind.fitness.values[0], reverse=True)
            immigrants = [sched.toolbox.individual() for _ in range(n_immigrants)]
            imm_fitnesses = sched.toolbox.map(sched.toolbox.evaluate, immigrants)
            for ind, fit in zip(immigrants, imm_fitnesses):
                ind.fitness.values = fit
            offspring[:n_immigrants] = immigrants

        pop[:] = elites + offspring

    elapsed = (datetime.now() - t0).total_seconds()

    def summarize(deltas):
        arr = np.array(deltas)
        total = len(arr)
        if total == 0:
            return {'total': 0, 'worse_pct': float('nan')}
        worse = int((arr > 1e-9).sum())
        return {'total': total, 'worse_pct': 100.0 * worse / total}

    r_cx = summarize(cx_only_deltas)
    r_mut = summarize(mut_only_deltas)
    r_both = summarize(both_total_deltas)
    print(f"[{case_name}/{variant_name}] done in {elapsed:.0f}s -- "
          f"crossover-only worse%: {r_cx['worse_pct']:.2f}% (n={r_cx['total']})  "
          f"mutation-only worse%: {r_mut['worse_pct']:.2f}% (n={r_mut['total']})  "
          f"both worse%: {r_both['worse_pct']:.2f}% (n={r_both['total']})")
    return {'case': case_name, 'variant': variant_name, 'elapsed_s': elapsed,
            'cx_only': r_cx, 'mut_only': r_mut, 'both': r_both}


CASES = [
    ('window1_seed42', window1_sub, window1_preds, 42),
    ('window1_seed123', window1_sub, window1_preds, 123),
    ('window2_seed42', window2_sub, window2_preds, 42),
]

results = []
for case_name, sub, preds, seed in CASES:
    for variant_name in VARIANTS:
        results.append(run_case(case_name, sub, preds, seed, variant_name))

print()
print("=" * 70)
print("SUMMARY -- crossover-only worse%, all cases x all 3 operator variants")
print("=" * 70)
print(f"{'case':<20}{'variant':<18}{'cx_n':>7}{'cx_worse%':>12}{'mut_worse%':>13}{'runtime':>9}")
for r in results:
    print(f"{r['case']:<20}{r['variant']:<18}{r['cx_only']['total']:>7}"
          f"{r['cx_only']['worse_pct']:>11.2f}%{r['mut_only']['worse_pct']:>12.2f}%{r['elapsed_s']:>8.0f}s")

print("\n--- Per-case comparison (baseline vs tcox vs shared_cutpoint) ---")
for case_name, *_ in CASES:
    print(f"\n{case_name}:")
    for variant_name in VARIANTS:
        r = next(x for x in results if x['case'] == case_name and x['variant'] == variant_name)
        print(f"  {variant_name:<18} crossover-only worse% = {r['cx_only']['worse_pct']:.2f}%")

print("\nDONE")
