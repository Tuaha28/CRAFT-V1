"""
Follow-up to scratch/alns_delta_calibration.py, per instruction: trace
the real improving high_delay_risk destroy-repair moves on window 2,
operation by operation -- what actually changed each time (line, machine
count, which pool was relieved) -- to determine whether there is a
repeatable structural pattern or whether the window-2 win was closer to
a lucky one-off. Same rng seeding (Random(0)) and same call sequence as
the original calibration script, so this reproduces the exact same 30
high_delay_risk cycles (10 per removal fraction) already measured,
this time with full per-operation instrumentation instead of just the
aggregate delta.
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
from ga.alns import (
    DESTROY_OPERATORS, _destroy_high_delay_risk, _destroy_bottleneck_pool,
    _destroy_random, _repair_reassign,
)
from ga.local_search import compute_pool_loads
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
window2_ids = set(all_ids[COMMIT_SIZE:COMMIT_SIZE * 2])
window2_sub = builder.df_routings[builder.df_routings['production_order_id'].isin(window2_ids)]
keys = {make_key(r['production_order_id'], r['operation_id']) for _, r in window2_sub.iterrows()}
window2_preds = {k: v for k, v in preds_all.items() if k in keys}

window1_ids = set(all_ids[:COMMIT_SIZE])
window1_sub = builder.df_routings[builder.df_routings['production_order_id'].isin(window1_ids)]
keys1 = {make_key(r['production_order_id'], r['operation_id']) for _, r in window1_sub.iterrows()}
window1_preds = {k: v for k, v in preds_all.items() if k in keys1}
print("Building window 1 scheduler FIRST (matching alns_delta_calibration.py's process order -- "
      "testing whether construction order affects reproducibility)...")
_sched_w1 = GAScheduler(window1_sub, df_machines, df_lines, window1_preds,
                         current_time=ORIGIN, present_operator_count=capacity)
_ = _sched_w1.run_baselines()

print("Building window 2 scheduler...")
sched = GAScheduler(window2_sub, df_machines, df_lines, window2_preds,
                     current_time=ORIGIN, present_operator_count=capacity)
baselines = sched.run_baselines()
best_name = min(baselines, key=lambda k: baselines[k]['fitness'])
start = baseline_chromosomes(
    sched.factory, sched.op_details, sched.order_deadlines_minutes, sched.seq_lookup
)[best_name]
start_fit = baselines[best_name]['fitness']
n = sched.num_ops
print(f"start fitness {start_fit:.2f} (seeded from '{best_name}')")

rng = random.Random(0)  # identical to alns_delta_calibration.py's measure()

improving_moves = []

for frac in REMOVAL_FRACS:
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

        if op == 'high_delay_risk':
            # Capture full before/after state for every destroyed operation.
            os_before = start[:n]
            la_before = start[n:2 * n]
            mc_before = start[2 * n:]
            pool_loads_before = compute_pool_loads(start, n, sched.factory, sched.op_work)
            before = {}
            for idx in idxs:
                op_key = os_before[idx]
                optype = sched.factory.optype_for_op.get(op_key)
                before[idx] = {
                    'op_key': op_key, 'line': la_before[idx], 'mc': mc_before[idx],
                    'optype': optype, 'delay_prob': sched.gnn_predictions.get(op_key, {}).get('delay', 0.0),
                    'pool_load_before': pool_loads_before.get((la_before[idx], optype), 0.0),
                }

        cand = _repair_reassign(start, n, sched.factory, sched.op_work, idxs, rng)
        cand_fit = sched.toolbox.evaluate(cand)[0]
        delta = cand_fit - start_fit

        if op == 'high_delay_risk':
            cand_fit_2 = sched.toolbox.evaluate(cand)[0]
            print(f"  [raw] frac={frac:.3f} i={i} delta={delta:+.3f} n_destroyed={len(idxs)} "
                  f"re-eval same candidate: {cand_fit:.3f} vs {cand_fit_2:.3f} "
                  f"(diff={cand_fit_2 - cand_fit:+.4f})")

        if op == 'high_delay_risk' and delta < -1e-9:
            os_after = cand[:n]
            la_after = cand[n:2 * n]
            mc_after = cand[2 * n:]
            pool_loads_after = compute_pool_loads(cand, n, sched.factory, sched.op_work)
            changes = []
            for idx in idxs:
                b = before[idx]
                after_line = la_after[idx]
                after_mc = mc_after[idx]
                changes.append({
                    **b,
                    'line_after': after_line, 'mc_after': after_mc,
                    'line_changed': after_line != b['line'],
                    'mc_changed': after_mc != b['mc'],
                    'pool_load_after': pool_loads_after.get((after_line, b['optype']), 0.0),
                })
            improving_moves.append({'frac': frac, 'sample_i': i, 'delta': delta, 'changes': changes})

print(f"\n{len(improving_moves)} improving high_delay_risk moves captured (out of up to "
      f"{len(REMOVAL_FRACS) * (SAMPLES_PER_FRAC // 3)} possible)")

for m in improving_moves:
    print()
    print("=" * 70)
    print(f"frac={m['frac']:.3f}  sample_i={m['sample_i']}  delta={m['delta']:.2f}")
    print("=" * 70)
    for c in m['changes']:
        line_move = f"line {c['line']} -> {c['line_after']}" if c['line_changed'] else f"line {c['line']} (unchanged)"
        mc_move = f"mc {c['mc']} -> {c['mc_after']}" if c['mc_changed'] else f"mc {c['mc']} (unchanged)"
        print(f"  {c['op_key']:<14} optype={c['optype']:<22} delay_prob={c['delay_prob']:.3f}  "
              f"{line_move:<24} {mc_move:<18} "
              f"pool_load {c['pool_load_before']:.1f} -> {c['pool_load_after']:.1f}")

# Summary: how many destroyed operations actually changed line vs stayed put,
# across ALL captured improving moves -- the direct answer to "is there a
# repeatable structural pattern".
total_ops = sum(len(m['changes']) for m in improving_moves)
line_changed = sum(1 for m in improving_moves for c in m['changes'] if c['line_changed'])
mc_changed = sum(1 for m in improving_moves for c in m['changes'] if c['mc_changed'])
both_changed = sum(1 for m in improving_moves for c in m['changes'] if c['line_changed'] and c['mc_changed'])
neither = sum(1 for m in improving_moves for c in m['changes'] if not c['line_changed'] and not c['mc_changed'])

print()
print("=" * 70)
print("STRUCTURAL SUMMARY across all captured improving moves")
print("=" * 70)
print(f"total destroyed-operation instances across all improving moves: {total_ops}")
print(f"  line changed:      {line_changed} ({100.0*line_changed/max(1,total_ops):.1f}%)")
print(f"  mc changed:        {mc_changed} ({100.0*mc_changed/max(1,total_ops):.1f}%)")
print(f"  both changed:      {both_changed} ({100.0*both_changed/max(1,total_ops):.1f}%)")
print(f"  neither changed:   {neither} ({100.0*neither/max(1,total_ops):.1f}%)")

# Are the SAME operations being destroyed across different frac values
# (expected, since _destroy_high_delay_risk is deterministic given the
# same starting chromosome -- larger frac just extends the same ranked list)?
by_frac = {}
for m in improving_moves:
    by_frac.setdefault(m['frac'], []).append({c['op_key'] for c in m['changes']})
print("\nDestroyed-operation-set overlap across samples within each frac (should be "
      "identical if high_delay_risk is purely deterministic given a fixed start):")
for frac, sets in by_frac.items():
    all_same = all(s == sets[0] for s in sets)
    print(f"  frac={frac:.3f}: {len(sets)} improving samples, identical destroyed-set across all = {all_same}")

print("\nDONE")
