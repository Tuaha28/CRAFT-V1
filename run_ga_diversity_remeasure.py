"""
Focused re-measurement of the immigrant/parent diversity split.

WHY THIS EXISTS. run_ga_multiseed_search_audit.py identified random
immigrants by id(individual), captured at toolbox.individual() time. CPython
REUSES id() after garbage collection, so a dead immigrant's address gets
reassigned to a later evolved individual and is counted as an immigrant. The
tell was arithmetic: it reported 7.5-16.0 immigrants per population when
immigrant_frac (0.1) x pop_size (50) creates exactly 5 per generation and the
snapshot is taken immediately afterwards. So
immigrant_share_of_spread and mean_evolved_distance_to_incumbent from that
run are unusable.

Immigrants are now tagged with the GENERATION at which they were created.
A clone of an immigrant carries the older generation number, so it is
correctly treated as an evolved descendant rather than a fresh immigrant --
which an attribute-copying deepcopy would otherwise get wrong too.

A hard assertion enforces the arithmetic bound, so this class of defect
cannot pass silently again.

Scope: ONE seed, ONE window. The multi-seed replication result (which cells
improved) does not depend on immigrant identification and is unaffected; only
this sub-measurement needed redoing, so this re-runs the cheapest slice that
answers it rather than the whole 129-minute sweep.
"""
import json
import os
import random
import sys
import time

import pandas as pd
import torch

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

SNAPSHOT_NAME = "authoritative_20260911"
SEED = 42
WINDOW_ORDERS = 300          # window 1 only
POP, GENS = 50, 50
DISTANCE_POSITIONS = 400
DISTANCE_PAIRS = 40

import database.ga_snapshot as ga_snapshot
ga_snapshot.activate_snapshot(SNAPSHOT_NAME)
ORIGIN = ga_snapshot.get_snapshot_origin(SNAPSHOT_NAME)

from database.db import get_engine
from database.queries import fetch_concurrent_operator_capacity
from graph.graph_builder import FactoryGraphBuilder
from graph.feature_schema import load_preprocessing
from models.gnn import MultiTaskFactoryGNN, prior_corrected_delay_probabilities
from ga.scheduler import GAScheduler
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR
from utils.run_artifacts import current_run_id, stamp_artifact

RUN_ID = current_run_id()
_T0 = time.time()

print("building context...", flush=True)
builder = FactoryGraphBuilder()
ckpt = os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")
pre = load_preprocessing(ckpt)
graph, _ = builder.build_graph(preprocessing=pre)
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())
model = MultiTaskFactoryGNN(metadata=graph.metadata(), hidden_channels=GNN_HIDDEN_CHANNELS,
                            operation_in_dim=pre['operation_schema']['count'])
model.load_state_dict(torch.load(ckpt))
model.eval()
with torch.no_grad():
    out = model(graph.x_dict, graph.edge_index_dict, graph['operation'].type_idx)
delay = prior_corrected_delay_probabilities(
    out['delay'], pre['delay_pos_weight']
).squeeze(-1)
preds = {k: {'delay': delay[i].item(), 'priority': out['priority'][i].item()}
         for i, k in enumerate(builder.op_map.keys())}

# Window 1 exactly as the rolling horizon builds it: earliest-released orders.
order_release = (builder.df_routings.groupby('production_order_id')['release_date'].min()
                 .reset_index().sort_values(['release_date', 'production_order_id']))
w1_orders = set(order_release['production_order_id'].tolist()[:WINDOW_ORDERS])
sub = builder.df_routings[builder.df_routings['production_order_id'].isin(w1_orders)]

from database.queries import fetch_committed_machine_reservations
committed = fetch_committed_machine_reservations(ORIGIN)
capacity = fetch_concurrent_operator_capacity()
sched = GAScheduler(sub, builder.df_machines, df_lines, preds,
                    ownership_snapshot=builder.ownership_snapshot,
                    current_time=ORIGIN, present_operator_count=capacity,
                    initial_machine_free=dict(committed),
                    scored_order_ids={str(o) for o in w1_orders})
print(f"  num_ops={sched.num_ops}  indpb={sched.mutation_indpb:.5f}  "
      f"committed_machines={len(committed)}", flush=True)

IMMIGRANT_COUNT = int(0.1 * POP)      # matches optimize()'s immigrant_frac default
ELITE_COUNT = int(0.04 * POP)

dist_rng = random.Random(20260912)
total_genes = 3 * sched.num_ops
positions = sorted(dist_rng.sample(range(total_genes), min(DISTANCE_POSITIONS, total_genes)))


