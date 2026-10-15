"""
Multi-seed replication of the "generational search contributes nothing"
finding, plus a diversity audit of the PRODUCTIVE part of the population.

WHY. The finding currently rests on one seed, one snapshot, one run. If it
goes in a paper as a central claim it has to hold across seeds. And Astra's
objection to the diversity evidence is fair and untested:

    "Large population spread does not rule out premature convergence. Poor
     immigrants can maintain a large fitness spread while selected parents
     and useful offspring concentrate around one basin."

That objection has a concrete mechanism here. _advance_generation replaces
the WORST offspring with `immigrant_count` fresh random individuals every
generation, and stats.compile(pop) includes them, so `avg - min` is inflated
by construction. This script separates the two explanations:

  * "the neighbourhood contains nothing better"  -> STRUCTURAL
  * "selection concentrated and perturbation is too small to escape, while
     immigrants inflate the spread"              -> TUNING

The distinguishing measurements are taken on SELECTED PARENTS and on
offspring relative to the incumbent, with immigrants identified and
excluded, rather than across the whole population.

Holds everything except the seed: same snapshot, same origin, same budget
(pop 50, gens 50, 400-eval polish), same commit, GA arm only, no baselines,
no evaluator change, no GA parameter change.

Instrumentation is applied by wrapping the toolbox in THIS script -- no
production code is modified, so the runs measure the shipped search.
"""
import copy
import json
import os
import random
import sys
import time
import uuid

import pandas as pd
import torch

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

SNAPSHOT_NAME = "authoritative_20260911"

# Seeds: 42 (the original, re-run to confirm determinism) plus the four
# consecutive integers after it. Chosen and fixed BEFORE any result was seen;
# no seed is added or dropped afterwards.
SEEDS = (42, 43, 44, 45, 46)

POP, GENS, POLISH_EVALS = 50, 50, 400

# Distance sampling. A full pairwise Hamming over 3 x num_ops genes for 48
# parents every generation is ~400M element comparisons per window, so
# positions and pairs are subsampled with a FIXED rng -- documented
# approximation, identical across seeds so cross-seed comparison is fair.
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
from ga.rolling_horizon import run_rolling_horizon
from ga.scheduler import GAScheduler
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR, PLANNING_HORIZON_ORDERS
from utils.run_artifacts import current_run_id, stamp_artifact

RUN_ID = current_run_id()
_T0 = time.time()


def banner(t):
    print("\n" + "=" * 78 + "\n" + t + "\n" + "=" * 78, flush=True)


# ---------------------------------------------------------------------------
# Instrumentation: wrap the toolbox, do not touch production code
# ---------------------------------------------------------------------------

_COLLECT = {'windows': []}


