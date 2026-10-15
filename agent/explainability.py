import sys
import os
import json
import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.logger import get_logger
from utils.config import OUTPUT_DIR

logger = get_logger("explainability", "agent.log")

class ExplainabilityEngine:
    """
    Generates human-readable audit reports and XAI justifications 
    from proposed production schedules and GNN risk metrics.
    """
    def __init__(self):
        logger.info("Initializing Explainability (XAI) Engine...")
        self.schedule_path = os.path.join(OUTPUT_DIR, "proposed_schedule.json")

    def generate_executive_summary(self) -> dict:
        """
        Reads the latest proposed schedule JSON and compiles an executive summary
        explaining why operations were routed, re-routed, or buffered.
        """
        if not os.path.exists(self.schedule_path):
            logger.warning("No proposed schedule found to analyze.")
            return {"error": "Schedule file not found."}

        with open(self.schedule_path, "r") as f:
            schedule_data = json.load(f)

        total_operations = len(schedule_data)
        rerouted_ops = [op for op in schedule_data if "exceeded" in op.get('xai_reason', '')]
        # BUG FIX (FULL_CODEBASE_AUDIT.md Section 3): decision_engine.py's
        # changeover XAI text was rewritten to "...re-rigging charged to
        # it." (the multi-machine/multi-operator XAI upgrade); this match
        # was left on the original "...changeover buffer applied." wording,
        # so style_changeovers_applied silently read 0 on every real run.
        changeover_ops = [op for op in schedule_data if "re-rigging charged" in op.get('xai_reason', '')]
        late_orders = [op for op in schedule_data if "past its" in op.get('xai_reason', '')]

        summary = {
            "total_scheduled_operations": total_operations,
            "high_risk_reroutes_count": len(rerouted_ops),
            "style_changeovers_applied": len(changeover_ops),
            "deadline_warning_count": len(late_orders),
            "audit_trail": [
                {
                    "order_id": op['production_order_id'],
                    "operation_id": op['operation_id'],
                    "assigned_line": op['assigned_line_number'],
                    "reasoning": op['xai_reason']
                }
                for op in schedule_data
            ]
        }

        logger.info(f"Executive XAI summary generated for {total_operations} operations.")
        return summary

    def format_operation_card(self, order_id: str, operation_id: str) -> str:
        """
        Returns a specific natural language explanation for a single operation query.
        Useful for future chatbot / RAG tool calls.
        """
        if not os.path.exists(self.schedule_path):
            return "Schedule data unavailable."

        with open(self.schedule_path, "r") as f:
            schedule_data = json.load(f)

        for op in schedule_data:
            if str(op['production_order_id']) == str(order_id) and str(op['operation_id']) == str(operation_id):
                return (
                    f"Order {op['production_order_id']} (Op {op['operation_id']}) "
                    f"is assigned to {op['assigned_line_number']}. "
                    f"Start: {op['proposed_start_time']}, End: {op['cushioned_end_time']}. "
                    f"XAI Audit: {op['xai_reason']}"
                )

        return f"Operation {operation_id} for Order {order_id} was not found in the active schedule."

if __name__ == "__main__":
    print("Testing Explainability Engine...")
    xai = ExplainabilityEngine()
    summary = xai.generate_executive_summary()
    print(f"[OK] XAI Summary Generated. Total Ops Analyzed: {summary.get('total_scheduled_operations', 0)}")