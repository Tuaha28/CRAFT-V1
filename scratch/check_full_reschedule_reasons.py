import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.config import DB_NAME
from agent.impact_assessor import assess_machine_breakdown
from agent.decision_engine import AIDecisionEngine

assert 'postmortem_test' in DB_NAME
eng = AIDecisionEngine()
assessments = assess_machine_breakdown(2576, manager_forced_orders=eng.manager_forced_orders)
for a in assessments:
    if a['tier'] == 'FULL_RESCHEDULE':
        print(f"order {a['production_order_id']} op {a['operation_id']}: slack={a['slack_days']:.2f}d "
              f"impact_hours={a['impact_hours']} candidate={a['candidate_asset_id']} reason={a['reason']}")
