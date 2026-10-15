"""
One-off diagnostic (not part of the pipeline): is grid.xls's early
54-day order burst a real, recurring pattern or a one-off data/export
artifact? Checked on the 1,029 source order-equivalent groups used to
calibrate the seeder's count (139 genuine PO groups plus 890
StyleId+Color+BuyMonth proxy groups), not raw rows. The seeder resamples
attributes independently; it does not preserve these source tuples -- see data_generation/seed_database_fixed.py and
DATA_REBUILD_LOG.md Module 6 for that grouping's own derivation.

Three checks, per instruction:
  1. Concentration -- is the burst a small number of PO numbers/companies
     (one bulk contract) or spread across many distinct orders?
  2. Garment-type mix -- does the burst's GarmentType distribution differ
     meaningfully from the tail's?
  3. End-of-window trend -- any upward order-start density near the end
     of the 399-day window suggesting a new cycle starting?
"""
import pandas as pd

df_grid = pd.read_excel('data_generation/grid.xls')

# Replicate the seeder's exact order-grouping logic (data_generation/seed_database_fixed.py)
_po_groups = df_grid.dropna(subset=['PO']).groupby('PO').agg(
    start_date=('StartDate', 'min'),
    company=('Company', lambda s: s.mode().iat[0] if not s.mode().empty else None),
    garment_type=('GarmentType', lambda s: s.mode().iat[0] if not s.mode().empty else None),
    n_rows=('StartDate', 'size'),
).reset_index()
_po_groups['order_key'] = 'PO:' + _po_groups['PO'].astype(str)
_po_groups['is_real_po'] = True

_no_po_rows = df_grid[df_grid['PO'].isna()]
_no_po_groups = _no_po_rows.groupby(['StyleId', 'Color', 'BuyMonth']).agg(
    start_date=('StartDate', 'min'),
    company=('Company', lambda s: s.mode().iat[0] if not s.mode().empty else None),
    garment_type=('GarmentType', lambda s: s.mode().iat[0] if not s.mode().empty else None),
    n_rows=('StartDate', 'size'),
).reset_index()
_no_po_groups['order_key'] = (
    'NOPO:' + _no_po_groups['StyleId'].astype(str) + '|' +
    _no_po_groups['Color'].astype(str) + '|' + _no_po_groups['BuyMonth'].astype(str)
)
_no_po_groups['is_real_po'] = False

orders = pd.concat([
    _po_groups[['order_key', 'start_date', 'company', 'garment_type', 'n_rows', 'is_real_po']],
    _no_po_groups[['order_key', 'start_date', 'company', 'garment_type', 'n_rows', 'is_real_po']],
], ignore_index=True)
orders = orders.dropna(subset=['start_date'])

print(f"Total real order-equivalent units with a start_date: {len(orders)} "
      f"(expected 1,029, some may lack a StartDate)")

min_date = orders['start_date'].min()
max_date = orders['start_date'].max()
span_days = (max_date - min_date).days
median_date = orders['start_date'].median()
burst_end = min_date + pd.Timedelta(days=54)

print(f"\nOrder-level date range: {min_date.date()} to {max_date.date()} ({span_days} days)")
print(f"Order-level median start: {median_date.date()} ({(median_date - min_date).days} days after min)")
print(f"Burst window (first 54 days): {min_date.date()} to {burst_end.date()}")

burst = orders[orders['start_date'] <= burst_end]
tail = orders[orders['start_date'] > burst_end]
print(f"\nBurst orders: {len(burst)} ({100*len(burst)/len(orders):.1f}%)")
print(f"Tail orders:  {len(tail)} ({100*len(tail)/len(orders):.1f}%)")

print("\n" + "="*70)
print("1. CONCENTRATION -- PO numbers / companies")
print("="*70)
print(f"Burst: {burst['order_key'].nunique()} distinct order keys, "
      f"{burst['company'].nunique()} distinct companies, "
      f"{burst['is_real_po'].sum()} with a real PO number")
print(f"Tail:  {tail['order_key'].nunique()} distinct order keys, "
      f"{tail['company'].nunique()} distinct companies, "
      f"{tail['is_real_po'].sum()} with a real PO number")

print("\nBurst company distribution (top 10):")
print(burst['company'].value_counts().head(10).to_string())
print(f"Burst top-1 company share: {100*burst['company'].value_counts().iloc[0]/len(burst):.1f}%"
      if len(burst) else "n/a")

print("\nTail company distribution (top 10):")
print(tail['company'].value_counts().head(10).to_string())
print(f"Tail top-1 company share: {100*tail['company'].value_counts().iloc[0]/len(tail):.1f}%"
      if len(tail) else "n/a")

print("\n" + "="*70)
print("2. GARMENT-TYPE MIX -- burst vs. tail")
print("="*70)
burst_mix = (burst['garment_type'].value_counts(normalize=True) * 100).round(1)
tail_mix = (tail['garment_type'].value_counts(normalize=True) * 100).round(1)
mix_compare = pd.DataFrame({'burst_pct': burst_mix, 'tail_pct': tail_mix}).fillna(0.0)
mix_compare['diff_pp'] = (mix_compare['burst_pct'] - mix_compare['tail_pct']).round(1)
print(mix_compare.sort_values('diff_pp', key=abs, ascending=False).to_string())

print("\n" + "="*70)
print("3. END-OF-WINDOW TREND -- order-start density over time")
print("="*70)
orders['month_bucket'] = orders['start_date'].dt.to_period('M')
monthly_counts = orders.groupby('month_bucket').size()
print(monthly_counts.to_string())

last_60_start = max_date - pd.Timedelta(days=60)
last_60 = orders[orders['start_date'] > last_60_start]
prior_stretch = orders[(orders['start_date'] > burst_end) & (orders['start_date'] <= last_60_start)]
prior_days = (last_60_start - burst_end).days
print(f"\nLast 60 days ({last_60_start.date()} to {max_date.date()}): "
      f"{len(last_60)} orders ({len(last_60)/60:.2f}/day)")
print(f"Prior mid-stretch ({burst_end.date()} to {last_60_start.date()}, {prior_days} days): "
      f"{len(prior_stretch)} orders ({len(prior_stretch)/prior_days:.2f}/day)"
      if prior_days > 0 else "n/a")

print("\nDONE")