def distance(a, b):
    return sum(1 for p in positions if a[p] != b[p]) / len(positions)


state = {'gen': 0, 'pool': None}
records = []
orig_select = sched.toolbox.select
orig_individual = sched.toolbox.individual


def select(pop, k):
    pool = orig_select(pop, k)
    state['pool'] = pool
    return pool


def individual():
    ind = orig_individual()
    # GENERATION-TAGGED, not id()-tagged. A clone made in a later generation
    # keeps this older number and is therefore correctly counted as evolved.
    ind._imm_gen = state['gen'] + 1
    return ind


sched.toolbox.register('select', select)
sched.toolbox.register('individual', individual)

orig_advance = sched._advance_generation


def advance(pop, *a, **k):
    n = orig_advance(pop, *a, **k)
    state['gen'] += 1
    gen = state['gen']
    pool = state['pool'] or []
    is_imm = [getattr(ind, '_imm_gen', None) == gen for ind in pop]
    n_imm = sum(is_imm)
    # THE ASSERTION the previous instrumentation lacked. Exactly this many
    # immigrants are created per generation and the snapshot is immediate, so
    # any larger count is an identification defect, not a finding.
    assert n_imm <= IMMIGRANT_COUNT, (
        f'gen {gen}: {n_imm} immigrants identified but at most '
        f'{IMMIGRANT_COUNT} are created -- identification is broken')
    fits = [ind.fitness.values[0] for ind in pop]
    evolved = [f for f, m in zip(fits, is_imm) if not m]
    imm = [f for f, m in zip(fits, is_imm) if m]
    best_ind = min(pop, key=lambda ind: ind.fitness.values[0])
    evolved_d = [distance(ind, best_ind) for ind, m in zip(pop, is_imm) if not m]
    imm_d = [distance(ind, best_ind) for ind, m in zip(pop, is_imm) if m]
    pool_genes = [tuple(x) for x in pool]
    pair = []
    if len(pool) >= 2:
        for _ in range(min(DISTANCE_PAIRS, len(pool) * (len(pool) - 1) // 2)):
            i, j = dist_rng.sample(range(len(pool)), 2)
            pair.append(distance(pool[i], pool[j]))
    spread_all = (sum(fits) / len(fits)) - min(fits)
    spread_evolved = ((sum(evolved) / len(evolved)) - min(evolved)) if evolved else 0.0
    records.append({
        'gen': gen, 'n_immigrants': n_imm,
        'best': min(fits), 'avg_all': sum(fits) / len(fits),
        'avg_evolved': (sum(evolved) / len(evolved)) if evolved else None,
        'avg_immigrants': (sum(imm) / len(imm)) if imm else None,
        'spread_all': spread_all, 'spread_evolved': spread_evolved,
        'immigrant_share_of_spread': (1.0 - spread_evolved / spread_all) if spread_all > 0 else None,
        'distinct_parents': len(set(pool_genes)), 'pool_size': len(pool),
        'mean_pairwise_parent_distance': (sum(pair) / len(pair)) if pair else None,
        'mean_evolved_distance_to_incumbent': (sum(evolved_d) / len(evolved_d)) if evolved_d else None,
        'mean_immigrant_distance_to_incumbent': (sum(imm_d) / len(imm_d)) if imm_d else None,
    })
    return n


sched._advance_generation = advance
print(f"running GA: seed={SEED} pop={POP} gens={GENS}", flush=True)
sols, logbook = sched.optimize(pop_size=POP, generations=GENS, n_solutions=1,
                               random_seed=SEED)
recs = list(logbook)
out = {'run_id': RUN_ID, 'snapshot': SNAPSHOT_NAME, 'origin': ORIGIN.isoformat(),
       'seed': SEED, 'window_orders': WINDOW_ORDERS, 'num_ops': sched.num_ops,
       'mutation_indpb': sched.mutation_indpb,
       'immigrant_count': IMMIGRANT_COUNT, 'elite_count': ELITE_COUNT,
       'gen0_best': recs[0]['min'], 'final_best': recs[-1]['min'],
       'generations': records, 'total_seconds': time.time() - _T0}
path = os.path.join(OUTPUT_DIR, "ga_diversity_remeasure.json")
with open(path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, default=str)
stamp_artifact(path, run_id=RUN_ID)
print(f"DONE in {out['total_seconds']/60:.1f} min -> {path} (run_id={RUN_ID})")
