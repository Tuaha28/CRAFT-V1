"""
One-off diagnostic (not part of the pipeline): the single, consistent
re-run requested after the release-date-enforcement fix (GA_AUDIT.md
Section 5/6/8) -- capacity floor, GA, and FIFO/EDF/CriticalRatio/
RandomRepaired baselines, ALL on the SAME corrected simulation, one
process, one origin, one seed. Not baselines re-added piecemeal on top of
a still-broken assumption: run_baselines() and the GA both now read
order_release_minutes through the identical evaluate_schedule() call this
fix patched, so there is exactly one ready-time definition in play here,
not two.

Also reports real in-flight-order-count-over-time from the actual GA
output (ga/rolling_horizon.py's aggregate now carries order_release_minutes
/order_completion_minutes, both merged across windows the same way
`lateness` already was) -- replacing the "all 300 at once" assumption with
what the corrected simulation actually produces.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from datetime import datetime

import numpy as np
import pandas as pd
import torch

from database.db import get_engine
from database.queries import fetch_active_orders_and_routings, fetch_machine_state, fetch_concurrent_operator_capacity
from ga.chromosome import ChromosomeFactory
from ga.scheduler import DEFAULT_LINE_EFFICIENCY, DEFAULT_SETUP_TIME_MINS
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from ga.rolling_horizon import run_rolling_horizon

MINS_PER_DAY = 1440.0
COMMIT_SIZE = 300
POP_SIZE = 50
GENERATIONS = 50
POLISH_EVALS = 400
SEED = 42

print("=" * 70)
print("SETUP -- loading real data + real GNN predictions ONCE")
print("=" * 70)

builder = FactoryGraphBuilder()
graph, _ = builder.build_graph()
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())

model = MultiTaskFactoryGNN(metadata=graph.metadata(), hidden_channels=GNN_HIDDEN_CHANNELS)
model.load_state_dict(torch.load(os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")))
model.eval()
with torch.no_grad():
    out = model(graph.x_dict, graph.edge_index_dict, graph['operation'].type_idx)
delay = torch.sigmoid(out['delay']).squeeze(-1)
preds_all = {k: {'delay': delay[i].item(), 'priority': out['priority'][i].item()}
             for i, k in enumerate(builder.op_map.keys())}

capacity = fetch_concurrent_operator_capacity()
ORIGIN = datetime.now()
print(f"fixed origin timestamp: {ORIGIN.isoformat()}")
print(f"commit=lookahead={COMMIT_SIZE}  pop={POP_SIZE}  gen={GENERATIONS}  polish={POLISH_EVALS}  seed={SEED}")

# ---------------------------------------------------------------------------
# PART 1: fresh capacity floor (same static computation as
# scratch/capacity_floor_current.py -- now genuinely the right comparison,
# since the simulation this run scores against actually enforces release
# dates, unlike when that script was first written).
# ---------------------------------------------------------------------------
print()
print("=" * 70)
print("PART 1: CAPACITY FLOOR (same-day and release-date-aware, per-type)")
print("=" * 70)

if 'target_efficiency_percentage' in df_lines.columns:
    eff_series = df_lines.loc[df_lines['is_active'] == 1, 'target_efficiency_percentage']
    avg_efficiency = (eff_series.mean() / 100.0) if not eff_series.empty and pd.notna(eff_series.mean()) else DEFAULT_LINE_EFFICIENCY
else:
    avg_efficiency = DEFAULT_LINE_EFFICIENCY
avg_efficiency = max(avg_efficiency, 0.05)

df_routings_all = builder.df_routings.drop_duplicates(subset=['production_order_id', 'operation_id']).copy()
df_routings_all['release_date'] = pd.to_datetime(df_routings_all['release_date'])
earliest_release = df_routings_all['release_date'].min()

factory = ChromosomeFactory(df_routings_all, builder.df_machines, df_lines)
usable_cap_by_type = factory.usable_cap_by_type
total_usable = sum(usable_cap_by_type.values())

rows = []
for _, row in df_routings_all.iterrows():
    key = f"{row['production_order_id']}_{row['operation_id']}"
    sam = float(row.get('sam', 30)) or 30.0
    quantity = int(row.get('target_quantity', 1)) or 1
    remaining_frac = row.get('remaining_frac', 1.0)
    remaining_frac = 1.0 if pd.isna(remaining_frac) else min(max(float(remaining_frac), 0.0), 1.0)
    quantity = max(1, int(round(quantity * remaining_frac)))
    serial = (sam * quantity) / avg_efficiency
    optype = factory.optype_for_op.get(key)
    release_days = (row['release_date'] - earliest_release).total_seconds() / 86400.0
    rows.append({'optype': optype, 'machine_minutes': serial + DEFAULT_SETUP_TIME_MINS, 'release_days': release_days})
df = pd.DataFrame(rows)

def release_aware_floor(sub_df, usable):
    cutoffs = sorted(sub_df['release_days'].unique())
    best = 0.0
    for r in cutoffs:
        remaining_days = sub_df.loc[sub_df['release_days'] >= r, 'machine_minutes'].sum() / MINS_PER_DAY
        bound = r + (remaining_days / usable)
        best = max(best, bound)
    return best

floor_a_tight = 0.0
floor_b_tight = 0.0
for optype, sub in df.groupby('optype'):
    usable = usable_cap_by_type.get(optype, total_usable)
    same_day = (sub['machine_minutes'].sum() / MINS_PER_DAY) / usable
    aware = release_aware_floor(sub, usable)
    floor_a_tight = max(floor_a_tight, same_day)
    floor_b_tight = max(floor_b_tight, aware)
    print(f"  {optype:<22} same-day: {same_day:>6.1f}d   release-aware: {aware:>7.1f}d")

print(f"\nFloor A-tight (same-day, per-type):      {floor_a_tight:.1f} days")
print(f"Floor B-tight (release-aware, per-type):  {floor_b_tight:.1f} days")
print("(Floor B-tight is now the operative comparison -- the simulation below")
print(" genuinely enforces release dates, so same-day availability is no")
print(" longer what it's being measured against.)")

# ---------------------------------------------------------------------------
# PART 2: GA + baselines together, once, on the corrected simulation
# ---------------------------------------------------------------------------
print()
print("=" * 70)
print("PART 2: GA + FIFO/EDF/CriticalRatio/RandomRepaired, corrected simulation")
print("=" * 70)

t0 = time.time()
result = run_rolling_horizon(
    builder.df_routings, builder.df_machines, df_lines, preds_all, capacity, ORIGIN,
    commit_size=COMMIT_SIZE, lookahead_size=None,
    pop_size=POP_SIZE, generations=GENERATIONS, polish_evals=POLISH_EVALS, seed=SEED,
    export=False, decision_engine=None, operator_index=None,
    log_fn=print,
)
elapsed = time.time() - t0
agg = result['aggregate']

print()
print("=" * 70)
print("AGGREGATE RESULT")
print("=" * 70)
print(f"orders={agg['orders']}  on-time={agg['on_time_pct']:.1f}%  "
      f"makespan={agg['makespan_days']:.1f}d  throughput={agg['throughput_orders_per_day']:.1f}/d  "
      f"mean-tard={agg['mean_tardiness_h']:.0f}h  labour={agg['labour_utilisation_pct']:.1f}%  "
      f"margin={agg['mean_margin_over_baseline_pct']:+.1f}%" if agg['mean_margin_over_baseline_pct'] is not None
      else f"orders={agg['orders']}  on-time={agg['on_time_pct']:.1f}%  makespan={agg['makespan_days']:.1f}d")
print(f"total runtime: {elapsed:.0f}s")
print(f"\nComparison: Floor B-tight {floor_b_tight:.1f}d vs achieved makespan {agg['makespan_days']:.1f}d "
      f"-> ratio {agg['makespan_days']/floor_b_tight:.2f}x" if floor_b_tight else "")

# ---------------------------------------------------------------------------
# PART 3: real in-flight order count over time
# ---------------------------------------------------------------------------
print()
print("=" * 70)
print("PART 3: ORDERS GENUINELY IN FLIGHT (released, not yet complete) OVER TIME")
print("=" * 70)
print("(from the ACTUAL corrected-simulation output -- release_date enforced,")
print(" real GA-assigned completion times -- not an assumption)")

releases = agg['order_release_minutes']
completions = agg['order_completion_minutes']
common_ids = set(releases) & set(completions)
print(f"{len(common_ids)} orders have both a release and a committed completion time "
      f"(of {agg['orders']} total committed).")

if common_ids:
    rel_days = {oid: releases[oid] / MINS_PER_DAY for oid in common_ids}
    comp_days = {oid: completions[oid] / MINS_PER_DAY for oid in common_ids}
    horizon = max(comp_days.values())
    sample_points = sorted(set([0, 1, 2, 5, 10, 15, 20, 30, 45, 60, 90] + [round(horizon)]))
    sample_points = [t for t in sample_points if t <= horizon]
    print(f"\n{'day':>6}{'in-flight':>12}{'released':>12}{'completed':>12}")
    for t in sample_points:
        in_flight = sum(1 for oid in common_ids if rel_days[oid] <= t <= comp_days[oid])
        released = sum(1 for oid in common_ids if rel_days[oid] <= t)
        completed = sum(1 for oid in common_ids if comp_days[oid] <= t)
        print(f"{t:>6}{in_flight:>12}{released:>12}{completed:>12}")
    peak_in_flight = max(
        sum(1 for oid in common_ids if rel_days[oid] <= t <= comp_days[oid])
        for t in np.arange(0, horizon + 1, 1.0)
    )
    print(f"\nPeak simultaneous in-flight orders: {peak_in_flight} (of {len(common_ids)}) "
          f"-- vs. the old model's implicit 'all {agg['orders']} at once from day 0'")

print("\nDONE")
