"""
ALNS_REPAIR_SCORING_PROPOSAL.md Section 6, Step 1: cheap mechanism check,
before any implementation. Does a richer candidate-line score (load +
GNN-delay-risk interaction + downstream-impact proxy) actually pick
DIFFERENT lines than the current pure-load rule, on real destroy
batches? And -- per instruction, added specifically because
GA_AGENT_POSTMORTEM.md's new methodology note found GNN inference is not
guaranteed bit-reproducible across separate process runs, and the risk
term here is built directly from that same inference -- is the RICHER
score's own choice stable across repeated (fresh-inference) runs on the
IDENTICAL destroy batch, or does it inherit the same fragility that just
flipped a hard cutoff elsewhere?

Uses placeholder weights (w_load=1.0, w_risk=1.0, w_impact=0.3) purely to
test whether the mechanism produces different, and stable, answers --
NOT proposed as tuned defaults (ALNS_REPAIR_SCORING_PROPOSAL.md Section 3
is explicit that weights are a Step 2 empirical question, not assumed
here).

downstream_impact proxy, a Step-1 pragmatic simplification stated
explicitly: count of operations OTHER than the ones in this destroy
batch whose eligible lines include the candidate line for this optype --
not position-based (the full "later in sequence" version would need
per-operation bookkeeping this cheap check does not attempt), computed
once per destroy batch and reused for every operation in it.

Does NOT modify ga/alns.py, ga/local_search.py, or ga/operators.py.
Run this script multiple times (separate process invocations) and diff
the output to test stability -- that is the point of the exercise.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random

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
SAMPLES_PER_FRAC = 15  # cheap mechanism check, not a full calibration sweep

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


def current_rule_line(eligible, pool_loads, optype):
    return min(eligible, key=lambda ln: pool_loads.get((ln, optype), 0.0))


def richer_score_line(eligible, pool_loads, optype, delay_prob, impact_counts, max_load, max_impact):
    def score(ln):
        load = pool_loads.get((ln, optype), 0.0)
        norm_load = load / max_load if max_load else 0.0
        norm_impact = impact_counts.get(ln, 0) / max_impact if max_impact else 0.0
        return (W_LOAD * norm_load) + (W_RISK * delay_prob * norm_load) + (W_IMPACT * norm_impact)
    return min(eligible, key=score)


def compute_impact_counts(chromosome, num_ops, factory, destroyed_idxs, optype):
    """Step-1 proxy (see module docstring): count of operations OUTSIDE
    this destroy batch eligible for each candidate line, for this optype."""
    os_array = chromosome[:num_ops]
    destroyed_ops = {os_array[i] for i in destroyed_idxs}
    counts = {}
    for i, op_key in enumerate(os_array):
        if op_key in destroyed_ops:
            continue
        if factory.optype_for_op.get(op_key) != optype:
            continue
        for ln in factory.eligibility_map.get(op_key, []):
            counts[ln] = counts.get(ln, 0) + 1
    return counts


def run_check(name, sched, start):
    print()
    print("=" * 70)
    print(f"{name}")
    print("=" * 70)
    n = sched.num_ops
    rng = random.Random(0)

    same = 0
    different = 0
    diffs_detail = []

    for frac in REMOVAL_FRACS:
        for i in range(SAMPLES_PER_FRAC * 3):
            op = DESTROY_OPERATORS[i % 3]
            if op != 'high_delay_risk':
                # still advance rng identically to keep the sequence
                # comparable to prior scripts' sampling pattern
                if op == 'bottleneck_pool':
                    _destroy_bottleneck_pool(start, n, sched.factory, sched.op_work, frac, rng)
                else:
                    _destroy_random(start, n, frac, rng)
                continue
            idxs = _destroy_high_delay_risk(start, n, sched.gnn_predictions, frac)
            if not idxs:
                continue

            os_array = start[:n]
            la_array = start[n:2 * n]
            pool_loads = compute_pool_loads(start, n, sched.factory, sched.op_work)

            for idx in idxs[:5]:  # first 5 destroyed ops per batch -- cheap, still representative
                op_key = os_array[idx]
                optype = sched.factory.optype_for_op.get(op_key)
                eligible = sched.factory.eligibility_map.get(op_key, [])
                if len(eligible) < 2:
                    continue  # no real choice to compare
                delay_prob = sched.gnn_predictions.get(op_key, {}).get('delay', 0.0)

                impact_counts = compute_impact_counts(start, n, sched.factory, idxs, optype)
                loads_for_type = [pool_loads.get((ln, optype), 0.0) for ln in eligible]
                max_load = max(loads_for_type) if loads_for_type else 1.0
                max_impact = max(impact_counts.values()) if impact_counts else 1.0

                current_choice = current_rule_line(eligible, pool_loads, optype)
                richer_choice = richer_score_line(eligible, pool_loads, optype, delay_prob,
                                                   impact_counts, max_load, max_impact)

                if current_choice == richer_choice:
                    same += 1
                else:
                    different += 1
                    diffs_detail.append(
                        f"    {op_key} (delay_prob={delay_prob:.3f}): current->{current_choice}  "
                        f"richer->{richer_choice}  eligible={len(eligible)}"
                    )
                # Machine-readable line for cross-run diffing.
                print(f"  [decision] frac={frac:.3f} i={i} {op_key} delay_prob={delay_prob:.6f} "
                      f"current={current_choice} richer={richer_choice}")

    total = same + different
    print(f"\n{name} SUMMARY: {total} comparable decisions, "
          f"same={same} ({100.0*same/max(1,total):.1f}%), different={different} ({100.0*different/max(1,total):.1f}%)")
    if diffs_detail:
        print("Sample of decisions where the richer score picked a DIFFERENT line:")
        for line in diffs_detail[:15]:
            print(line)


print("Building window 1 scheduler...")
sched_w1, start_w1 = build_and_seed(window1_sub, window1_preds)
print("Building window 2 scheduler...")
sched_w2, start_w2 = build_and_seed(window2_sub, window2_preds)

run_check("WINDOW 1", sched_w1, start_w1)
run_check("WINDOW 2", sched_w2, start_w2)

print("\nDONE")
