"""
Real, MUTATING disruption tests against the disposable clone DB
(garment_fyp_db_postmortem_test -- see scratch/clone_db_for_disruption_test.py).
Run with DB_NAME already pointed at the clone (set by the launching shell,
BEFORE this process starts, since utils.config reads it at import time).

Part A: confirm the GA_AGENT_POSTMORTEM.md NO_ACTION finding against the
REAL mutating agent/event_handler.py::handle_machine_breakdown path (not
just the read-only assess_machine_breakdown used before), on a machine
untouched by anything else in this script.

Part B: deliberately construct scarcity for the scarcest machine type
(Tubular Sleeve Join) -- break most of its scheduled-capacity AND standby-
reserve machines -- specifically to try to force donor preemption to fire
for real, then run the REAL mutating handler on the surviving scenario and
confirm via direct before/after DB queries what actually got written.
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

assert 'postmortem_test' in DB_NAME, f"REFUSING to run mutating test against non-clone DB_NAME={DB_NAME}"
print(f"Confirmed running against disposable clone: {DB_NAME}")

engine = get_engine()

# ---------------------------------------------------------------------------
# PART A: confirm the NO_ACTION finding via the REAL mutating handler
# ---------------------------------------------------------------------------
print()
print("=" * 70)
print("PART A: real mutating handle_machine_breakdown, untouched machine")
print("=" * 70)

query = text("""
    SELECT asset_id, COUNT(*) AS n_affected
    FROM Job_Shop_Schedules
    WHERE execution_status IN ('Scheduled', 'Running')
    GROUP BY asset_id ORDER BY n_affected DESC LIMIT 1
""")
target_a = int(pd.read_sql(query, engine).iloc[0]['asset_id'])
print(f"Target machine (untouched, real busiest): {target_a}")

before_status = pd.read_sql(text(
    "SELECT current_status, line_id FROM Machinery_Assets WHERE asset_id = :aid"
), engine, params={'aid': target_a})
before_ops = pd.read_sql(text(
    "SELECT production_order_id, operation_id, asset_id, execution_status "
    "FROM Job_Shop_Schedules WHERE asset_id = :aid AND execution_status IN ('Scheduled','Running')"
), engine, params={'aid': target_a})
print(f"Before: Machinery_Assets.current_status={before_status.iloc[0]['current_status']}, "
      f"{len(before_ops)} real Job_Shop_Schedules rows on this asset")

handler = ShopFloorEventHandler()
handler.handle_machine_breakdown(target_a)

after_status = pd.read_sql(text(
    "SELECT current_status, line_id FROM Machinery_Assets WHERE asset_id = :aid"
), engine, params={'aid': target_a})
after_ops_same_asset = pd.read_sql(text(
    "SELECT production_order_id, operation_id, asset_id, execution_status "
    "FROM Job_Shop_Schedules WHERE asset_id = :aid AND execution_status IN ('Scheduled','Running')"
), engine, params={'aid': target_a})
if len(before_ops):
    keys = [f"{o}_{p}" for o, p in zip(before_ops['production_order_id'], before_ops['operation_id'])]
    moved = pd.read_sql(text(
        "SELECT production_order_id, operation_id, asset_id, execution_status FROM Job_Shop_Schedules "
        "WHERE CONCAT(production_order_id, '_', operation_id) IN :keys"
    ).bindparams(bindparam('keys', expanding=True)), engine, params={'keys': keys})
else:
    moved = pd.DataFrame()

print(f"\nAfter REAL handle_machine_breakdown({target_a}):")
print(f"  Machinery_Assets.current_status: {before_status.iloc[0]['current_status']} -> {after_status.iloc[0]['current_status']}")
print(f"  Real DB rows STILL pointing at asset {target_a}: {len(after_ops_same_asset)} of the original {len(before_ops)}")
if len(moved):
    reassigned = moved[moved['asset_id'] != target_a]
    print(f"  Of the {len(before_ops)} originally-affected operations, {len(reassigned)} were REASSIGNED "
          f"to a different asset_id by this real run; {len(before_ops) - len(reassigned)} still show "
          f"asset_id={target_a} in Job_Shop_Schedules.")
    if len(reassigned):
        print(reassigned.to_string(index=False))
    else:
        print("  CONFIRMED: 0 real reassignments written -- matches the NO_ACTION-dominant read-only "
              "finding, but ALSO reveals NO_ACTION performs no DB write at all: these operations' rows "
              "still reference the now-Breakdown machine, unchanged.")

# ---------------------------------------------------------------------------
# PART B: force scarcity on Tubular Sleeve Join, try to trigger donor
# preemption for real
# ---------------------------------------------------------------------------
print()
print("=" * 70)
print("PART B: constructing scarcity -- Tubular Sleeve Join")
print("=" * 70)

# Find TSJ operations belonging to orders with REAL, meaningful positive
# slack (not already-overdue) -- donor preemption requires a donor with
# STRICTLY LOWER urgency than the affected order, which is structurally
# impossible if the affected order is already urgency-saturated (slack
# <= 3 days scores urgency=1.0, per agent/fuzzy_logic.py).
candidates_q = text("""
    SELECT jss.production_order_id, jss.operation_id, jss.asset_id,
           po.scheduled_end_date,
           DATEDIFF(po.scheduled_end_date, NOW()) AS slack_days
    FROM Job_Shop_Schedules jss
    JOIN Machine_Capabilities mc ON mc.asset_id = jss.asset_id
    JOIN Production_Orders po ON po.production_order_id = jss.production_order_id
    WHERE jss.execution_status IN ('Scheduled', 'Running')
      AND mc.operation_type = 'tubular sleeve join'
      AND DATEDIFF(po.scheduled_end_date, NOW()) BETWEEN 8 AND 18
    ORDER BY slack_days DESC
    LIMIT 20
