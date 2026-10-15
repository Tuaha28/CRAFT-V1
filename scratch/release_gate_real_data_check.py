"""
One-off verification (not part of the pipeline): confirm ga/release_gate.py
builds correctly and produces sane numbers against the REAL live order
book, not only synthetic test fixtures -- and confirm explicitly that the
norm is drawing from LIVE usable_cap_by_type (per the instruction to check
this directly), not a static pre-breakdown baseline. Drives the SAME
fast-forward loop ga/rolling_horizon.py uses, but with each admitted
order's completion faked immediately (no real GA/evaluate_schedule run --
that is the next planned step, the gated-buffer x release-gate ablation)
so this stays a cheap structural check, not a full expensive run.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime

import pandas as pd

from database.db import get_engine
from database.queries import fetch_active_orders_and_routings, fetch_machine_state
from ga.release_gate import ReleaseGate, MINS_PER_DAY

df_routings = fetch_active_orders_and_routings()
df_machines = fetch_machine_state()
df_lines = pd.read_sql(
    "SELECT line_id, department_id, line_number, target_efficiency_percentage, is_active "
    "FROM Production_Lines WHERE is_active = 1", get_engine())

ORIGIN = datetime.now()
print(f"ORIGIN: {ORIGIN.isoformat()}")

gate = ReleaseGate(df_routings, df_machines, df_lines, ORIGIN, norm_days=2.0, log_fn=print)

print()
print("=" * 70)
print("NORM SOURCE -- confirm it reads LIVE usable_cap_by_type, not static")
print("=" * 70)
for optype, usable in gate.factory.usable_cap_by_type.items():
    norm_days_check = gate.norm_minutes_by_pool[optype] / MINS_PER_DAY / usable if usable else 0
    print(f"  {optype:<22} live usable={usable:>4}  norm={gate.norm_minutes_by_pool[optype]/MINS_PER_DAY:>8.1f} machine-days "
          f"({norm_days_check:.2f} days/machine, expect 2.00)")

print()
print("=" * 70)
print("POOL")
print("=" * 70)
print(f"Total orders pooled: {gate.pool_size()}")
print(f"Infeasible orders reported: {len(gate.infeasible_orders)}")
for r in gate.infeasible_orders[:10]:
    print(f"  {r}")
if len(gate.infeasible_orders) > 10:
    print(f"  ... and {len(gate.infeasible_orders) - 10} more")

print()
print("=" * 70)
print("FIRST FEW ADMISSION ROUNDS (fast-forward loop, completions faked)")
print("=" * 70)
gate_time_mins = 0.0
for round_no in range(1, 11):
    if not gate.has_pending():
        print(f"Pool drained after {round_no - 1} rounds.")
        break
    admitted = gate.admit_next_batch(max_batch=300, current_time_mins=gate_time_mins)
    if not admitted:
        next_release = gate.next_release_mins()
        if next_release is not None and next_release > gate_time_mins:
            gate_time_mins = next_release
        else:
            gate_time_mins += gate.starvation_mins + 1.0
        print(f"  round {round_no}: nothing admitted, fast-forwarding to day {gate_time_mins/MINS_PER_DAY:.1f}")
        continue
    print(f"  round {round_no}: day {gate_time_mins/MINS_PER_DAY:.1f} -- admitted {len(admitted)} orders, "
          f"{gate.pool_size()} remaining in pool")
    gate.mark_completions({oid: gate_time_mins for oid in admitted})

print("\nDONE")
