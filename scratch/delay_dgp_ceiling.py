"""
One-off diagnostic (not part of the pipeline): what is the THEORETICAL
BEST-POSSIBLE AP for the delay label, given the label's own generating
process? This is synthetic data -- data_generation/seed_database_fixed.py
::sample_execution_variance_multiplier() IS the ground truth, not
something to infer statistically. If a model with PERFECT knowledge of
the true per-lot causal inputs (breakdown count, on_leave, queue delay --
exactly what the GNN's features are lossy PROXIES for) still can't beat
some ceiling AP by much, that tells us how much of the current AP=0.16-0.17
gap-to-perfect is genuinely irreducible noise vs. how much is left on the
table by imperfect features/model.

METHOD: for each real operation (held-out val split, same as the GNN's),
take its REAL recorded per-lot causal inputs (already baked into the live
database at seed time) and Monte-Carlo resample
sample_execution_variance_multiplier() many times using THOSE exact
inputs, to estimate P(label=1 | true causal inputs) for that operation --
an ORACLE prediction no real feature set could ever beat, since it uses
the exact per-lot values the label's own randomness is conditioned on,
not an aggregated/lossy proxy. Score this oracle's AP against the
operations' REAL observed labels (one actual draw each) -- this is the
Bayes-optimal ceiling for ANY classifier on this label, GNN or otherwise.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

from database.db import get_engine
from data_generation.seed_database_fixed import sample_execution_variance_multiplier
from graph.graph_builder import FactoryGraphBuilder
from graph.labels import DELAY_LABEL_LATENESS_FRACTION

N_REPLICATES = 500
rng = random.Random()  # unseeded on purpose -- genuine Monte Carlo, not reproducing one fixed draw

print("Fetching per-lot causal inputs + actual outcomes (one query, batch_id-keyed to avoid a many-to-many join)...")
query = """
    SELECT
        production_order_id, operation_id, batch_id,
        planned_start_time, planned_end_time, actual_start_time, actual_end_time,
        employee_on_leave_at_dispatch, queue_delay_mins_at_dispatch, machine_breakdown_count_at_dispatch
    FROM Job_Shop_Schedules
    WHERE actual_start_time IS NOT NULL AND actual_end_time IS NOT NULL;
"""
df = pd.read_sql(query, get_engine())
df['unique_op_key'] = df['production_order_id'].astype(str) + "_" + df['operation_id'].astype(str)
df['lot_duration_mins'] = (
    (df['planned_end_time'] - df['planned_start_time']).dt.total_seconds() / 60.0
).clip(lower=1.0)
print(f"lots: {len(df)}, operations: {df['unique_op_key'].nunique()}")

print("Building graph to reuse the EXACT same val split the GNN was scored on...")
builder = FactoryGraphBuilder()
hetero_graph, labels = builder.build_graph()
val_mask = hetero_graph['operation'].val_mask.numpy()
op_map = builder.op_map  # unique_op_key -> node index
idx_to_key = {v: k for k, v in op_map.items()}
val_keys = {idx_to_key[i] for i in range(len(val_mask)) if val_mask[i] and i in idx_to_key}
print(f"val operations (from graph split): {len(val_keys)}")

df_val = df[df['unique_op_key'].isin(val_keys)].copy()
print(f"lots in val operations with real actual timing: {len(df_val)}")

# REAL observed label per operation, identical logic to graph/labels.py
agg_real = df_val.groupby('unique_op_key').agg(
    latest_actual_end=('actual_end_time', 'max'),
    latest_planned_end=('planned_end_time', 'max'),
    mean_lot_duration_mins=('lot_duration_mins', 'mean'),
)
agg_real['lateness_frac'] = (
    (agg_real['latest_actual_end'] - agg_real['latest_planned_end']).dt.total_seconds() / 60.0
) / agg_real['mean_lot_duration_mins']
agg_real['real_label'] = (agg_real['lateness_frac'] > DELAY_LABEL_LATENESS_FRACTION).astype(int)

print(f"\nReal positive rate (val operations, this run): {agg_real['real_label'].mean():.4f}, n={len(agg_real)}")

print(f"\nRunning Monte Carlo oracle simulation ({N_REPLICATES} replicates per operation)...")
lots_by_op = {k: g for k, g in df_val.groupby('unique_op_key')}

oracle_probs = []
real_labels = []
for i, (op_key, real_row) in enumerate(agg_real.iterrows()):
    # Precompute plain-Python records ONCE per operation -- iterrows() inside
    # the 500x replicate loop would re-pay pandas Series-construction
    # overhead on every single draw, ~16.6M times total across the run.
    lot_records = lots_by_op[op_key][[
        'machine_breakdown_count_at_dispatch', 'employee_on_leave_at_dispatch',
        'queue_delay_mins_at_dispatch', 'lot_duration_mins', 'planned_start_time',
    ]].to_dict('records')
    positive_count = 0
    for _ in range(N_REPLICATES):
        sim_actual_ends = []
        for lot in lot_records:
            mult = sample_execution_variance_multiplier(
                rng=rng,
                machine_breakdown_count_at_dispatch=float(lot['machine_breakdown_count_at_dispatch'] or 0.0),
                on_leave=bool(lot['employee_on_leave_at_dispatch']),
                queue_delay_mins=float(lot['queue_delay_mins_at_dispatch'] or 0.0),
            )
            sim_duration = lot['lot_duration_mins'] * mult
            sim_actual_end = lot['planned_start_time'] + pd.Timedelta(minutes=sim_duration)
            sim_actual_ends.append(sim_actual_end)
        sim_latest_actual_end = max(sim_actual_ends)
        sim_lateness_frac = (
            (sim_latest_actual_end - real_row['latest_planned_end']).total_seconds() / 60.0
        ) / real_row['mean_lot_duration_mins']
        if sim_lateness_frac > DELAY_LABEL_LATENESS_FRACTION:
            positive_count += 1
    oracle_probs.append(positive_count / N_REPLICATES)
    real_labels.append(real_row['real_label'])
    if (i + 1) % 200 == 0:
        print(f"  ...{i+1}/{len(agg_real)} operations simulated")

oracle_probs = np.array(oracle_probs)
real_labels = np.array(real_labels)

ceiling_ap = average_precision_score(real_labels, oracle_probs)
base_rate = real_labels.mean()

print("\n" + "="*70)
print("RESULT")
print("="*70)
print(f"n operations: {len(real_labels)}, base rate: {base_rate:.4f}")
print(f"oracle predicted-prob range: [{oracle_probs.min():.4f}, {oracle_probs.max():.4f}]")
print(f"THEORETICAL CEILING AP (perfect knowledge of true causal inputs): {ceiling_ap:.4f}")
print(f"no-skill baseline AP: {base_rate:.4f}")
print(f"\nFor comparison (already measured): GNN shared-encoder AP = 0.1591-0.1723")
print("\nDONE")
