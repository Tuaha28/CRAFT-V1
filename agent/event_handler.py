import sys
import os
import pandas as pd
from sqlalchemy import text

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.logger import get_logger
from database.db import get_db_session, get_engine
from agent.decision_engine import AIDecisionEngine
from agent.impact_assessor import assess_machine_breakdown, apply_local_patch
from agent.fuzzy_logic import Tier

logger = get_logger("event_handler", "agent.log")

class BreakdownPersistenceError(RuntimeError):
    """The machine state change did not reach durable database state."""


class ShopFloorEventHandler:
    """
    Handles real-time shop floor events, updates database states, 
    and triggers automated AI rescheduling via the AIDecisionEngine.
    """
    def __init__(self):
        logger.info("Initializing Shop Floor Event Handler...")
        self.decision_engine = AIDecisionEngine()

    def handle_machine_breakdown(self, machine_id: int, affected_line_id: int = None):
        """
        Triggered when a machine breaks down on the shop floor.
        Deactivates the machine, then runs the fuzzy impact gate instead of
        unconditionally re-running the whole factory schedule:
          - No affected orders / negligible impact -> log only, no-op.
          - Impact stays within an affected order's cushion -> LOCAL_PATCH:
            reassign just those specific operations (possibly via a single
            safe donor preemption from a lower-urgency order), leaving
            every other order's schedule untouched.
          - Impact would breach the cushion (or no alternate machine exists
            at all) -> FULL_RESCHEDULE: fall back to the full factory-wide
            GA pass, same as before.
        This replaces the previous behavior of calling trigger_rescheduling()
        for every breakdown regardless of how trivial it was.
        """
        # affected_line_id is informational only — the UPDATE below clears the
        # machine's line regardless, and the assessor works from asset_id. It
        # is optional because agent/monitor.py detects breakdowns by polling
        # Machinery_Assets and knows the asset without the line.
        where = f"on Line ID {affected_line_id}" if affected_line_id is not None else "(line not specified)"
        logger.warning(f"🚨 EVENT: Machine ID {machine_id} broke down {where}!")
        
        session = next(get_db_session())
        try:
            # 1. Update machinery asset state in database
            # Machinery_Assets columns are asset_id / current_status (see
            # database/queries.py) — this previously used machine_id/status,
            # which don't exist on this table and would fail at runtime.
            sql = text("""
                UPDATE Machinery_Assets 
                SET current_status = 'Breakdown', line_id = NULL 
                WHERE asset_id = :mid
            """)
            result = session.execute(sql, {'mid': machine_id})
            if result.rowcount != 1:
                raise LookupError(
                    f"Machine {machine_id} does not exist or was not uniquely updated")
            session.commit()
            logger.info(f"Machine {machine_id} marked as 'Breakdown' and unassigned from line.")
        except Exception as exc:
            session.rollback()
            logger.error(f"Failed to persist machine breakdown event: {exc}")
            raise BreakdownPersistenceError(
                f"Machine breakdown {machine_id} was not persisted; handling aborted"
            ) from exc
        finally:
            session.close()

        # 2. Assess impact per affected operation via the fuzzy triage gate.
        try:
            assessments = assess_machine_breakdown(
                machine_id, manager_forced_orders=self.decision_engine.manager_forced_orders
            )
        except Exception as e:
            logger.error(f"❌ Impact assessment failed ({e}) — falling back to a full reschedule for safety.")
            self.decision_engine.trigger_rescheduling()
            return

        if not assessments:
            logger.info(f"✅ Machine {machine_id} breakdown had no queued/running work on it — no action needed.")
            return

        needs_full_reschedule = False
        orders_needing_priority = []

        for a in assessments:
            logger.info(
                f"Impact assessment — Order {a['production_order_id']} op {a.get('operation_id')}: "
                f"tier={a['tier']} | {a['reason']}"
            )
            if a['tier'] == Tier.NO_ACTION.value:
                continue
            elif a['tier'] == Tier.LOCAL_PATCH.value:
                success = apply_local_patch(a)
                if not success:
                    logger.warning(f"Local patch failed for order {a['production_order_id']} — escalating this order to full reschedule.")
                    needs_full_reschedule = True
                    orders_needing_priority.append(a['production_order_id'])
            else:  # FULL_RESCHEDULE
                needs_full_reschedule = True
                orders_needing_priority.append(a['production_order_id'])

        if needs_full_reschedule:
            logger.info(
                f"Escalating to full factory-wide reschedule for {len(set(orders_needing_priority))} "
                f"order(s) that couldn't be safely locally patched..."
            )
            self.decision_engine.trigger_rescheduling(manager_prioritized_orders=orders_needing_priority)
            logger.info("✅ Full rescheduling completed following machine breakdown.")
        else:
            logger.info(f"✅ Machine {machine_id} breakdown fully absorbed via local patch(es) — no factory-wide reschedule needed.")

    def handle_rush_order_insertion(self, order_id: str):
        """
        Triggered when a manager inserts a high-priority rush order.
        Registers a PERSISTENT priority override (survives future
        disruptions too, not just this one reschedule) and forces the AI
        scheduler to optimize with this order prioritized right now.
        """
        logger.info(f"🚨 EVENT: Rush order inserted for Production Order ID: {order_id}")
        
        self.decision_engine.set_manager_priority(order_id)
        self.decision_engine.trigger_rescheduling()
        logger.info(f"✅ Schedule re-optimized to prioritize rush order {order_id}.")

    def handle_material_shortage(self, material_name: str, affected_order_id: str):
        """
        Triggered when warehouse flags a fabric or trim shortage.
        Logs the bottleneck and adjusts schedule buffer times.
        """
        logger.warning(f"🚨 EVENT: Material shortage reported ({material_name}) for Order ID {affected_order_id}!")
        
        # In a full deployment, this could flag the order or add penalty weight.
        # Here we trigger a rescheduling cycle to account for delayed start times.
        self.decision_engine.trigger_rescheduling()
        logger.info(f"✅ Schedule re-balanced around material shortage for Order {affected_order_id}.")

if __name__ == "__main__":
    print("Testing Shop Floor Event Handler...")
    handler = ShopFloorEventHandler()
    print("[OK] Event Handler initialized successfully.")