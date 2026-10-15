"""
Diagnose the window1 (0%) vs window2 (60%) split found in
ALNS_REPAIR_SCORING_PROPOSAL.md Section 11's corrected-formula Step 1
re-run. Two candidate mechanisms, checked directly on the SAME 450
comparable decisions that test used (same snapshot, same destroy batches,
same sampling, same eligible>=2 filter):

  (1) POOL-SIZE UNIFORMITY: if window1's eligible lines have near-identical
      machine counts for each operation's type, marginal_load(line) =
      op_work / pool_size(line) is near-constant across candidates too --
      collapsing norm_marginal_load(line) back toward a constant, which
      would recreate the ORIGINAL degeneracy for a different reason
      (structural -- the fix silently doesn't work for uniform-pool cases).

  (2) DELAY_PROB NEAR ZERO: if window1's high-delay-risk-destroyed
      operations mostly have low delay_prob, then w_risk*delay_prob*(...)
      is negligible regardless of how much norm_marginal_load varies --
      an honest property of that window's risk profile, not a mechanism
      failure.

Reports real numbers for both windows, same standard as the original
formula's structural diagnosis (ALNS_REPAIR_SCORING_PROPOSAL.md Section 7).
Read-only: does not modify ga/alns.py, ga/local_search.py, or ga/operators.py.
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

    delay_probs = []
    pool_size_cvs = []          # coefficient of variation of pool_size across eligible lines, per decision
    frac_identical_pool_size = 0  # count of decisions where ALL eligible lines have the exact same pool_size
    n_decisions = 0

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

            for idx in idxs[:5]:
                op_key = os_array[idx]
                optype = sched.factory.optype_for_op.get(op_key)
                eligible = sched.factory.eligibility_map.get(op_key, [])
                if len(eligible) < 2:
                    continue  # same filter the Step 1 test used -- must match its 450 decisions

                delay_prob = sched.gnn_predictions.get(op_key, {}).get('delay', 0.0)
                delay_probs.append(delay_prob)

                sizes = [pool_size(sched.factory, op_key, ln) for ln in eligible]
                mean_size = statistics.mean(sizes)
                std_size = statistics.pstdev(sizes)
                cv = (std_size / mean_size) if mean_size else 0.0
                pool_size_cvs.append(cv)
                if len(set(sizes)) == 1:
                    frac_identical_pool_size += 1
                n_decisions += 1

    print(f"n decisions (eligible>=2, high_delay_risk batches): {n_decisions}")
    print()
    print("-- (2) delay_prob distribution --")
    print(f"  mean={statistics.mean(delay_probs):.4f}  median={statistics.median(delay_probs):.4f}  "
          f"min={min(delay_probs):.4f}  max={max(delay_probs):.4f}")
    near_zero = sum(1 for d in delay_probs if d < 0.05)
    below_01 = sum(1 for d in delay_probs if d < 0.10)
    print(f"  fraction with delay_prob < 0.05: {near_zero}/{n_decisions} ({100.0*near_zero/n_decisions:.1f}%)")
    print(f"  fraction with delay_prob < 0.10: {below_01}/{n_decisions} ({100.0*below_01/n_decisions:.1f}%)")
    print()
    print("-- (1) pool-size uniformity across eligible lines, per decision --")
    print(f"  mean CV(pool_size) across eligible lines: {statistics.mean(pool_size_cvs):.4f}  "
          f"median CV: {statistics.median(pool_size_cvs):.4f}")
    print(f"  decisions where ALL eligible lines have IDENTICAL pool_size: "
          f"{frac_identical_pool_size}/{n_decisions} ({100.0*frac_identical_pool_size/n_decisions:.1f}%)")

    return {
        'delay_probs': delay_probs,
        'pool_size_cvs': pool_size_cvs,
        'frac_identical_pool_size': frac_identical_pool_size,
        'n_decisions': n_decisions,
    }


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
print(f"delay_prob mean:           window1={statistics.mean(r1['delay_probs']):.4f}  "
      f"window2={statistics.mean(r2['delay_probs']):.4f}")
print(f"delay_prob median:         window1={statistics.median(r1['delay_probs']):.4f}  "
      f"window2={statistics.median(r2['delay_probs']):.4f}")
print(f"pool_size CV mean:         window1={statistics.mean(r1['pool_size_cvs']):.4f}  "
      f"window2={statistics.mean(r2['pool_size_cvs']):.4f}")
print(f"identical-pool-size frac:  window1={100.0*r1['frac_identical_pool_size']/r1['n_decisions']:.1f}%  "
      f"window2={100.0*r2['frac_identical_pool_size']/r2['n_decisions']:.1f}%")

print("\nDONE")
