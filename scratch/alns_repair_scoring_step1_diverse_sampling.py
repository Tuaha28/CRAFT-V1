"""
Step 1 re-run with the sampling methodology fixed. ALNS_REPAIR_SCORING_
PROPOSAL.md Section 12 / GA_AGENT_POSTMORTEM.md Section 12 found the
original harness's "450 decisions" was actually only 10 UNIQUE operations
(5 per window), each replayed 45 times identically -- because
_destroy_high_delay_risk is rng-free and its top-K is removal_frac-
invariant, and `start` (the seeded chromosome) never mutates across the
sampling loop.

TWO independent fixes, both applied (the postmortem note names either as
sufficient; using both maximizes real diversity per the follow-up
instruction):

  1. MULTIPLE STARTING CHROMOSOMES/WINDOWS: three windows (orders
     [0:300), [300:600), [600:900) of the 1,029 resampled synthetic orders in snapshot
     "20260825"), not two -- three genuinely different load landscapes,
     each with its own heuristic-seeded baseline chromosome.

  2. REAL RANDOMNESS WITHIN THE DESTROY BATCH, still grounded in real
     delay-risk ranking: instead of always taking the deterministic top-5
     by delay_prob (frac-invariant, hence identical every repeat), each
     repeat draws a random sample of K=8 operations WITHOUT replacement
     from the real high-delay-risk destroy batch _destroy_high_delay_risk
     actually returns for that (window, frac). This is not pure noise --
     every sampled operation is still a real member of the top
     removal_frac fraction by GNN-predicted delay risk -- but which
     members get compared now genuinely varies run-to-run within a
     window/frac, closing the collapse.

Reports BOTH raw comparison count and UNIQUE op_key count explicitly, so
this harness can never again silently understate how repetitive its own
sample is -- the exact self-documentation gap that let the original
collapse go unnoticed (GA_AGENT_POSTMORTEM.md Section 12).

Also re-checks the ORIGINAL (structurally-dead) interaction term under
this same diverse sampling, as a free cross-check that its 0-differences
verdict was not itself an artifact of the old sampling bug.

Run multiple times (separate process invocations) for the same stability
check every Step 1 script in this thread has run. Read-only: does not
modify ga/alns.py, ga/local_search.py, or ga/operators.py.
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
from ga.alns import _destroy_high_delay_risk
from ga.local_search import compute_pool_loads
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR

COMMIT_SIZE = 300
WINDOW_STARTS = [0, 300, 600]  # 3 genuinely different windows, not 2
REMOVAL_FRACS = [0.02, 0.035, 0.05]
SAMPLES_PER_FRAC = 20   # repeats per (window, frac) -- each now a REAL random draw
K_PER_BATCH = 8         # operations sampled per repeat, up from the old fixed top-5

W_LOAD, W_RISK, W_IMPACT = 1.0, 1.0, 0.3
RNG_SEED = 0  # fixed for reproducibility -- this is what the stability check verifies

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
print(f"total distinct orders available: {len(all_ids)}")


def window_data(pos, size):
    ids = set(all_ids[pos:pos + size])
    sub = builder.df_routings[builder.df_routings['production_order_id'].isin(ids)]
    keys = {make_key(r['production_order_id'], r['operation_id']) for _, r in sub.iterrows()}
    preds = {k: v for k, v in preds_all.items() if k in keys}
    return sub, preds


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


def current_rule_line(eligible, pool_loads, optype):
    return min(eligible, key=lambda ln: pool_loads.get((ln, optype), 0.0))


def original_score_line(eligible, pool_loads, optype, delay_prob, impact_counts, max_load, max_impact):
    def score(ln):
        load = pool_loads.get((ln, optype), 0.0)
        norm_load = load / max_load if max_load else 0.0
        norm_impact = impact_counts.get(ln, 0) / max_impact if max_impact else 0.0
        return (W_LOAD * norm_load) + (W_RISK * delay_prob * norm_load) + (W_IMPACT * norm_impact)
    return min(eligible, key=score)


def corrected_score_line(eligible, pool_loads, optype, op_key, delay_prob, impact_counts,
                          op_work, factory, max_load, max_impact):
    marginals = {ln: op_work.get(op_key, 0.0) / pool_size(factory, op_key, ln) for ln in eligible}
    max_marginal = max(marginals.values()) if marginals else 1.0

    def score(ln):
        load = pool_loads.get((ln, optype), 0.0)
        norm_load = load / max_load if max_load else 0.0
        norm_marginal = marginals[ln] / max_marginal if max_marginal else 0.0
        norm_impact = impact_counts.get(ln, 0) / max_impact if max_impact else 0.0
        return (W_LOAD * norm_load) + (W_RISK * delay_prob * norm_marginal) + (W_IMPACT * norm_impact)
    return min(eligible, key=score)


def compute_impact_counts(chromosome, num_ops, factory, destroyed_idxs, optype):
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


def run_check(name, sched, start, rng):
    print()
    print("=" * 70)
    print(f"{name}")
    print("=" * 70)
    n = sched.num_ops

    same_orig, diff_orig = 0, 0
    same_corr, diff_corr = 0, 0
    seen_op_keys = set()
    raw_count = 0

    for frac in REMOVAL_FRACS:
        idxs_full = _destroy_high_delay_risk(start, n, sched.gnn_predictions, frac)
        if not idxs_full:
            continue
        os_array = start[:n]
        pool_loads = compute_pool_loads(start, n, sched.factory, sched.op_work)

        for repeat in range(SAMPLES_PER_FRAC):
            k = min(K_PER_BATCH, len(idxs_full))
            sampled_idxs = rng.sample(idxs_full, k)

            for idx in sampled_idxs:
                op_key = os_array[idx]
                optype = sched.factory.optype_for_op.get(op_key)
                eligible = sched.factory.eligibility_map.get(op_key, [])
                if len(eligible) < 2:
                    continue

                delay_prob = sched.gnn_predictions.get(op_key, {}).get('delay', 0.0)
                impact_counts = compute_impact_counts(start, n, sched.factory, idxs_full, optype)
                loads_for_type = [pool_loads.get((ln, optype), 0.0) for ln in eligible]
                max_load = max(loads_for_type) if loads_for_type else 1.0
                max_impact = max(impact_counts.values()) if impact_counts else 1.0

                current_choice = current_rule_line(eligible, pool_loads, optype)
                orig_choice = original_score_line(eligible, pool_loads, optype, delay_prob,
                                                    impact_counts, max_load, max_impact)
                corr_choice = corrected_score_line(eligible, pool_loads, optype, op_key, delay_prob,
                                                    impact_counts, sched.op_work, sched.factory,
                                                    max_load, max_impact)

                raw_count += 1
                seen_op_keys.add(op_key)
                if current_choice == orig_choice:
                    same_orig += 1
                else:
                    diff_orig += 1
                if current_choice == corr_choice:
                    same_corr += 1
                else:
                    diff_corr += 1

                print(f"  [decision] frac={frac:.3f} {op_key} delay_prob={delay_prob:.6f} "
                      f"current={current_choice} original={orig_choice} corrected={corr_choice}")

    total = same_orig + diff_orig
    print(f"\n{name} SUMMARY: raw_comparisons={total}, UNIQUE op_keys={len(seen_op_keys)}")
    print(f"  ORIGINAL formula:  same={same_orig} ({100.0*same_orig/max(1,total):.1f}%), "
          f"different={diff_orig} ({100.0*diff_orig/max(1,total):.1f}%)")
    print(f"  CORRECTED formula: same={same_corr} ({100.0*same_corr/max(1,total):.1f}%), "
          f"different={diff_corr} ({100.0*diff_corr/max(1,total):.1f}%)")
    return {
        'raw': total, 'unique': len(seen_op_keys),
        'diff_orig': diff_orig, 'diff_corr': diff_corr,
        'same_orig': same_orig, 'same_corr': same_corr,
    }


results = []
for wi, wstart in enumerate(WINDOW_STARTS, start=1):
    sub, preds = window_data(wstart, COMMIT_SIZE)
    print(f"Building window {wi} scheduler (orders [{wstart}:{wstart+COMMIT_SIZE})) ...")
    sched, start_genes = build_and_seed(sub, preds)
    rng = random.Random(RNG_SEED)
    results.append(run_check(f"WINDOW {wi}", sched, start_genes, rng))

print()
print("=" * 70)
print("GRAND TOTAL ACROSS ALL WINDOWS")
print("=" * 70)
raw_total = sum(r['raw'] for r in results)
unique_total = sum(r['unique'] for r in results)
diff_orig_total = sum(r['diff_orig'] for r in results)
diff_corr_total = sum(r['diff_corr'] for r in results)
print(f"raw comparisons: {raw_total}, unique op_keys (sum across windows, NOT deduped across windows): {unique_total}")
print(f"ORIGINAL formula:  different in {diff_orig_total}/{raw_total} raw "
      f"({100.0*diff_orig_total/raw_total:.1f}%)")
print(f"CORRECTED formula: different in {diff_corr_total}/{raw_total} raw "
      f"({100.0*diff_corr_total/raw_total:.1f}%)")

print("\nDONE")
