"""Measure offspring quality for seed-42/window-1 without changing GA behaviour."""
import csv
import json
import os
import sys
import time

import numpy as np
import pandas as pd
import torch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SNAPSHOT = "authoritative_20260911"
SEED, WINDOW_ORDERS, POP, GENS = 42, 300, 50, 50
EXPECTED_GEN0, REPRO_TOL, EQUAL_TOL = 1178.204149553035, 1e-6, 1e-9

import database.ga_snapshot as ga_snapshot
ga_snapshot.activate_snapshot(SNAPSHOT)
ORIGIN = ga_snapshot.get_snapshot_origin(SNAPSHOT)

from database.db import get_engine
from database.queries import (fetch_committed_machine_reservations,
                              fetch_concurrent_operator_capacity)
from ga.scheduler import GAScheduler
from graph.feature_schema import load_preprocessing
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR
from utils.run_artifacts import current_run_id, stamp_artifact

RUN_ID, started = current_run_id(), time.time()
print("Building the frozen window-1 context...", flush=True)
builder = FactoryGraphBuilder()
checkpoint = os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")
preprocessing = load_preprocessing(checkpoint)
graph, _ = builder.build_graph(preprocessing=preprocessing)
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())
model = MultiTaskFactoryGNN(
    metadata=graph.metadata(), hidden_channels=GNN_HIDDEN_CHANNELS,
    operation_in_dim=preprocessing['operation_schema']['count'])
model.load_state_dict(torch.load(checkpoint))
model.eval()
with torch.no_grad():
    output = model(graph.x_dict, graph.edge_index_dict, graph['operation'].type_idx)
delay = torch.sigmoid(output['delay']).squeeze(-1)
predictions = {key: {'delay': delay[i].item(), 'priority': output['priority'][i].item()}
               for i, key in enumerate(builder.op_map.keys())}

order_release = (builder.df_routings.groupby('production_order_id')['release_date'].min()
                 .reset_index().sort_values(['release_date', 'production_order_id']))
window_ids = set(order_release['production_order_id'].tolist()[:WINDOW_ORDERS])
window = builder.df_routings[builder.df_routings['production_order_id'].isin(window_ids)]
committed = fetch_committed_machine_reservations(ORIGIN)
scheduler = GAScheduler(
    window, builder.df_machines, df_lines, predictions,
    ownership_snapshot=builder.ownership_snapshot, current_time=ORIGIN,
    present_operator_count=fetch_concurrent_operator_capacity(),
    initial_machine_free=dict(committed),
    scored_order_ids={str(order_id) for order_id in window_ids})

state = {'generation': 0, 'incumbent': None, 'reproduction_checked': False}
rows = []
original_select = scheduler.toolbox.select
original_mate = scheduler.toolbox.mate
original_mutate = scheduler.toolbox.mutate
original_evaluate = scheduler.toolbox.evaluate
original_individual = scheduler.toolbox.individual
original_advance = scheduler._advance_generation


def select(population, count):
    selected = original_select(population, count)
    for item in selected:
        fitness = float(item.fitness.values[0])
        item._source_parent_fitness = fitness
        # A selected individual may carry instrumentation attributes from the
        # preceding generation. Reset lineage at this generation's boundary.
        item._was_crossed = False
        item._was_mutated = False
        item._best_parent_fitness = fitness
        item._mean_parent_fitness = fitness
    return selected


def mate(left, right):
    left_fit = float(getattr(left, '_source_parent_fitness', left.fitness.values[0]))
    right_fit = float(getattr(right, '_source_parent_fitness', right.fitness.values[0]))
    children = original_mate(left, right)
    for child in children:
        child._was_crossed = True
        child._best_parent_fitness = min(left_fit, right_fit)
        child._mean_parent_fitness = (left_fit + right_fit) / 2.0
    return children


def mutate(item):
    source = getattr(item, '_source_parent_fitness', None)
    crossed = bool(getattr(item, '_was_crossed', False))
    best_parent = getattr(item, '_best_parent_fitness', source)
    mean_parent = getattr(item, '_mean_parent_fitness', source)
    mutated, = original_mutate(item)
    mutated._was_mutated = True
    mutated._was_crossed = crossed
    mutated._best_parent_fitness = best_parent
    mutated._mean_parent_fitness = mean_parent
    return mutated,


def individual():
    created = original_individual()
    if state['generation'] > 0:
        created._immigrant_generation = state['generation']
    return created


def evaluate(item):
    result = original_evaluate(item)
    if state['generation'] <= 0:
        return result
    fitness = float(result[0])
    incumbent = float(state['incumbent'])
    immigrant = getattr(item, '_immigrant_generation', None) == state['generation']
    best_parent = None if immigrant else getattr(
        item, '_best_parent_fitness', getattr(item, '_source_parent_fitness', None))
    mean_parent = None if immigrant else getattr(
        item, '_mean_parent_fitness', getattr(item, '_source_parent_fitness', None))
    rows.append({
        'generation': state['generation'],
        'candidate_type': 'immigrant' if immigrant else 'genetic_offspring',
        'crossover': False if immigrant else bool(getattr(item, '_was_crossed', False)),
        'mutation': False if immigrant else bool(getattr(item, '_was_mutated', False)),
        'fitness': fitness, 'incumbent_before': incumbent,
        'gap_pct': 100.0 * (fitness - incumbent) / abs(incumbent),
        'best_parent_fitness': best_parent, 'mean_parent_fitness': mean_parent,
        'minus_best_parent': None if best_parent is None else fitness - float(best_parent),
        'minus_mean_parent': None if mean_parent is None else fitness - float(mean_parent),
    })
    state['incumbent'] = min(state['incumbent'], fitness)
    return result