def _install_instrumentation(sched, window):
    """
    Records, per generation:
      * distinct individuals entering the mating pool (tournament output)
      * mean pairwise genotype distance among SELECTED PARENTS
      * which individuals are random immigrants (identified at creation)
      * fitness spread with and without immigrants
      * offspring genotype distance to the incumbent best

    Distinguishes "nothing better nearby" from "selection concentrated and
    immigrants inflate the spread".
    """
    rec = {'window': window, 'num_ops': sched.num_ops,
           'mutation_indpb': sched.mutation_indpb, 'generations': []}
    _COLLECT['windows'].append(rec)

    dist_rng = random.Random(20260912)          # fixed, seed-independent
    total_genes = 3 * sched.num_ops
    positions = sorted(dist_rng.sample(range(total_genes),
                                       min(DISTANCE_POSITIONS, total_genes)))

    orig_select = sched.toolbox.select
    orig_individual = sched.toolbox.individual
    state = {'pool': None, 'immigrant_ids': set()}

    def distance(a, b):
        diff = sum(1 for p in positions if a[p] != b[p])
        return diff / len(positions)

    def select(pop, k):
        pool = orig_select(pop, k)
        state['pool'] = pool
        return pool

    def individual():
        ind = orig_individual()
        # Immigrants are the ONLY consumers of toolbox.individual() once the
        # seeded population exists, so creation identity marks them exactly.
        state['immigrant_ids'].add(id(ind))
        return ind

    sched.toolbox.register('select', select)
    sched.toolbox.register('individual', individual)

    def snapshot_generation(pop, gen):
        pool = state['pool'] or []
        pool_genes = [tuple(ind) for ind in pool]
        distinct_parents = len(set(pool_genes))
        pair_dists = []
        if len(pool) >= 2:
            for _ in range(min(DISTANCE_PAIRS, len(pool) * (len(pool) - 1) // 2)):
                i, j = dist_rng.sample(range(len(pool)), 2)
                pair_dists.append(distance(pool[i], pool[j]))
        fits = [ind.fitness.values[0] for ind in pop]
        imm_mask = [id(ind) in state['immigrant_ids'] for ind in pop]
        evolved = [f for f, m in zip(fits, imm_mask) if not m]
        immigrants = [f for f, m in zip(fits, imm_mask) if m]
        best = min(pop, key=lambda ind: ind.fitness.values[0])
        incumbent_dist = [distance(ind, best) for ind, m in zip(pop, imm_mask) if not m]
        spread_all = (sum(fits) / len(fits)) - min(fits) if fits else 0.0
        spread_evolved = ((sum(evolved) / len(evolved)) - min(evolved)) if evolved else 0.0
        rec['generations'].append({
            'gen': gen,
            'distinct_parents': distinct_parents,
            'pool_size': len(pool),
            'mean_pairwise_parent_distance': (
                sum(pair_dists) / len(pair_dists)) if pair_dists else None,
            'n_immigrants_in_pop': sum(imm_mask),
            'best': min(fits) if fits else None,
            'avg_all': (sum(fits) / len(fits)) if fits else None,
            'avg_evolved': (sum(evolved) / len(evolved)) if evolved else None,
            'avg_immigrants': (sum(immigrants) / len(immigrants)) if immigrants else None,
            'spread_all': spread_all,
            'spread_evolved': spread_evolved,
            'immigrant_share_of_spread': (
                1.0 - spread_evolved / spread_all) if spread_all > 0 else None,
            'mean_evolved_distance_to_incumbent': (
                sum(incumbent_dist) / len(incumbent_dist)) if incumbent_dist else None,
            'max_evolved_distance_to_incumbent': max(incumbent_dist) if incumbent_dist else None,
        })
    return snapshot_generation


_ORIG_OPTIMIZE = GAScheduler.optimize


def instrumented_optimize(self, *args, **kwargs):
    window = len(_COLLECT['windows']) + 1
    snapshot = _install_instrumentation(self, window)
    orig_advance = self._advance_generation
    counter = {'gen': 0}

    def advance(pop, *a, **k):
        n = orig_advance(pop, *a, **k)
        counter['gen'] += 1
        snapshot(pop, counter['gen'])
        return n

    self._advance_generation = advance
    try:
        return _ORIG_OPTIMIZE(self, *args, **kwargs)
    finally:
        self._advance_generation = orig_advance


GAScheduler.optimize = instrumented_optimize


# ---------------------------------------------------------------------------
banner("CONTEXT (built once; identical for every seed)")
builder = FactoryGraphBuilder()
ckpt = os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")
preprocessing = load_preprocessing(ckpt)
graph, _ = builder.build_graph(preprocessing=preprocessing)
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())
model = MultiTaskFactoryGNN(metadata=graph.metadata(), hidden_channels=GNN_HIDDEN_CHANNELS,
                            operation_in_dim=preprocessing['operation_schema']['count'])
model.load_state_dict(torch.load(ckpt))
model.eval()
with torch.no_grad():
    out = model(graph.x_dict, graph.edge_index_dict, graph['operation'].type_idx)
delay = prior_corrected_delay_probabilities(
    out['delay'], preprocessing['delay_pos_weight']
).squeeze(-1)
preds_all = {k: {'delay': delay[i].item(), 'priority': out['priority'][i].item()}
             for i, k in enumerate(builder.op_map.keys())}
capacity = fetch_concurrent_operator_capacity()
commit_size = PLANNING_HORIZON_ORDERS or builder.df_routings['production_order_id'].nunique()
print(f"  run_id={RUN_ID}  origin={ORIGIN}  commit_size={commit_size}  operators={capacity}")
print(f"  seeds={SEEDS}  pop={POP} gens={GENS} polish={POLISH_EVALS}")

results = {}
for seed in SEEDS:
    banner(f"SEED {seed} -- GA arm only, export=False")
    _COLLECT['windows'] = []
    t0 = time.time()
    res = run_rolling_horizon(
        builder.df_routings, builder.df_machines, df_lines, preds_all, capacity, ORIGIN,
        commit_size=commit_size, pop_size=POP, generations=GENS,
        polish_evals=POLISH_EVALS, seed=seed, export=False, log_fn=print,
        ownership_snapshot=builder.ownership_snapshot)
    per_window = []
    for lb, entry in zip(res.get('logbooks', []), res['windows']):
        recs = lb['records']
        gen0, final = recs[0], recs[-1]
        diag = next((w for w in _COLLECT['windows'] if w['window'] == lb['window']), None)
        per_window.append({
            'window': lb['window'],
            'num_ops': entry.get('committed_operations'),
            'seed_best_gen0': gen0['min'],
            'after_generations': final['min'],
            'delta_generations': gen0['min'] - final['min'],
            'committed_after_polish': entry['fitness'],
            'delta_polish': final['min'] - entry['fitness'],
            'nevals_after_gen0': sum(r['nevals'] for r in recs[1:]),
            'final_spread_all': final['avg'] - final['min'],
            'diagnostics': diag,
        })
    results[seed] = {'per_window': per_window, 'aggregate': res['aggregate'],
                     'runtime_min': (time.time() - t0) / 60.0}
    for w in per_window:
        print(f"  w{w['window']}: gen0={w['seed_best_gen0']:.6f} "
              f"after_gens={w['after_generations']:.6f} ({w['delta_generations']:+.6f}) "
              f"polish={w['committed_after_polish']:.6f} ({w['delta_polish']:+.6f}) "
              f"nevals={w['nevals_after_gen0']}")
    print(f"  seed {seed} done in {results[seed]['runtime_min']:.1f} min")

out = {'run_id': RUN_ID, 'snapshot': SNAPSHOT_NAME, 'origin': ORIGIN.isoformat(),
       'seeds': list(SEEDS), 'pop_size': POP, 'generations': GENS,
       'polish_evals': POLISH_EVALS, 'commit_size': commit_size,
       'distance_positions': DISTANCE_POSITIONS, 'distance_pairs': DISTANCE_PAIRS,
       'results': {str(k): v for k, v in results.items()},
       'total_seconds': time.time() - _T0}
path = os.path.join(OUTPUT_DIR, "ga_multiseed_search_audit.json")
with open(path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, default=str)
stamp_artifact(path, run_id=RUN_ID)
banner("DONE")
print(f"total runtime: {out['total_seconds']/60:.1f} min")
print(f"written: {path}  (run_id={RUN_ID})")
