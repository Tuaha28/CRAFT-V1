"""
GA improvement work, Step 1: cheap diagnostic. For one real window (window
1, full production settings), measure what fraction of crossover/mutation
offspring are genuinely FITTER than the individual they started as (not
just different/mutated) -- across every generation of a real 50-generation
run, not one snapshot.

Does NOT modify ga/scheduler.py -- replicates _advance_generation's exact
logic (elites -> select -> clone -> varAnd -> evaluate -> immigrants) with
instrumentation added: fitness is captured on each selected individual
BEFORE varAnd (crossover+mutation) runs, DEAP's own fitness-invalidation
is used to identify exactly which individuals varAnd actually touched
(untouched ones keep valid, unchanged fitness and are correctly excluded
-- they underwent no genetic operation), and post-evaluation fitness is
compared against the pre-operation value for every touched individual.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
from datetime import datetime
from collections import Counter

import numpy as np
import pandas as pd
import torch
from deap import tools, algorithms, base, creator

from database.db import get_engine
from database.queries import fetch_active_orders_and_routings, fetch_machine_state, fetch_concurrent_operator_capacity
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from ga.scheduler import GAScheduler, make_key
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR

COMMIT_SIZE = 300
POP_SIZE = 50
GENERATIONS = 50
SEED = 42
ELITE_FRAC = 0.04
IMMIGRANT_FRAC = 0.1
CXPB, MUTPB = 0.7, 0.2

print("=" * 70)
print("SETUP -- window 1, same as GA_AGENT_POSTMORTEM.md")
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

sched = GAScheduler(sub, df_machines, df_lines, preds_window,
                     current_time=datetime.now(), present_operator_count=capacity)

random.seed(SEED)
np.random.seed(SEED)

pop = sched.toolbox.population(n=POP_SIZE)
try:
    baselines = sched.run_baselines()
    from ga.heuristics import baseline_chromosomes
    best_name = min(baselines, key=lambda k: baselines[k]['fitness'])
    best_genes = baseline_chromosomes(sched.factory, sched.op_details, sched.order_deadlines_minutes, sched.seq_lookup)[best_name]
    pop[-1] = creator.Individual(best_genes)
except Exception as e:
    print(f"heuristic seeding skipped: {e}")

invalid_ind = [ind for ind in pop if not ind.fitness.valid]
fitnesses = sched.toolbox.map(sched.toolbox.evaluate, invalid_ind)
for ind, fit in zip(invalid_ind, fitnesses):
    ind.fitness.values = fit

elite_count = max(1, int(POP_SIZE * ELITE_FRAC))
immigrant_count = max(1, int(POP_SIZE * IMMIGRANT_FRAC))
print(f"pop_size={POP_SIZE} elite_count={elite_count} immigrant_count={immigrant_count} "
      f"generations={GENERATIONS} cxpb={CXPB} mutpb={MUTPB}")

# ---------------------------------------------------------------------------
# Instrumented generation loop -- mirrors ga/scheduler.py::_advance_generation
# exactly, with offspring-vs-parent fitness tracking added.
# ---------------------------------------------------------------------------
improved, neutral, worse = 0, 0, 0
deltas = []
per_gen_stats = []

t0 = datetime.now()
for gen in range(1, GENERATIONS + 1):
    target_size = len(pop)
    elites = [sched.toolbox.clone(ind) for ind in tools.selBest(pop, elite_count)]

    offspring = sched.toolbox.select(pop, target_size - elite_count)
    offspring = [sched.toolbox.clone(ind) for ind in offspring]

    # Capture pre-operation fitness (each clone still holds its selected
    # parent's valid fitness at this point, untouched by varAnd yet).
    pre_fitness = [ind.fitness.values[0] for ind in offspring]

    offspring = algorithms.varAnd(offspring, sched.toolbox, CXPB, MUTPB)

    # DEAP invalidates fitness on any individual varAnd actually modified
    # (crossover recipient or mutation target) -- untouched ones keep their
    # valid, unchanged fitness and are correctly excluded from "offspring"
    # for this measurement (no genetic operation actually happened to them).
    touched_idx = [i for i, ind in enumerate(offspring) if not ind.fitness.valid]

    invalid_ind = [offspring[i] for i in touched_idx]
    fitnesses = sched.toolbox.map(sched.toolbox.evaluate, invalid_ind)
    for i, (ind, fit) in zip(touched_idx, zip(invalid_ind, fitnesses)):
        ind.fitness.values = fit
        delta = fit[0] - pre_fitness[i]  # negative = improved (minimization)
        deltas.append(delta)
        if delta < -1e-9:
            improved += 1
        elif delta > 1e-9:
            worse += 1
        else:
            neutral += 1
    n_evals = len(invalid_ind)

    n_immigrants = min(immigrant_count, len(offspring))
    if n_immigrants > 0:
        offspring.sort(key=lambda ind: ind.fitness.values[0], reverse=True)
        immigrants = [sched.toolbox.individual() for _ in range(n_immigrants)]
        imm_fitnesses = sched.toolbox.map(sched.toolbox.evaluate, immigrants)
        for ind, fit in zip(immigrants, imm_fitnesses):
            ind.fitness.values = fit
        offspring[:n_immigrants] = immigrants
        n_evals += n_immigrants

    pop[:] = elites + offspring
    per_gen_stats.append((gen, len(touched_idx), improved, neutral, worse))
    if gen % 10 == 0 or gen == 1:
        print(f"  gen {gen:>3}: {len(touched_idx)} touched this gen, "
              f"cumulative improved={improved} neutral={neutral} worse={worse}")

elapsed = (datetime.now() - t0).total_seconds()
print(f"\nDone in {elapsed:.0f}s")

total = improved + neutral + worse
print()
print("=" * 70)
print("REAL DISTRIBUTION -- crossover/mutation offspring vs. their pre-operation fitness")
print("=" * 70)
print(f"Total touched (genuinely underwent crossover and/or mutation) individuals: {total}")
print(f"  IMPROVED (fitter than parent):  {improved:>5} ({100.0*improved/total:.2f}%)")
print(f"  NEUTRAL (identical fitness):    {neutral:>5} ({100.0*neutral/total:.2f}%)")
print(f"  WORSE (less fit than parent):   {worse:>5} ({100.0*worse/total:.2f}%)")

deltas_arr = np.array(deltas)
improving_deltas = deltas_arr[deltas_arr < -1e-9]
print(f"\nOf the improving moves specifically:")
print(f"  mean improvement: {-improving_deltas.mean():.3f} fitness points" if len(improving_deltas) else "  none")
print(f"  median improvement: {-np.median(improving_deltas):.3f}" if len(improving_deltas) else "")
print(f"  max single improvement: {-improving_deltas.min():.3f}" if len(improving_deltas) else "")

worsening_deltas = deltas_arr[deltas_arr > 1e-9]
print(f"\nOf the worsening moves specifically:")
print(f"  mean damage: {worsening_deltas.mean():.3f} fitness points" if len(worsening_deltas) else "  none")
print(f"  max single damage: {worsening_deltas.max():.3f}" if len(worsening_deltas) else "")

print("\nDONE")
