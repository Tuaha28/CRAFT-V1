"""
GA improvement work, Step 1 re-validation (GA_AGENT_POSTMORTEM.md Section
7g): the domain-aware crossover hold was revisited on the grounds that
Step 1's diagnostic (80.92% of crossover/mutation offspring are WORSE
than their parent, harmful moves ~4x more damaging than beneficial ones
gained) had only ever been measured on ONE instance (window 1, seed 42) --
the same single-instance-measurement risk that just burned two other
results (Section 8). This script re-runs that exact diagnostic across the
SAME three frozen-snapshot test cases used for the memetic re-validation
(Section 7f): window 1/seed 42, window 1/seed 123, window 2/seed 42 --
same snapshot ("20260825"), same origin, same standard as everything else
in this investigation.

Does NOT modify ga/scheduler.py -- replicates _advance_generation's exact
logic (elites -> select -> clone -> varAnd -> evaluate -> immigrants) with
instrumentation added, identical to scratch/ga_offspring_fitness_diagnostic.py
(the original, unfrozen version), just parameterized over 3 (window, seed)
cases and pointed at the frozen snapshot instead of the live DB.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must happen before any other project import.
import database.ga_snapshot as ga_snapshot
SNAPSHOT_NAME = "20260825"
ga_snapshot.activate_snapshot(SNAPSHOT_NAME)

import random
from datetime import datetime

import numpy as np
import pandas as pd
import torch
from deap import tools, algorithms, creator

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

    improved, neutral, worse = 0, 0, 0
    deltas = []

    t0 = datetime.now()
    for gen in range(1, GENERATIONS + 1):
        target_size = len(pop)
        elites = [sched.toolbox.clone(ind) for ind in tools.selBest(pop, elite_count)]

        offspring = sched.toolbox.select(pop, target_size - elite_count)
        offspring = [sched.toolbox.clone(ind) for ind in offspring]
        pre_fitness = [ind.fitness.values[0] for ind in offspring]

        offspring = algorithms.varAnd(offspring, sched.toolbox, CXPB, MUTPB)

        touched_idx = [i for i, ind in enumerate(offspring) if not ind.fitness.valid]
        invalid_ind = [offspring[i] for i in touched_idx]
        fitnesses = sched.toolbox.map(sched.toolbox.evaluate, invalid_ind)
        for i, (ind, fit) in zip(touched_idx, zip(invalid_ind, fitnesses)):
            ind.fitness.values = fit
            delta = fit[0] - pre_fitness[i]
            deltas.append(delta)
            if delta < -1e-9:
                improved += 1
            elif delta > 1e-9:
                worse += 1
            else:
                neutral += 1

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
            print(f"  gen {gen:>3}: cumulative improved={improved} neutral={neutral} worse={worse}")

    elapsed = (datetime.now() - t0).total_seconds()
    total = improved + neutral + worse
    deltas_arr = np.array(deltas)
    improving_deltas = deltas_arr[deltas_arr < -1e-9]
    worsening_deltas = deltas_arr[deltas_arr > 1e-9]

    result = {
        'name': name, 'seed': seed, 'total': total,
        'improved': improved, 'neutral': neutral, 'worse': worse,
        'improved_pct': 100.0 * improved / total, 'worse_pct': 100.0 * worse / total,
        'mean_gain': -improving_deltas.mean() if len(improving_deltas) else 0.0,
        'mean_damage': worsening_deltas.mean() if len(worsening_deltas) else 0.0,
        'elapsed_s': elapsed,
    }
    result['asymmetry'] = (result['mean_damage'] / result['mean_gain']) if result['mean_gain'] else float('nan')

    print(f"\n[{name}] done in {elapsed:.0f}s -- total touched: {total}")
    print(f"  IMPROVED: {improved:>5} ({result['improved_pct']:.2f}%)   "
          f"NEUTRAL: {neutral:>5} ({100.0*neutral/total:.2f}%)   "
          f"WORSE: {worse:>5} ({result['worse_pct']:.2f}%)")
    print(f"  mean gain (improving moves): {result['mean_gain']:.3f}   "
          f"mean damage (worsening moves): {result['mean_damage']:.3f}   "
          f"asymmetry (damage/gain): {result['asymmetry']:.2f}x")
    return result


results = []
results.append(run_case('window1_seed42', window1_sub, window1_preds, 42))
results.append(run_case('window1_seed123', window1_sub, window1_preds, 123))
results.append(run_case('window2_seed42', window2_sub, window2_preds, 42))

print()
print("=" * 70)
print("SUMMARY -- Step 1 diagnostic across all 3 frozen-snapshot cases")
print("=" * 70)
print(f"{'case':<20}{'total':>8}{'improved%':>12}{'worse%':>10}{'mean_gain':>12}{'mean_damage':>13}{'asymmetry':>11}")
for r in results:
    print(f"{r['name']:<20}{r['total']:>8}{r['improved_pct']:>11.2f}%{r['worse_pct']:>9.2f}%"
          f"{r['mean_gain']:>12.3f}{r['mean_damage']:>13.3f}{r['asymmetry']:>10.2f}x")

print("\nDONE")
