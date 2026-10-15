"""
Real, MUTATING verification of the committed_by_asset staleness fix
(agent/impact_assessor.py::_book_tentative_slot / _release_tentative_slot).
Runs against the disposable clone (garment_fyp_db_postmortem_test).

Goal: construct real scarcity on Bar-Tack Pocketing (the same type
machine 2576 -- GA_AGENT_POSTMORTEM.md Section 5c -- already showed the
identical-candidate symptom on, unconstrained) severe enough that two or
more of machine 2576's real affected operations queue for the SAME
remaining alternate with genuinely nonzero impact, ideally landing at
least one pair in LOCAL_PATCH so the REAL mutating handler writes two
patches from the same breakdown event -- then confirm via direct query
that the two written rows do NOT overlap in time on the same asset.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import text, bindparam

from utils.config import DB_NAME
from database.db import get_engine
from agent.event_handler import ShopFloorEventHandler
from agent.impact_assessor import assess_machine_breakdown
from agent.decision_engine import AIDecisionEngine

assert 'postmortem_test' in DB_NAME, f"REFUSING to run against non-clone DB_NAME={DB_NAME}"
print(f"Running against disposable clone: {DB_NAME}")
engine = get_engine()

TARGET = 2576  # Bar-Tack Pocketing, the machine that showed the repeated-candidate symptom for real

before = pd.read_sql(text(
    "SELECT production_order_id, operation_id, asset_id, planned_start_time, planned_end_time "
    "FROM Job_Shop_Schedules WHERE asset_id = :aid AND execution_status IN ('Scheduled','Running')"
), engine, params={'aid': TARGET})
print(f"Machine {TARGET}: {len(before)} real affected rows.")
print(f"Duration spread (minutes): "
      f"{((before['planned_end_time'] - before['planned_start_time']).dt.total_seconds() / 60).describe().to_dict()}")

# Construct scarcity: break almost every OTHER Bar-Tack Pocketing machine
# (scheduled-capacity AND reserve -- both are just Machinery_Assets rows,
# reserve is a computed concept, not a DB column), sparing only a
# small handful plus the target itself, same method as the TSJ stress test.
btp_assets = pd.read_sql(text(
    "SELECT ma.asset_id FROM Machinery_Assets ma JOIN Machine_Capabilities mc "
    "ON mc.asset_id = ma.asset_id WHERE mc.operation_type = 'bar-tack pocketing'"
), engine)['asset_id'].tolist()
print(f"Total real Bar-Tack Pocketing machines: {len(btp_assets)}")

import random as _r
_r.seed(11)
others = [a for a in btp_assets if a != TARGET]
spared = set(_r.sample(others, 2))  # only 2 real alternates left, deliberately tight
spared.add(TARGET)
to_break = [a for a in btp_assets if a not in spared]
print(f"Sparing {len(spared)} (incl. target), breaking {len(to_break)} to force queueing.")

with engine.begin() as conn:
    conn.execute(text(
        "UPDATE Machinery_Assets SET current_status = 'Breakdown' WHERE asset_id IN :ids"
    ).bindparams(bindparam('ids', expanding=True)), {'ids': to_break})

remaining_active = pd.read_sql(text(
    "SELECT COUNT(*) AS n FROM Machinery_Assets ma JOIN Machine_Capabilities mc "
    "ON mc.asset_id = ma.asset_id WHERE mc.operation_type = 'bar-tack pocketing' "
    "AND ma.current_status = 'Active'"
), engine).iloc[0]['n']
print(f"Real Active Bar-Tack Pocketing machines remaining: {remaining_active}")

print("\n--- Read-only assess_machine_breakdown, POST-FIX ---")
eng = AIDecisionEngine()
assessments = assess_machine_breakdown(TARGET, manager_forced_orders=eng.manager_forced_orders)
tier_counts = {}
for a in assessments:
    tier_counts[a['tier']] = tier_counts.get(a['tier'], 0) + 1
print(f"Tier distribution under scarcity: {tier_counts} (of {len(assessments)} affected ops)")

# Look specifically for evidence the fix is working: among ops that got
# the SAME candidate_asset_id, do they have DIFFERENT candidate_start_time
# (i.e. queued one after another), rather than identical (double-booked)?
by_asset = {}
for a in assessments:
    if a['candidate_asset_id'] is not None:
        by_asset.setdefault(a['candidate_asset_id'], []).append(a)

collision_free = True
for asset_id, ops in by_asset.items():
    if len(ops) < 2:
        continue
    starts = [o['candidate_start_time'] for o in ops]
    print(f"\nAsset {asset_id} recommended for {len(ops)} affected ops:")
    for o in ops:
        print(f"  order {o['production_order_id']} op {o['operation_id']}: tier={o['tier']} "
              f"start={o['candidate_start_time']} impact_hours={o['impact_hours']}")
    if len(set(starts)) < len(starts):
        print(f"  *** COLLISION: {len(starts) - len(set(starts))} pair(s) share an identical start time ***")
        collision_free = False
    else:
        print(f"  No identical start times among these {len(ops)} -- fix is separating them correctly.")

local_patch_ops = [a for a in assessments if a['tier'] == 'LOCAL_PATCH']
print(f"\nLOCAL_PATCH count: {len(local_patch_ops)}")

if len(local_patch_ops) >= 2:
    print("\n--- Running the REAL mutating handler to confirm actual writes don't collide ---")
    targets = local_patch_ops[:2]
    for t in targets:
        print(f"  will patch: order {t['production_order_id']} op {t['operation_id']} -> "
              f"asset {t['candidate_asset_id']} @ {t['candidate_start_time']}")

    handler = ShopFloorEventHandler()
    handler.handle_machine_breakdown(TARGET)

    print("\nReal written rows for the two targeted operations, post-write:")
    for t in targets:
        row = pd.read_sql(text(
            "SELECT production_order_id, operation_id, asset_id, planned_start_time, planned_end_time "
            "FROM Job_Shop_Schedules WHERE production_order_id = :oid AND operation_id = :op "
            "AND asset_id != :orig"
        ), engine, params={'oid': t['production_order_id'], 'op': t['operation_id'], 'orig': TARGET})
        print(row.to_string(index=False) if not row.empty else "  (no row found off the original asset -- check manually)")

    # Direct overlap check: re-fetch the full real committed schedule for
    # whichever candidate asset(s) were actually used and scan for overlaps.
    for asset_id in set(t['candidate_asset_id'] for t in targets):
        sched = pd.read_sql(text(
            "SELECT production_order_id, operation_id, planned_start_time, planned_end_time "
            "FROM Job_Shop_Schedules WHERE asset_id = :aid AND execution_status IN ('Scheduled','Running') "
            "ORDER BY planned_start_time"
        ), engine, params={'aid': int(asset_id)})
        overlaps = 0
        for i in range(len(sched) - 1):
            if sched.iloc[i]['planned_end_time'] > sched.iloc[i + 1]['planned_start_time']:
                overlaps += 1
        print(f"Asset {asset_id}: {len(sched)} real committed rows post-write, {overlaps} overlapping consecutive pair(s).")
else:
    print("\nFewer than 2 LOCAL_PATCH verdicts in this scenario -- cannot exercise a real 2+ "
          "LOCAL_PATCH write from a single breakdown event this way. Reporting honestly, tier "
          f"breakdown was {tier_counts}.")

print("\nDONE")
