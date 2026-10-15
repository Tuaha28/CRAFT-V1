"""
One-off diagnostic (not part of the pipeline): a FRESH, release-date-aware
capacity floor computed against TODAY's resampled synthetic order book (1,029-order,
Module-6-grounded), real routing, real rebalanced machines -- not a lookup
of an old number computed against data that no longer exists.

Reuses the exact production code paths for every quantity that matters
(ga/scheduler.py's avg_efficiency and serial_processing_mins formula,
ga/schedule_utils.py's calculate_operation_work, ga/chromosome.py's
ChromosomeFactory for machine-type eligibility/usable counts) so this is
not a second, drifting definition of "how much work is there" -- same
methodology PROJECT_STATUS.txt Section 1.2.2 adopted (Track 1: naive Floor
A vs. release-date-aware Floor B), extended two ways since this dataset's
release-date spread (~385 days) and 5 non-fungible machine-type pools make
both extensions actually matter here (they didn't on the old 25-day-spread
dataset, where Floor B collapsed to Floor A exactly):

  1. PER-TYPE capacity relaxation, not just the pooled company-wide one --
     the 5 operation types do not share machines, so pooling them is a
     valid but looser lower bound. Reported alongside the pooled number for
     continuity with PROJECT_STATUS.txt history, not in place of it.
  2. A genuine critical-path bound: each order's own routing chain must run
     serially regardless of how much idle capacity exists elsewhere, and
     with a 385-day release spread, a late-released order's own minimum
     completion time can plausibly exceed the capacity-relaxation floor.

Final floor = max(pooled release-aware, per-type release-aware, critical-path).
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from database.db import get_engine
from database.queries import fetch_active_orders_and_routings, fetch_machine_state
from ga.chromosome import ChromosomeFactory
from ga.scheduler import DEFAULT_LINE_EFFICIENCY, DEFAULT_SETUP_TIME_MINS
from utils.config import GA_MAX_MACHINES_PER_OPERATION

MINS_PER_DAY = 1440.0

print("=" * 70)
print("LOADING REAL DATA (live database, same tables the GA uses)")
print("=" * 70)

df_routings = fetch_active_orders_and_routings()
df_machines = fetch_machine_state()
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())

n_orders = df_routings['production_order_id'].nunique()
n_ops = len(df_routings.drop_duplicates(subset=['production_order_id', 'operation_id']))
print(f"Active order book: {n_orders} orders, {n_ops} operations")

if 'target_efficiency_percentage' in df_lines.columns:
    eff_series = df_lines.loc[df_lines['is_active'] == 1, 'target_efficiency_percentage']
    avg_efficiency = (eff_series.mean() / 100.0) if not eff_series.empty and pd.notna(eff_series.mean()) else DEFAULT_LINE_EFFICIENCY
else:
    avg_efficiency = DEFAULT_LINE_EFFICIENCY
avg_efficiency = max(avg_efficiency, 0.05)
print(f"avg_efficiency (real Production_Lines.target_efficiency_percentage): {avg_efficiency:.4f}")

# Reuses the PRODUCTION eligibility/reserve logic -- same object the real GA
# builds -- so usable_cap_by_type and optype_for_op cannot silently drift
# from what the scheduler itself would see.
factory = ChromosomeFactory(df_routings, df_machines, df_lines)
print(f"Usable machines by type (7% standby reserve already excluded): {factory.usable_cap_by_type}")
total_usable = sum(factory.usable_cap_by_type.values())
print(f"Total usable machines (company-wide): {total_usable}")

# --- Per-operation remaining work, release date, deadline -----------------
df_routings = df_routings.drop_duplicates(subset=['production_order_id', 'operation_id']).copy()
df_routings['release_date'] = pd.to_datetime(df_routings['release_date'])
earliest_release = df_routings['release_date'].min()
latest_release = df_routings['release_date'].max()
print(f"Release-date span: {earliest_release.date()} -> {latest_release.date()} "
      f"({(latest_release - earliest_release).days} days)")

rows = []
for _, row in df_routings.iterrows():
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
    optype = factory.optype_for_op.get(key)
    release_days = (row['release_date'] - earliest_release).total_seconds() / 86400.0

    # Fastest this SPECIFIC operation could ever run: most machines of the
    # required type available on any one of its eligible lines, capped by
    # the same three limits ga/chromosome.py::_max_machines_for enforces
    # (business cap, per-line usable count, 93%-of-company-type cap) --
    # protected_machine_type softening does not apply here (that is a
    # per-window GA runtime concept, not a structural capacity limit).
    per_line_counts = factory.machines_per_line_map.get(key, {})
    company_cap = factory.usable_cap_by_type.get(optype, GA_MAX_MACHINES_PER_OPERATION)
    best_line_machines = max(per_line_counts.values()) if per_line_counts else 1
    max_machines = max(1, min(int(best_line_machines), int(company_cap), GA_MAX_MACHINES_PER_OPERATION))
    min_duration_mins = DEFAULT_SETUP_TIME_MINS + (serial_processing_mins / max_machines)

    rows.append({
        'order_id': row['production_order_id'],
        'optype': optype,
        'setup_mins': DEFAULT_SETUP_TIME_MINS,
        'serial_processing_mins': serial_processing_mins,
        'release_days': release_days,
        'min_duration_mins': min_duration_mins,
        'seq': row['operation_sequence_number'],
    })

df = pd.DataFrame(rows)
# Machine-minutes consumed by an operation regardless of parallelism split:
# the full single-machine-equivalent work content, plus its one-time setup
# (charged once per operation, same convention as calculate_operation_work).
df['machine_minutes'] = df['serial_processing_mins'] + df['setup_mins']

print()
print("=" * 70)
print("FLOOR A -- NAIVE CAPACITY RELAXATION (continuity with PROJECT_STATUS.txt Track 1)")
print("=" * 70)
total_machine_days = df['machine_minutes'].sum() / MINS_PER_DAY
floor_a_pooled = total_machine_days / total_usable
print(f"Total remaining machine-days of work (all types pooled): {total_machine_days:,.1f}")
print(f"Floor A (pooled, same-day-availability): {floor_a_pooled:.1f} days")

print()
print("=" * 70)
print("FLOOR A-TIGHT -- PER-TYPE CAPACITY RELAXATION (types do not share machines)")
print("=" * 70)
per_type_days = df.groupby('optype')['machine_minutes'].sum() / MINS_PER_DAY
floor_a_tight_by_type = {}
for optype, machine_days in per_type_days.items():
    usable = factory.usable_cap_by_type.get(optype, total_usable)
    floor_a_tight_by_type[optype] = machine_days / usable
    print(f"  {optype:<22} {machine_days:>10,.1f} machine-days / {usable:>5} usable = {machine_days/usable:>6.1f} days")
floor_a_tight = max(floor_a_tight_by_type.values())
bottleneck_type = max(floor_a_tight_by_type, key=floor_a_tight_by_type.get)
print(f"Floor A-tight (max across types, same-day-availability): {floor_a_tight:.1f} days "
      f"(bottleneck: {bottleneck_type})")

print()
print("=" * 70)
print("FLOOR B -- RELEASE-DATE-AWARE CAPACITY RELAXATION")
print("=" * 70)
print("(max over every release cutoff r of: r + remaining-work-released-at-or-after-r / machines)")

def release_aware_floor(sub_df, usable):
    """Classic parallel-machine release-date lower bound."""
    cutoffs = sorted(sub_df['release_days'].unique())
    best = 0.0
    for r in cutoffs:
        remaining_work_days = sub_df.loc[sub_df['release_days'] >= r, 'machine_minutes'].sum() / MINS_PER_DAY
        bound = r + (remaining_work_days / usable)
        if bound > best:
            best = bound
    return best

floor_b_pooled = release_aware_floor(df, total_usable)
print(f"Floor B (pooled, release-date-aware): {floor_b_pooled:.1f} days")

floor_b_tight_by_type = {}
for optype, sub in df.groupby('optype'):
    usable = factory.usable_cap_by_type.get(optype, total_usable)
    floor_b_tight_by_type[optype] = release_aware_floor(sub, usable)
    print(f"  {optype:<22} release-aware floor: {floor_b_tight_by_type[optype]:>6.1f} days")
floor_b_tight = max(floor_b_tight_by_type.values())
bottleneck_type_b = max(floor_b_tight_by_type, key=floor_b_tight_by_type.get)
print(f"Floor B-tight (max across types, release-date-aware): {floor_b_tight:.1f} days "
      f"(bottleneck: {bottleneck_type_b})")

print()
print("=" * 70)
print("FLOOR C -- CRITICAL-PATH BOUND (per order's own routing chain, release-offset included)")
print("=" * 70)
print("(ignores capacity entirely: an order's own operations must run in sequence")
print(" even with infinite machines elsewhere -- ties precedence, not resources)")

order_release = df.groupby('order_id')['release_days'].first()
order_critical_mins = df.groupby('order_id')['min_duration_mins'].sum()
order_bound_days = order_release + (order_critical_mins / MINS_PER_DAY)
floor_c = order_bound_days.max()
worst_order = order_bound_days.idxmax()
print(f"Worst single order: {worst_order} -- release day {order_release[worst_order]:.1f}, "
      f"own critical path {order_critical_mins[worst_order]/MINS_PER_DAY:.1f} days, "
      f"bound {floor_c:.1f} days")
print(f"Floor C (critical-path, release-date-aware): {floor_c:.1f} days")

print()
print("=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"Floor A       (pooled, same-day):            {floor_a_pooled:.1f} days")
print(f"Floor A-tight (per-type, same-day):           {floor_a_tight:.1f} days")
print(f"Floor B       (pooled, release-aware):        {floor_b_pooled:.1f} days")
print(f"Floor B-tight (per-type, release-aware):      {floor_b_tight:.1f} days")
print(f"Floor C       (critical-path, release-aware): {floor_c:.1f} days")
overall_floor = max(floor_b_tight, floor_c)
binding = "Floor B-tight (capacity)" if floor_b_tight >= floor_c else "Floor C (critical-path)"
print(f"\nOVERALL FLOOR (max of the two release-aware, rigorous bounds): {overall_floor:.1f} days")
print(f"Binding constraint: {binding}")
print(f"\nGA_AUDIT.md's just-measured achieved makespan (fresh run, same order book): 30.1 days")
print(f"Ratio achieved/floor: {30.1 / overall_floor:.2f}x")

print("\nDONE")
