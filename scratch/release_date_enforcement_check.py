"""
One-off diagnostic (not part of the pipeline): confirms or refutes, with real
numbers, whether the "everyone available from day one" mis-modeling
(GA_AUDIT.md Section 5's finding -- release_date is used ONLY to sort orders
into rolling-horizon windows in ga/rolling_horizon.py, never enforced as an
operation ready-time floor in ga/fitness.py::evaluate_schedule, where every
order's order_ready_times defaults to 0.0) is actually a meaningful
contributor to window 1's 11.3% on-time / the 10.75x capacity-floor gap --
BEFORE writing any fix.

Two questions, both answered from real data, no GA run required (this is a
pure supply/demand-arrival shape check, not a scheduling simulation):

1. How spread are window 1's real release dates? (confirm the 20-day/300-
   order/15-per-day numbers already in GA_AUDIT.md Section 3, and check
   whether the spread is roughly uniform or itself bursty within the 20 days)

2. Does replacing "all 300 orders' work available at t=0" with "each order's
   work only available from its own real release_date" reduce the PEAK
   instantaneous backlog against the bottleneck machine type's capacity?
   This is the direct mechanism: ga/fitness.py's list-scheduler queues
   whatever is "ready" against a finite machine pool, so if 300 orders'
   worth of a bottleneck type's demand all becomes "ready" simultaneously
   at t=0 (current behaviour) rather than trickling in over 20 real days
   (release_date's actual meaning), the current model may be manufacturing
   artificial queueing/contention that a correct ready-time model would not
   have -- or it may not (if total demand still swamps capacity fast either
   way). Measured, not assumed.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

from database.db import get_engine
from database.queries import fetch_active_orders_and_routings, fetch_machine_state
from ga.chromosome import ChromosomeFactory
from ga.scheduler import DEFAULT_LINE_EFFICIENCY, DEFAULT_SETUP_TIME_MINS

MINS_PER_DAY = 1440.0
COMMIT_SIZE = 300  # matches production ga/rolling_horizon.py default

print("=" * 70)
print("LOADING REAL DATA (live database, same tables the GA uses)")
print("=" * 70)

df_routings_all = fetch_active_orders_and_routings()
df_machines = fetch_machine_state()
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())

df_routings_all = df_routings_all.drop_duplicates(subset=['production_order_id', 'operation_id']).copy()
df_routings_all['release_date'] = pd.to_datetime(df_routings_all['release_date'])

# Replicate ga/rolling_horizon.py's EXACT window-1 selection: sort orders by
# (release_date, production_order_id), take the first commit_size.
order_release = (
    df_routings_all.groupby('production_order_id')['release_date'].min()
    .reset_index().sort_values(['release_date', 'production_order_id'])
)
window1_order_ids = set(order_release['production_order_id'].head(COMMIT_SIZE).tolist())
df_w1 = df_routings_all[df_routings_all['production_order_id'].isin(window1_order_ids)].copy()

w1_release = order_release[order_release['production_order_id'].isin(window1_order_ids)].copy()
window_origin = w1_release['release_date'].min()
w1_release['release_days'] = (w1_release['release_date'] - window_origin).dt.total_seconds() / 86400.0

print(f"Window 1: {len(window1_order_ids)} orders, {len(df_w1)} operations")

print()
print("=" * 70)
print("1. WINDOW 1's REAL RELEASE-DATE SPREAD")
print("=" * 70)
span_days = w1_release['release_days'].max()
print(f"Span: {window_origin.date()} -> {w1_release['release_date'].max().date()} ({span_days:.1f} days)")
print(f"Orders/day (mean over span): {len(window1_order_ids) / span_days:.2f}")
print(f"release_days stats: min={w1_release['release_days'].min():.1f}  "
      f"mean={w1_release['release_days'].mean():.1f}  median={w1_release['release_days'].median():.1f}  "
      f"max={w1_release['release_days'].max():.1f}  std={w1_release['release_days'].std():.1f}")

print("\nDaily histogram (orders released that day):")
bins = np.arange(0, np.ceil(span_days) + 2)
hist, edges = np.histogram(w1_release['release_days'], bins=bins)
for i, count in enumerate(hist):
    if count > 0:
        bar = '#' * count
        print(f"  day {i:>3}: {count:>4}  {bar}")
uniform_expectation = len(window1_order_ids) / span_days
print(f"(uniform-arrival expectation would be ~{uniform_expectation:.1f}/day; "
      f"actual daily counts range {hist[hist>0].min()}-{hist[hist>0].max()})")

# --- Per-operation setup, same production formulas as GA_AUDIT.md Section 5 ---
if 'target_efficiency_percentage' in df_lines.columns:
    eff_series = df_lines.loc[df_lines['is_active'] == 1, 'target_efficiency_percentage']
    avg_efficiency = (eff_series.mean() / 100.0) if not eff_series.empty and pd.notna(eff_series.mean()) else DEFAULT_LINE_EFFICIENCY
else:
    avg_efficiency = DEFAULT_LINE_EFFICIENCY
avg_efficiency = max(avg_efficiency, 0.05)

factory = ChromosomeFactory(df_routings_all, df_machines, df_lines)
usable_cap_by_type = factory.usable_cap_by_type

release_lookup = dict(zip(w1_release['production_order_id'], w1_release['release_days']))

rows = []
for _, row in df_w1.iterrows():
    key = f"{row['production_order_id']}_{row['operation_id']}"
    sam = float(row.get('sam', 30))
    if sam <= 0:
        sam = 30.0
    quantity = int(row.get('target_quantity', 1))
    if quantity <= 0:
        quantity = 1
    remaining_frac = row.get('remaining_frac', 1.0)
    if pd.isna(remaining_frac):
        remaining_frac = 1.0
    remaining_frac = min(max(float(remaining_frac), 0.0), 1.0)
    quantity = max(1, int(round(quantity * remaining_frac)))

    serial_processing_mins = (sam * quantity) / avg_efficiency
    machine_minutes = serial_processing_mins + DEFAULT_SETUP_TIME_MINS
    optype = factory.optype_for_op.get(key)
    rows.append({
        'order_id': row['production_order_id'],
        'optype': optype,
        'machine_minutes': machine_minutes,
        'release_days': release_lookup.get(row['production_order_id'], 0.0),
    })

df = pd.DataFrame(rows)

print()
print("=" * 70)
print("2. PEAK BACKLOG: DAY-0-FOR-EVERYONE vs REAL RELEASE-DATE ARRIVAL")
print("=" * 70)
print("(backlog(t) = cumulative demand arrived by day t, minus cumulative")
print(" capacity available by day t; positive = queueing pressure building)")

horizon_days = int(np.ceil(span_days)) + 15  # look a bit past the window itself
t_grid = np.arange(0, horizon_days + 1, 1.0)

summary_rows = []
for optype, sub in df.groupby('optype'):
    usable = usable_cap_by_type.get(optype)
    if not usable:
        continue
    total_demand_days = sub['machine_minutes'].sum() / MINS_PER_DAY

    # Model 0 (current behaviour): all demand "ready" at t=0.
    demand0 = np.where(t_grid >= 0, total_demand_days, 0.0)  # constant total from t=0
    capacity_t = usable * t_grid / MINS_PER_DAY * MINS_PER_DAY  # usable machines * days -> machine-days
    capacity_t = usable * t_grid  # usable machines * elapsed days = machine-days of capacity
    backlog0 = demand0 - capacity_t
    peak_backlog0 = backlog0.max()
    peak_day0 = t_grid[backlog0.argmax()]

    # Model 1 (corrected): demand arrives per-order on its real release day.
    sorted_release = np.sort(sub['release_days'].values)
    sorted_minutes = sub.loc[sub['release_days'].sort_values().index, 'machine_minutes'].values \
        if False else sub.sort_values('release_days')['machine_minutes'].values
    cum_demand_days = np.cumsum(sorted_minutes) / MINS_PER_DAY
    # cumulative demand arrived BY each t in t_grid
    demand1 = np.array([
        cum_demand_days[np.searchsorted(sorted_release, t, side='right') - 1]
        if np.searchsorted(sorted_release, t, side='right') > 0 else 0.0
        for t in t_grid
    ])
    backlog1 = demand1 - capacity_t
    peak_backlog1 = backlog1.max()
    peak_day1 = t_grid[backlog1.argmax()]

    reduction_pct = 100.0 * (peak_backlog0 - peak_backlog1) / peak_backlog0 if peak_backlog0 > 0 else 0.0

    print(f"\n  {optype} (usable={usable}, total demand={total_demand_days:.1f} machine-days):")
    print(f"    Model 0 (day-0-for-everyone):  peak backlog {peak_backlog0:>8.1f} machine-days at day {peak_day0:.0f}")
    print(f"    Model 1 (real release dates):  peak backlog {peak_backlog1:>8.1f} machine-days at day {peak_day1:.0f}")
    print(f"    Peak-backlog reduction: {reduction_pct:.1f}%")

    summary_rows.append({
        'optype': optype, 'usable': usable, 'total_demand_days': total_demand_days,
        'peak_backlog0': peak_backlog0, 'peak_backlog1': peak_backlog1,
        'reduction_pct': reduction_pct,
    })

print()
print("=" * 70)
print("3. ORDERS 'IN FLIGHT' (released, current naive-vs-real comparison)")
print("=" * 70)
print("Under the CURRENT model, all 300 window-1 orders are treated as")
print("in-flight (ready to be worked) from t=0 onward -- there is no other")
print("option, since release_date is never read as a readiness gate.")
print("Under REAL release dates, orders-released-by-day-t is:")
for t in [0, 1, 2, 5, 10, 15, 20]:
    n_released = int((w1_release['release_days'] <= t).sum())
    print(f"  day {t:>3}: {n_released:>4} / {len(window1_order_ids)} orders released "
          f"({100.0*n_released/len(window1_order_ids):.1f}%)")

print()
print("=" * 70)
print("SUMMARY")
print("=" * 70)
summary_df = pd.DataFrame(summary_rows).sort_values('reduction_pct', ascending=False)
print(summary_df.to_string(index=False))
overall_bottleneck = summary_df.iloc[0]
print(f"\nBottleneck type: {overall_bottleneck['optype']} -- "
      f"peak backlog {overall_bottleneck['peak_backlog0']:.1f}d (Model 0) vs "
      f"{overall_bottleneck['peak_backlog1']:.1f}d (Model 1), "
      f"{overall_bottleneck['reduction_pct']:.1f}% reduction")

print("\nDONE")
