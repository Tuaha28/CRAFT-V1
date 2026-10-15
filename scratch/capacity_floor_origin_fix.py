"""
Bug check + fix: scratch/corrected_simulation_run.py's Floor B-tight (386.0d)
computed release_days relative to the BOOK's earliest release date
(2026-06-27), but the actual GA run (ga/scheduler.py's self.now, via
order_release_minutes) measures release offsets relative to ORIGIN =
datetime.now() at run time (2026-08-24T15:58:13, ~58 days AFTER the
earliest release) and CLAMPS any release date before that to 0.0 (already
released = available now). Those are two different origins -- the floor
script's 386.0d is not directly comparable to the achieved 327.6d makespan,
which used the real origin. Recomputing Floor B-tight against the SAME
origin the real run used, to check whether the apparent "achieved beats
the floor" (0.85x) result was this bug rather than a real finding.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
import pandas as pd

from database.db import get_engine
from database.queries import fetch_active_orders_and_routings, fetch_machine_state
from ga.chromosome import ChromosomeFactory
from ga.scheduler import DEFAULT_LINE_EFFICIENCY, DEFAULT_SETUP_TIME_MINS

MINS_PER_DAY = 1440.0
# Exact origin the real corrected_simulation_run.py used (from its log).
ORIGIN = datetime.fromisoformat("2026-08-24T15:58:13.065670")

df_routings_all = fetch_active_orders_and_routings()
df_machines = fetch_machine_state()
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())
df_routings_all = df_routings_all.drop_duplicates(subset=['production_order_id', 'operation_id']).copy()
df_routings_all['release_date'] = pd.to_datetime(df_routings_all['release_date'])

if 'target_efficiency_percentage' in df_lines.columns:
    eff_series = df_lines.loc[df_lines['is_active'] == 1, 'target_efficiency_percentage']
    avg_efficiency = (eff_series.mean() / 100.0) if not eff_series.empty and pd.notna(eff_series.mean()) else DEFAULT_LINE_EFFICIENCY
else:
    avg_efficiency = DEFAULT_LINE_EFFICIENCY
avg_efficiency = max(avg_efficiency, 0.05)

factory = ChromosomeFactory(df_routings_all, df_machines, df_lines)
usable_cap_by_type = factory.usable_cap_by_type

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
    # SAME clamp ga/scheduler.py applies: max(0.0, (release_dt - self.now)/60)
    release_days = max(0.0, (row['release_date'] - ORIGIN).total_seconds() / 86400.0)
    rows.append({'optype': optype, 'machine_minutes': serial + DEFAULT_SETUP_TIME_MINS, 'release_days': release_days})
df = pd.DataFrame(rows)

print(f"ORIGIN: {ORIGIN.isoformat()}")
print(f"release_days range under the REAL origin+clamp: "
      f"{df['release_days'].min():.1f} to {df['release_days'].max():.1f}")
n_already_released = (df['release_days'] == 0.0).sum()
print(f"operations clamped to release_days=0.0 (already released as of ORIGIN): "
      f"{n_already_released} / {len(df)} ({100.0*n_already_released/len(df):.1f}%)")

def release_aware_floor(sub_df, usable):
    cutoffs = sorted(sub_df['release_days'].unique())
    best = 0.0
    for r in cutoffs:
        remaining_days = sub_df.loc[sub_df['release_days'] >= r, 'machine_minutes'].sum() / MINS_PER_DAY
        bound = r + (remaining_days / usable)
        best = max(best, bound)
    return best

floor_b_tight = 0.0
for optype, sub in df.groupby('optype'):
    usable = usable_cap_by_type.get(optype)
    aware = release_aware_floor(sub, usable)
    floor_b_tight = max(floor_b_tight, aware)
    print(f"  {optype:<22} release-aware floor (real origin): {aware:>7.1f}d")

print(f"\nFloor B-tight, RECOMPUTED against the real run's origin: {floor_b_tight:.1f} days")
print(f"Achieved makespan (from the real corrected run): 327.6 days")
print(f"Ratio achieved/floor: {327.6/floor_b_tight:.3f}x" if floor_b_tight else "n/a")
print("\nDONE")
