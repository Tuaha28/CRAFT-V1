"""
GA improvement work, pre-proposal decomposition diagnostic (requested
before DOMAIN_AWARE_CROSSOVER_PROPOSAL.md is written): Step 1's original
diagnostic (GA_AGENT_POSTMORTEM.md 7a/7h) measured "touched by varAnd"
offspring vs. their pre-operation fitness, but DEAP's varAnd applies
crossover (cxpb=0.7) THEN mutation (mutpb=0.2) to the same offspring pool
in one combined pass -- an individual "touched" could have been crossed
over, mutated, or both, and the existing diagnostic cannot tell them
apart. The mechanistic hypothesis behind the proposed domain-aware
crossover redesign (OS/LA/MC recombine via two INDEPENDENT, unrelated
cut-point draws -- ga/operators.py custom_crossover line 211 vs.
order_crossover's own line 52 -- reconciled only by a validity-only
repair) specifically implicates CROSSOVER. This script checks that
directly, before any design proposal is written, by replicating varAnd's
exact two-phase loop (same pairing, same draw order) with fitness
captured at three points per individual: pre-operation, post-crossover-
before-mutation, and post-mutation -- so crossover-only, mutation-only,
and combined effects can be measured separately instead of assumed.

Same 3 frozen-snapshot cases as everything else in this investigation
(window 1/seed 42, window 1/seed 123, window 2/seed 42), same snapshot
"20260825". Does NOT modify ga/scheduler.py or ga/operators.py.
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
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR

COMMIT_SIZE = 300
POP_SIZE = 50
GENERATIONS = 50
ELITE_FRAC = 0.04
IMMIGRANT_FRAC = 0.1
CXPB, MUTPB = 0.7, 0.2

print("=" * 70)
print(f"SETUP -- frozen snapshot '{SNAPSHOT_NAME}', shared across all 3 cases")
print("=" * 70)

ORIGIN = ga_snapshot.get_snapshot_origin(SNAPSHOT_NAME)
print(f"Frozen origin (current_time for every case): {ORIGIN}")

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


def run_case(name, sub, preds_window, seed):
    print()
    print("=" * 70)
    print(f"CASE: {name} (seed={seed})")
    print("=" * 70)
    sched = GAScheduler(sub, df_machines, df_lines, preds_window,
                         current_time=ORIGIN, present_operator_count=capacity)

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

    # Buckets, matching this script's docstring exactly.
    cx_only_deltas = []       # crossed, never mutated: post-crossover - pre
    mut_only_deltas = []      # mutated, never crossed: post-mutation - pre
    both_cx_deltas = []       # crossed AND mutated: crossover's own contribution (post-cx - pre)
    both_mut_deltas = []      # crossed AND mutated: mutation's marginal contribution (post-final - post-cx)
    both_total_deltas = []    # crossed AND mutated: combined effect (post-final - pre)

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

        # Phase 1: crossover -- mirrors deap.algorithms.varAnd exactly
        # (same pairing, same draw order, same manual fitness invalidation).
        for i in range(1, n, 2):
            if random.random() < CXPB:
                offspring[i - 1], offspring[i] = sched.toolbox.mate(offspring[i - 1], offspring[i])
                crossed[i - 1] = True
                crossed[i] = True
                del offspring[i - 1].fitness.values
                del offspring[i].fitness.values

        # Extra evaluation pass, not present in the original diagnostic:
        # capture fitness right after crossover, before mutation runs, for
        # every crossed individual -- this is what isolates crossover's
        # effect from mutation's when both touch the same individual.
        cx_touched_idx = [i for i in range(n) if crossed[i]]
        post_cx_fitness = {}
        if cx_touched_idx:
            fits = sched.toolbox.map(sched.toolbox.evaluate, [offspring[i] for i in cx_touched_idx])
            for i, fit in zip(cx_touched_idx, fits):
                offspring[i].fitness.values = fit
                post_cx_fitness[i] = fit[0]

        # Phase 2: mutation -- mirrors varAnd's second loop exactly.
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

        # Classify every individual touched by SOMETHING this generation.
        for i in range(n):
            if crossed[i] and not mutated[i]:
                cx_only_deltas.append(post_cx_fitness[i] - pre_fitness[i])
            elif mutated[i] and not crossed[i]:
                mut_only_deltas.append(offspring[i].fitness.values[0] - pre_fitness[i])
            elif crossed[i] and mutated[i]:
                both_cx_deltas.append(post_cx_fitness[i] - pre_fitness[i])
                both_mut_deltas.append(offspring[i].fitness.values[0] - post_cx_fitness[i])
                both_total_deltas.append(offspring[i].fitness.values[0] - pre_fitness[i])
            # else: untouched, excluded -- no genetic operation happened.

        n_immigrants = min(immigrant_count, len(offspring))
        if n_immigrants > 0:
            offspring.sort(key=lambda ind: ind.fitness.values[0], reverse=True)
            immigrants = [sched.toolbox.individual() for _ in range(n_immigrants)]
            imm_fitnesses = sched.toolbox.map(sched.toolbox.evaluate, immigrants)
            for ind, fit in zip(immigrants, imm_fitnesses):
                ind.fitness.values = fit
            offspring[:n_immigrants] = immigrants

        pop[:] = elites + offspring
        if gen % 10 == 0 or gen == 1:
            print(f"  gen {gen:>3}: cx_only={len(cx_only_deltas)} mut_only={len(mut_only_deltas)} "
                  f"both={len(both_total_deltas)}")

    elapsed = (datetime.now() - t0).total_seconds()

    def summarize(label, deltas):
        arr = np.array(deltas)
        total = len(arr)
        if total == 0:
            print(f"  {label}: no observations")
            return None
        improved = int((arr < -1e-9).sum())
        worse = int((arr > 1e-9).sum())
        neutral = total - improved - worse
        improving = arr[arr < -1e-9]
        worsening = arr[arr > 1e-9]
        mean_gain = -improving.mean() if len(improving) else 0.0
        mean_damage = worsening.mean() if len(worsening) else 0.0
        print(f"  {label}: total={total} improved={improved} ({100.0*improved/total:.2f}%) "
              f"worse={worse} ({100.0*worse/total:.2f}%) neutral={neutral} "
              f"mean_gain={mean_gain:.3f} mean_damage={mean_damage:.3f}")
        return {'total': total, 'improved_pct': 100.0 * improved / total,
                'worse_pct': 100.0 * worse / total, 'mean_gain': mean_gain, 'mean_damage': mean_damage}

    print(f"\n[{name}] done in {elapsed:.0f}s")
    r_cx_only = summarize('CROSSOVER-ONLY (never mutated)', cx_only_deltas)
    r_mut_only = summarize('MUTATION-ONLY (never crossed)', mut_only_deltas)
    r_both_total = summarize('BOTH -- combined effect', both_total_deltas)
    r_both_cx = summarize('BOTH -- crossover\'s own contribution', both_cx_deltas)
    r_both_mut = summarize('BOTH -- mutation\'s marginal contribution (on top of crossover)', both_mut_deltas)

    return {'name': name, 'seed': seed, 'elapsed_s': elapsed,
            'cx_only': r_cx_only, 'mut_only': r_mut_only,
            'both_total': r_both_total, 'both_cx': r_both_cx, 'both_mut': r_both_mut}


results = []
results.append(run_case('window1_seed42', window1_sub, window1_preds, 42))
results.append(run_case('window1_seed123', window1_sub, window1_preds, 123))
results.append(run_case('window2_seed42', window2_sub, window2_preds, 42))

print()
print("=" * 70)
print("SUMMARY -- crossover-only vs mutation-only harm, all 3 frozen-snapshot cases")
print("=" * 70)
print(f"{'case':<20}{'bucket':<28}{'total':>7}{'improved%':>11}{'worse%':>9}{'mean_gain':>11}{'mean_damage':>13}")
for r in results:
    for label, key in [('crossover-only', 'cx_only'), ('mutation-only', 'mut_only'),
                        ('both-combined', 'both_total')]:
        d = r[key]
        if d:
            print(f"{r['name']:<20}{label:<28}{d['total']:>7}{d['improved_pct']:>10.2f}%"
                  f"{d['worse_pct']:>8.2f}%{d['mean_gain']:>11.3f}{d['mean_damage']:>13.3f}")

print("\nDONE")