def advance(population, *args, **kwargs):
    if not state['reproduction_checked']:
        gen0 = min(float(item.fitness.values[0]) for item in population)
        if abs(gen0 - EXPECTED_GEN0) > REPRO_TOL:
            raise RuntimeError(
                f"REPRODUCTION GUARD FAILED: gen0={gen0:.12f}, expected={EXPECTED_GEN0:.12f}")
        state['reproduction_checked'], state['incumbent'] = True, gen0
        print(f"REPRODUCTION PASS: gen-0 best={gen0:.12f}", flush=True)
    state['generation'] += 1
    count = original_advance(population, *args, **kwargs)
    if state['generation'] % 10 == 0:
        print(f"  generation {state['generation']}: records={len(rows)}, "
              f"incumbent={state['incumbent']:.9f}", flush=True)
    return count


scheduler.toolbox.register('select', select)
scheduler.toolbox.register('mate', mate)
scheduler.toolbox.register('mutate', mutate)
scheduler.toolbox.register('evaluate', evaluate)
scheduler.toolbox.register('individual', individual)
scheduler._advance_generation = advance

solutions, logbook = scheduler.optimize(
    pop_size=POP, generations=GENS, n_solutions=1, random_seed=SEED)
log_records = list(logbook)
assert state['reproduction_checked']
assert abs(float(log_records[0]['min']) - EXPECTED_GEN0) <= REPRO_TOL


def stats(items):
    gaps = np.asarray([row['gap_pct'] for row in items], dtype=float)
    diffs = np.asarray([row['fitness'] - row['incumbent_before'] for row in items])
    return {
        'n': len(items),
        'better_than_incumbent_pct': 100.0 * float((diffs < -EQUAL_TOL).mean()),
        'equal_to_incumbent_pct': 100.0 * float((np.abs(diffs) <= EQUAL_TOL).mean()),
        'within_incumbent_pct': {
            str(limit): 100.0 * float((np.abs(gaps) <= limit).mean())
            for limit in (0.1, 0.5, 1.0, 2.0, 5.0)},
        'gap_pct': {
            'mean': float(gaps.mean()), 'p10': float(np.percentile(gaps, 10)),
            'p25': float(np.percentile(gaps, 25)), 'median_p50': float(np.median(gaps)),
            'p75': float(np.percentile(gaps, 75)), 'p90': float(np.percentile(gaps, 90)),
            'best_min': float(gaps.min()), 'worst_max': float(gaps.max())}}


genetic = [row for row in rows if row['candidate_type'] == 'genetic_offspring']
immigrants = [row for row in rows if row['candidate_type'] == 'immigrant']
parent_delta = np.asarray([row['minus_best_parent'] for row in genetic], dtype=float)
mean_parent_delta = np.asarray([row['minus_mean_parent'] for row in genetic], dtype=float)
summary = {
    'run_id': RUN_ID, 'snapshot': SNAPSHOT, 'origin': ORIGIN.isoformat(), 'seed': SEED,
    'window': 1, 'window_orders': WINDOW_ORDERS, 'num_ops': scheduler.num_ops,
    'committed_machines': len(committed), 'mutation_indpb': scheduler.mutation_indpb,
    'configuration': {'population': POP, 'generations': GENS, 'cxpb': 0.7,
                      'mutpb': 0.2, 'elite_frac': 0.04, 'immigrant_frac': 0.1},
    'reproduction': {'expected_gen0': EXPECTED_GEN0,
                     'observed_gen0': float(log_records[0]['min']), 'passed': True},
    'evaluated': {'all_candidates': stats(rows), 'genetic_offspring': stats(genetic),
                  'immigrants': stats(immigrants)},
    'parent_relative': {
        'n': len(genetic),
        'better_than_best_parent_pct': 100.0 * float((parent_delta < -EQUAL_TOL).mean()),
        'equal_to_best_parent_pct': 100.0 * float((np.abs(parent_delta) <= EQUAL_TOL).mean()),
        'better_than_mean_parent_pct': 100.0 * float((mean_parent_delta < -EQUAL_TOL).mean()),
        'minus_best_parent': {
            'mean': float(parent_delta.mean()), 'p10': float(np.percentile(parent_delta, 10)),
            'p25': float(np.percentile(parent_delta, 25)),
            'median_p50': float(np.median(parent_delta)),
            'p75': float(np.percentile(parent_delta, 75)),
            'p90': float(np.percentile(parent_delta, 90)),
            'best_min': float(parent_delta.min()), 'worst_max': float(parent_delta.max())}},
    'generation_bands': {}, 'final_best': float(log_records[-1]['min']),
    'total_seconds': time.time() - started}
for low in (1, 11, 21, 31, 41):
    band = [row for row in rows if low <= row['generation'] <= low + 9]
    summary['generation_bands'][f'{low}-{low + 9}'] = stats(band)

raw_path = os.path.join(OUTPUT_DIR, 'ga_offspring_quality_seed42_w1.csv')
summary_path = os.path.join(OUTPUT_DIR, 'ga_offspring_quality_seed42_w1_summary.json')
with open(raw_path, 'w', newline='', encoding='utf-8') as handle:
    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
with open(summary_path, 'w', encoding='utf-8') as handle:
    json.dump(summary, handle, indent=2)
stamp_artifact(raw_path, run_id=RUN_ID)
stamp_artifact(summary_path, run_id=RUN_ID)
print(json.dumps(summary, indent=2), flush=True)
print(f"RAW={raw_path}\nSUMMARY={summary_path}", flush=True)
