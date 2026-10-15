"""
One-off verification (not part of the pipeline): confirm the live-capacity
fix to utils/machine_reserve.py::build_reserve behaves correctly against
the REAL live database, not just synthetic test fixtures. Reports the real
current_status distribution and how much (if any) usable_cap_by_type now
differs from the old static policy cap on today's real machine table.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.queries import fetch_machine_state
from utils.machine_reserve import build_reserve, usable_cap_for_type

df_machines = fetch_machine_state()
print(f"Total machine rows: {len(df_machines)}")
print("\nReal current_status distribution:")
print(df_machines.drop_duplicates(subset=['asset_id'])['current_status'].value_counts())

reserved, live_caps = build_reserve(df_machines)

print("\nLive usable_cap_by_type vs. the OLD static policy cap, per type:")
df = df_machines.dropna(subset=['operation_type']).copy()
df['_optype'] = df['operation_type'].astype(str).str.strip().str.lower()
for optype, group in df.groupby('_optype'):
    total = group['asset_id'].nunique()
    policy_cap = usable_cap_for_type(total)
    live_cap = live_caps.get(optype)
    delta = policy_cap - live_cap
    flag = "  <-- DIFFERS" if delta else ""
    print(f"  {optype:<22} total={total:>5}  policy_cap={policy_cap:>5}  live_cap={live_cap:>5}  down={delta:>3}{flag}")

print("\nDONE")
