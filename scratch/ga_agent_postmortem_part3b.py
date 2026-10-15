"""
Follow-up to Part 3: the first 15-machine sample (by raw affected-op count)
happened to land entirely on Bar-Tack Pocketing / Body Assembly / Seam Edge
Overlock -- all large-alternate-pool types -- and came back 100% NO_ACTION,
0% donor preemption. Checking whether the scarcest type (Tubular Sleeve
Join, only 300 live-usable machines per Section 9) surfaces a tighter case,
before concluding donor preemption is simply unreachable under current
real data.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import text

from database.db import get_engine
from agent.decision_engine import AIDecisionEngine
from agent.impact_assessor import assess_machine_breakdown
from agent.fuzzy_logic import Tier

query = text("""
    SELECT jss.asset_id, COUNT(*) AS n_affected, mc.operation_type
    FROM Job_Shop_Schedules jss
    JOIN Machine_Capabilities mc ON mc.asset_id = jss.asset_id
    WHERE jss.execution_status IN ('Scheduled', 'Running')
      AND mc.operation_type = 'tubular sleeve join'
    GROUP BY jss.asset_id, mc.operation_type
    ORDER BY n_affected DESC
    LIMIT 5
""")
candidates = pd.read_sql(query, get_engine())
print("Top Tubular Sleeve Join machines by affected-op count:")
print(candidates.to_string(index=False))

eng = AIDecisionEngine()
all_tiers = {}
donor_used = 0
reserve_used = 0
total = 0
for aid in candidates['asset_id'].tolist():
    aid = int(aid)
    result = assess_machine_breakdown(aid, manager_forced_orders=eng.manager_forced_orders)
    counts = {}
    for a in result:
        counts[a['tier']] = counts.get(a['tier'], 0) + 1
        all_tiers[a['tier']] = all_tiers.get(a['tier'], 0) + 1
        if a.get('donor_plan') is not None:
            donor_used += 1
        if a.get('from_standby_reserve'):
            reserve_used += 1
    total += len(result)
    print(f"machine {aid}: {len(result)} affected ops -> {counts}")
    non_no_action = [a for a in result if a['tier'] != 'NO_ACTION']
    for a in non_no_action[:3]:
        print(f"    order {a['production_order_id']} op {a['operation_id']}: tier={a['tier']} "
              f"impact_hours={a['impact_hours']} slack_days={a['slack_days']:.2f} "
              f"candidate_asset_id={a['candidate_asset_id']} from_reserve={a['from_standby_reserve']} "
              f"donor_plan={a['donor_plan']}")

print(f"\nTOTAL (Tubular Sleeve Join, {len(candidates)} machines): {total} affected ops")
print(f"Tier distribution: {all_tiers}")
print(f"Donor preemption: {donor_used}/{total}")
print(f"Reserve draw: {reserve_used}/{total}")
print("\nDONE")
