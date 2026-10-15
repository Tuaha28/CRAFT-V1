"""
GA/agent post-mortem, Part 3: agent disruption handling, real numbers.

SCOPE DECISION, stated up front: agent/event_handler.py::handle_machine_breakdown
is the real top-level production entry point, but it MUTATES the live
database (UPDATE Machinery_Assets SET current_status='Breakdown', and
apply_local_patch's UPDATE Job_Shop_Schedules). Running that repeatedly
against several real machines as a "batch of simulated disruptions" would
corrupt the live database's real machine/schedule state as a side effect
of an audit. This script calls agent/impact_assessor.py::assess_machine_breakdown
directly instead -- the SAME real assessment logic (real DB queries, real
fuzzy-gate tier decisions, real candidate-slot search, real donor-preemption
logic), fully READ-ONLY. Where a LOCAL_PATCH would be applied,
apply_local_patch's real computed fields (candidate_asset_id,
candidate_start_time, donor_plan) are reported but apply_local_patch()
itself is NOT called, so nothing is written back. Flagged explicitly, not
quietly substituted.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import text

from database.db import get_engine
from agent.decision_engine import AIDecisionEngine
from agent.impact_assessor import assess_machine_breakdown
from agent.fuzzy_logic import Tier

print("=" * 70)
print("CANDIDATE MACHINES -- real query, ranked by real queued/running work")
print("=" * 70)

query = text("""
    SELECT asset_id, COUNT(*) AS n_affected
    FROM Job_Shop_Schedules
    WHERE execution_status IN ('Scheduled', 'Running')
    GROUP BY asset_id
    ORDER BY n_affected DESC
    LIMIT 15
""")
candidates = pd.read_sql(query, get_engine())
print(candidates.to_string(index=False))

eng = AIDecisionEngine()  # manager_forced_orders starts empty; real, no persisted state yet
print(f"\ndecision_engine.manager_forced_orders at start: {eng.manager_forced_orders}")

# --------------------------------------------------------------------------
# ONE DETAILED TRACE -- the top candidate machine
# --------------------------------------------------------------------------
trace_asset_id = int(candidates.iloc[0]['asset_id'])
print()
print("=" * 70)
print(f"DETAILED TRACE: machine {trace_asset_id} ({int(candidates.iloc[0]['n_affected'])} affected ops)")
print("=" * 70)

assessments = assess_machine_breakdown(trace_asset_id, manager_forced_orders=eng.manager_forced_orders)
print(f"Real assessment returned {len(assessments)} affected-operation records.")

tier_counts = {}
local_patch_example = None
for a in assessments:
    tier_counts[a['tier']] = tier_counts.get(a['tier'], 0) + 1
    if a['tier'] == Tier.LOCAL_PATCH.value and local_patch_example is None:
        local_patch_example = a

print(f"Tier breakdown for THIS machine's affected operations: {tier_counts}")

# Show a few individual assessments with full real reasoning.
for a in assessments[:5]:
    print(f"\n  order {a['production_order_id']} op {a['operation_id']}:")
    print(f"    slack_days={a['slack_days']:.2f}  urgency_score={a['urgency_score']:.3f}  "
          f"impact_hours={a['impact_hours']}")
    print(f"    tier={a['tier']}")
    print(f"    reason: {a['reason']}")
    print(f"    candidate_asset_id={a['candidate_asset_id']}  from_standby_reserve={a['from_standby_reserve']}  "
          f"donor_plan={a['donor_plan']}")

if local_patch_example:
    print(f"\n--- LOCAL_PATCH example: exactly what WOULD move, where (not applied) ---")
    a = local_patch_example
    print(f"Order {a['production_order_id']} op {a['operation_id']}: "
          f"machine {a['original_asset_id']} -> {a['candidate_asset_id']}"
          f"{' (standby reserve)' if a['from_standby_reserve'] else ''}")
    print(f"New start: {a['candidate_start_time']}, duration {a['duration']}")
    if a['donor_plan']:
        d = a['donor_plan']
        print(f"Donor preemption: order {d['production_order_id']} op {d['operation_id']} "
              f"(slack {d['donor_slack_days']:.1f}d) bumped {d['old_start']} -> {d['new_start']}")
    else:
        print("No donor preemption needed for this one.")
else:
    print("\nNo LOCAL_PATCH tier occurred for this machine's affected operations.")

# --------------------------------------------------------------------------
# BATCH -- real tier distribution + donor-preemption rate across several
# real machines
# --------------------------------------------------------------------------
print()
print("=" * 70)
print("BATCH: real tier distribution across multiple real machines")
print("=" * 70)

batch_asset_ids = [int(a) for a in candidates['asset_id'].tolist()]
all_assessments = []
per_machine_summary = []
for aid in batch_asset_ids:
    try:
        result = assess_machine_breakdown(aid, manager_forced_orders=eng.manager_forced_orders)
    except Exception as e:
        print(f"  machine {aid}: assessment FAILED ({e})")
        continue
    all_assessments.extend(result)
    counts = {}
    for a in result:
        counts[a['tier']] = counts.get(a['tier'], 0) + 1
    per_machine_summary.append((aid, len(result), counts))
    print(f"  machine {aid}: {len(result)} affected ops -> {counts}")

overall_tier_counts = {}
donor_used = 0
reserve_used = 0
for a in all_assessments:
    overall_tier_counts[a['tier']] = overall_tier_counts.get(a['tier'], 0) + 1
    if a.get('donor_plan') is not None:
        donor_used += 1
    if a.get('from_standby_reserve'):
        reserve_used += 1

total = len(all_assessments)
print()
print(f"TOTAL across {len(batch_asset_ids)} real machines: {total} affected operations assessed")
print(f"Real tier distribution: {overall_tier_counts}")
if total:
    for tier, count in overall_tier_counts.items():
        print(f"  {tier}: {count} ({100.0*count/total:.1f}%)")
print(f"Real donor-preemption rate: {donor_used}/{total} ({100.0*donor_used/total:.1f}% of all assessed ops)" if total else "n/a")
print(f"Real standby-reserve draw rate: {reserve_used}/{total} ({100.0*reserve_used/total:.1f}%)" if total else "n/a")

print("\nDONE")
