"""
ALNS_REFINEMENT_PROPOSAL.md Section 9 follow-up, per instruction: before
re-running the validation, measure the REAL typical fitness delta from an
actual multi-operation destroy-repair move at this problem's scale,
instead of assuming the borrowed single-gene-scale formula
(t0 = 0.02 * start_fitness) was ever a reasonable estimate.

Instruments a handful of real destroy-repair-evaluate cycles directly
(no full ALNS run, no acceptance logic) at all three removal fractions
already used in the validation (2%, 3.5%, 5%), on both window 1 and
window 2 (the two distinct landscapes the validation already covers),
starting fresh from the same heuristic-seeded chromosome each cycle (an
unbiased sample of "what does ONE destroy-repair move cost from a good
starting point", the exact situation the real search begins in).

Does NOT modify ga/alns.py yet -- this script only measures and reports.
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
from ga.alns import DESTROY_OPERATORS, _destroy_high_delay_risk, _destroy_bottleneck_pool, _destroy_random, _repair_reassign
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR

COMMIT_SIZE = 300
REMOVAL_FRACS = [0.02, 0.035, 0.05]
SAMPLES_PER_FRAC = 30

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
    return sched, best_genes, baselines[best_name]['fitness']


def measure(name, sched, start, start_fit):
    print()
    print("=" * 70)
    print(f"{name}  (start fitness {start_fit:.2f})")
    print("=" * 70)
    rng = random.Random(0)
    n = sched.num_ops
    for frac in REMOVAL_FRACS:
        deltas = []
        for i in range(SAMPLES_PER_FRAC):
            op = DESTROY_OPERATORS[i % 3]
            if op == 'high_delay_risk':
                idxs = _destroy_high_delay_risk(start, n, sched.gnn_predictions, frac)
            elif op == 'bottleneck_pool':
                idxs = _destroy_bottleneck_pool(start, n, sched.factory, sched.op_work, frac, rng)
            else:
                idxs = _destroy_random(start, n, frac, rng)
            if not idxs:
                continue
            cand = _repair_reassign(start, n, sched.factory, sched.op_work, idxs, rng)
            cand_fit = sched.toolbox.evaluate(cand)[0]
            deltas.append(cand_fit - start_fit)

        worsening = [d for d in deltas if d > 0]
        improving = [d for d in deltas if d < 0]
        abs_deltas = [abs(d) for d in deltas]
        print(f"removal_frac={frac:.3f} (~{max(1, round(frac*n))} ops/move, n={len(deltas)} samples):")
        print(f"  median |delta|={statistics.median(abs_deltas):.2f}  mean |delta|={statistics.mean(abs_deltas):.2f}")
        print(f"  worsening: {len(worsening)}/{len(deltas)}, median={statistics.median(worsening) if worsening else 0:.2f}, "
              f"max={max(worsening) if worsening else 0:.2f}")
        print(f"  improving: {len(improving)}/{len(deltas)}, median={statistics.median([-d for d in improving]) if improving else 0:.2f}")
        if worsening:
            import math
            implied_t0 = statistics.median(worsening) / math.log(2)
            print(f"  implied t0 (median-worsening/ln2, 50% accept at typical worsening move): {implied_t0:.2f}")
            print(f"  for comparison, OLD formula (0.02*start_fit) gave: {0.02*start_fit:.2f}")


print("Building window 1 scheduler...")
sched_w1, start_w1, fit_w1 = build_and_seed(window1_sub, window1_preds)
print("Building window 2 scheduler...")
sched_w2, start_w2, fit_w2 = build_and_seed(window2_sub, window2_preds)

measure("WINDOW 1", sched_w1, start_w1, fit_w1)
measure("WINDOW 2", sched_w2, start_w2, fit_w2)

print("\nDONE")
