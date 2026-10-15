"""
Follow-up to alns_repair_scoring_window_diagnostic.py: neither of the two
proposed mechanisms (pool-size uniformity, near-zero delay_prob) explained
the window1/0% vs window2/60% split -- both windows came back nearly
identical on those two measures. Testing a third, more direct mechanism:
the MARGIN by which the current rule's pick beats the runner-up on
normalized load. w_risk*delay_prob*norm_marginal_load can only flip the
argmin if it's large enough to close that margin -- so if window1's
current-best line usually has a much bigger load-advantage over the
second-best eligible line than window2's does, that alone would explain
the split without needing non-uniform pool sizes or high delay_prob.

For every one of the same 450 comparable decisions used in both prior
scripts, computes:
  - gap = norm_load(second-best eligible) - norm_load(best eligible)
    (normalized the same way the actual score does: divided by max_load
    across eligible lines for that decision)
  - whether the corrected score actually flipped the pick (same formula
    as alns_repair_scoring_step1_corrected.py)
and reports gap statistics per window, AND the correlation between gap
size and flip/no-flip -- the direct causal check, not just an aggregate
comparison.

Read-only, same snapshot/batches/sampling as the prior two scripts.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
import statistics

import database.ga_snapshot as ga_snapshot
SNAPSHOT_NAME = "20260825"
ga_snapshot.activate_snapshot(SNAPSHOT_NAME)

import pandas as pd
import torch

from database.db import get_engine
from database.queries import fetch_concurrent_operator_capacity
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from ga.scheduler import GAScheduler, make_key
from ga.heuristics import baseline_chromosomes
from ga.alns import DESTROY_OPERATORS, _destroy_high_delay_risk, _destroy_bottleneck_pool, _destroy_random
from ga.local_search import compute_pool_loads
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR

COMMIT_SIZE = 300
REMOVAL_FRACS = [0.02, 0.035, 0.05]
SAMPLES_PER_FRAC = 15
W_LOAD, W_RISK, W_IMPACT = 1.0, 1.0, 0.3

ORIGIN = ga_snapshot.get_snapshot_origin(SNAPSHOT_NAME)
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


def build_and_seed(sub, preds_window):
    sched = GAScheduler(sub, df_machines, df_lines, preds_window,
                         current_time=ORIGIN, present_operator_count=capacity)
    baselines = sched.run_baselines()
    best_name = min(baselines, key=lambda k: baselines[k]['fitness'])
    best_genes = baseline_chromosomes(
        sched.factory, sched.op_details, sched.order_deadlines_minutes, sched.seq_lookup
    )[best_name]
    return sched, best_genes


def pool_size(factory, op_key, ln):
    return max(1, len(factory.machine_ids_per_line_map.get(op_key, {}).get(ln, [])))


def diagnose(name, sched, start):
    print()
    print("=" * 70)
    print(f"{name}")
    print("=" * 70)
    n = sched.num_ops
    rng = random.Random(0)

    gaps_flipped = []
    gaps_not_flipped = []
    all_gaps = []
    n_decisions = 0
    n_flipped = 0

    for frac in REMOVAL_FRACS:
        for i in range(SAMPLES_PER_FRAC * 3):
            op = DESTROY_OPERATORS[i % 3]
            if op != 'high_delay_risk':
                if op == 'bottleneck_pool':
                    _destroy_bottleneck_pool(start, n, sched.factory, sched.op_work, frac, rng)
                else:
                    _destroy_random(start, n, frac, rng)
                continue
            idxs = _destroy_high_delay_risk(start, n, sched.gnn_predictions, frac)
            if not idxs:
                continue

            os_array = start[:n]
            pool_loads = compute_pool_loads(start, n, sched.factory, sched.op_work)

            for idx in idxs[:5]:
                op_key = os_array[idx]
                optype = sched.factory.optype_for_op.get(op_key)
                eligible = sched.factory.eligibility_map.get(op_key, [])
                if len(eligible) < 2:
                    continue

                delay_prob = sched.gnn_predictions.get(op_key, {}).get('delay', 0.0)

                loads_for_type = [pool_loads.get((ln, optype), 0.0) for ln in eligible]
                max_load = max(loads_for_type) if loads_for_type else 1.0
                sorted_loads = sorted(loads_for_type)
                best_load, second_load = sorted_loads[0], sorted_loads[1]
                gap = (second_load - best_load) / max_load if max_load else 0.0

                marginals = {ln: op_work_of(sched, op_key) / pool_size(sched.factory, op_key, ln) for ln in eligible}
                max_marginal = max(marginals.values()) if marginals else 1.0

                def score(ln):
                    load = pool_loads.get((ln, optype), 0.0)
                    norm_load = load / max_load if max_load else 0.0
                    norm_marginal = marginals[ln] / max_marginal if max_marginal else 0.0
                    return (W_LOAD * norm_load) + (W_RISK * delay_prob * norm_marginal)

                current_choice = min(eligible, key=lambda ln: pool_loads.get((ln, optype), 0.0))
                richer_choice = min(eligible, key=score)
                flipped = current_choice != richer_choice

                all_gaps.append(gap)
                n_decisions += 1
                if flipped:
                    n_flipped += 1
                    gaps_flipped.append(gap)
                else:
                    gaps_not_flipped.append(gap)

    print(f"n decisions: {n_decisions}, flipped: {n_flipped} ({100.0*n_flipped/n_decisions:.1f}%)")
    print(f"load-gap (2nd-best minus best, normalized by max_load) -- ALL decisions:")
    print(f"  mean={statistics.mean(all_gaps):.4f}  median={statistics.median(all_gaps):.4f}")
    if gaps_flipped:
        print(f"load-gap where the corrected score FLIPPED the pick (n={len(gaps_flipped)}):")
        print(f"  mean={statistics.mean(gaps_flipped):.4f}  median={statistics.median(gaps_flipped):.4f}  "
              f"max={max(gaps_flipped):.4f}")
    if gaps_not_flipped:
        print(f"load-gap where the corrected score did NOT flip the pick (n={len(gaps_not_flipped)}):")
        print(f"  mean={statistics.mean(gaps_not_flipped):.4f}  median={statistics.median(gaps_not_flipped):.4f}  "
              f"min={min(gaps_not_flipped):.4f}")
    return {'all_gaps': all_gaps, 'gaps_flipped': gaps_flipped, 'gaps_not_flipped': gaps_not_flipped,
            'n_decisions': n_decisions, 'n_flipped': n_flipped}


_op_work_cache = {}
def op_work_of(sched, op_key):
    if id(sched) not in _op_work_cache:
        _op_work_cache[id(sched)] = sched.op_work
    return _op_work_cache[id(sched)].get(op_key, 0.0)


print("Building window 1 scheduler...")
sched_w1, start_w1 = build_and_seed(window1_sub, window1_preds)
print("Building window 2 scheduler...")
sched_w2, start_w2 = build_and_seed(window2_sub, window2_preds)

r1 = diagnose("WINDOW 1", sched_w1, start_w1)
r2 = diagnose("WINDOW 2", sched_w2, start_w2)

print()
print("=" * 70)
print("DIRECT COMPARISON")
print("=" * 70)
print(f"mean load-gap, ALL decisions:     window1={statistics.mean(r1['all_gaps']):.4f}  "
      f"window2={statistics.mean(r2['all_gaps']):.4f}")
print(f"median load-gap, ALL decisions:   window1={statistics.median(r1['all_gaps']):.4f}  "
      f"window2={statistics.median(r2['all_gaps']):.4f}")

print("\nDONE")