""")
slack_candidates = pd.read_sql(candidates_q, engine)
print(f"TSJ operations with real 8-18 day slack (donor-preemption-eligible affected orders): "
      f"{len(slack_candidates)}")
print(slack_candidates.head(10).to_string(index=False))

if slack_candidates.empty:
    print("No TSJ operation has an order with 8-18 days of real slack -- donor preemption cannot "
          "be tested this way; every TSJ order is either already overdue or has very little/very much "
          "slack. Reporting this as the real finding rather than forcing a scenario.")
else:
    target_asset = int(slack_candidates.iloc[0]['asset_id'])
    print(f"\nTarget TSJ machine for the stress scenario: {target_asset} "
          f"(carries an order with {slack_candidates.iloc[0]['slack_days']} days real slack)")

    # All TSJ asset_ids, so we know exactly what to spare vs break.
    tsj_assets = pd.read_sql(text(
        "SELECT ma.asset_id FROM Machinery_Assets ma JOIN Machine_Capabilities mc "
        "ON mc.asset_id = ma.asset_id WHERE mc.operation_type = 'tubular sleeve join'"
    ), engine)['asset_id'].tolist()
    print(f"Total real Tubular Sleeve Join machines (company-wide): {len(tsj_assets)}")

    # Spare only the target + 4 others (kept Active, whatever their real
    # status/committed schedule already is) -- everyone else in this type
    # goes to Breakdown, a severe simulated multi-machine outage.
    import random as _r
    _r.seed(7)
    others = [a for a in tsj_assets if a != target_asset]
    spared = set(_r.sample(others, min(4, len(others))))
    spared.add(target_asset)
    to_break = [a for a in tsj_assets if a not in spared]
    print(f"Sparing {len(spared)} TSJ machines (incl. target {target_asset}), "
          f"breaking {len(to_break)} others to construct severe scarcity.")

    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE Machinery_Assets SET current_status = 'Breakdown' WHERE asset_id IN :ids"
        ).bindparams(bindparam('ids', expanding=True)), {'ids': to_break})

    remaining_active = pd.read_sql(text(
        "SELECT COUNT(*) AS n FROM Machinery_Assets ma JOIN Machine_Capabilities mc "
        "ON mc.asset_id = ma.asset_id WHERE mc.operation_type = 'tubular sleeve join' "
        "AND ma.current_status = 'Active'"
    ), engine).iloc[0]['n']
    print(f"Real Machinery_Assets.current_status='Active' TSJ machines remaining after the simulated outage: "
          f"{remaining_active}")

    print(f"\n--- Read-only assess_machine_breakdown({target_asset}) under severe scarcity ---")
    eng_check = AIDecisionEngine()
    assessments = assess_machine_breakdown(target_asset, manager_forced_orders=eng_check.manager_forced_orders)
    tier_counts = {}
    donor_found = None
    for a in assessments:
        tier_counts[a['tier']] = tier_counts.get(a['tier'], 0) + 1
        if a.get('donor_plan') is not None and donor_found is None:
            donor_found = a
    print(f"Tier distribution under scarcity: {tier_counts} (of {len(assessments)} affected ops)")

    if donor_found:
        print(f"\nDONOR PREEMPTION FOUND (read-only check):")
        print(f"  order {donor_found['production_order_id']} op {donor_found['operation_id']}: "
              f"tier={donor_found['tier']}, candidate_asset={donor_found['candidate_asset_id']}")
        print(f"  donor_plan={donor_found['donor_plan']}")

        print(f"\n--- Running the REAL mutating handler on {target_asset} to confirm ---")
        before_donor_row = pd.read_sql(text(
            "SELECT production_order_id, operation_id, asset_id, planned_start_time, planned_end_time "
            "FROM Job_Shop_Schedules WHERE production_order_id = :oid AND operation_id = :op"
        ), engine, params={'oid': donor_found['donor_plan']['production_order_id'],
                            'op': donor_found['donor_plan']['operation_id']})
        before_affected_row = pd.read_sql(text(
            "SELECT production_order_id, operation_id, asset_id, planned_start_time, planned_end_time "
            "FROM Job_Shop_Schedules WHERE production_order_id = :oid AND operation_id = :op"
        ), engine, params={'oid': donor_found['production_order_id'], 'op': donor_found['operation_id']})

        handler2 = ShopFloorEventHandler()
        handler2.handle_machine_breakdown(target_asset)

        after_donor_row = pd.read_sql(text(
            "SELECT production_order_id, operation_id, asset_id, planned_start_time, planned_end_time "
            "FROM Job_Shop_Schedules WHERE production_order_id = :oid AND operation_id = :op"
        ), engine, params={'oid': donor_found['donor_plan']['production_order_id'],
                            'op': donor_found['donor_plan']['operation_id']})
        after_affected_row = pd.read_sql(text(
            "SELECT production_order_id, operation_id, asset_id, planned_start_time, planned_end_time "
            "FROM Job_Shop_Schedules WHERE production_order_id = :oid AND operation_id = :op"
        ), engine, params={'oid': donor_found['production_order_id'], 'op': donor_found['operation_id']})

        print("\nREAL DB CONFIRMATION:")
        print(f"  Affected op BEFORE: {before_affected_row.to_dict('records')}")
        print(f"  Affected op AFTER:  {after_affected_row.to_dict('records')}")
        print(f"  Donor op BEFORE:    {before_donor_row.to_dict('records')}")
        print(f"  Donor op AFTER:     {after_donor_row.to_dict('records')}")
    else:
        print("\nNo donor preemption in this scenario either -- reporting honestly. "
              "Full tier breakdown and reasons for the first few non-NO_ACTION assessments:")
        for a in assessments:
            if a['tier'] != 'NO_ACTION':
                print(f"  order {a['production_order_id']} op {a['operation_id']}: tier={a['tier']} "
                      f"impact_hours={a['impact_hours']} slack_days={a['slack_days']:.2f} "
                      f"candidate={a['candidate_asset_id']} reserve={a['from_standby_reserve']}")

print("\nDONE")
