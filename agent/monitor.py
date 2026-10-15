import os
import sys
import time
from sqlalchemy import text

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.logger import get_logger
from database.db import get_db_session
from agent.event_handler import BreakdownPersistenceError, ShopFloorEventHandler

logger = get_logger("factory_monitor", "agent.log")

class FactoryMonitor:
    def __init__(self):
        logger.info("Initializing Real-Time Factory Monitor...")
        # The monitor drives the SAME handler the dashboard does, so a
        # breakdown is triaged identically however it is detected. self.agent
        # is that handler's engine, not a second one — two engines would mean
        # two disconnected sets of manager priority overrides.
        self.handler = ShopFloorEventHandler()
        self.agent = self.handler.decision_engine
        self.machine_states = self._get_current_states()
        logger.info(f"Tracking {len(self.machine_states)} machines on the floor.")

    def _get_current_states(self):
        """Fetches the current status of all machines to establish a baseline."""
        session = next(get_db_session())
        try:
            # Machinery_Assets columns are asset_id / current_status — matches
            # database/queries.py and graph/edge_builder.py. (Previously this
            # queried machine_id/status, which don't exist on this table and
            # would raise a SQL error on every poll.)
            sql = text("SELECT asset_id, current_status FROM Machinery_Assets")
            result = session.execute(sql).fetchall()
            return {row[0]: row[1] for row in result}
        except Exception as e:
            logger.error(f"Database read error: {e}")
            return {}
        finally:
            session.close()

    def _handle_detected_breakdown(self, machine_id):
        logger.info(
            f"Assessing breakdown impact for machine {machine_id} via the fuzzy triage gate..."
        )
        try:
            self.handler.handle_machine_breakdown(machine_id, affected_line_id=None)
        except BreakdownPersistenceError as exc:
            # Scheduling against unchanged state can reuse the broken asset.
            # Keep the event visible and require persistence to be restored.
            logger.critical(
                f"Breakdown {machine_id} was not persisted ({exc}); "
                "rescheduling was not started against stale machine state."
            )
        except Exception as exc:
            logger.error(
                f"Post-persistence breakdown handling failed for machine {machine_id} "
                f"({exc}); falling back to a full reschedule for safety."
            )
            self.agent.trigger_rescheduling()

    def listen(self, poll_interval=5):
        """Continuously monitors for machine breakdowns."""
        logger.info(f"Monitor active. Scanning floor every {poll_interval} seconds... (Press Ctrl+C to stop)")
        
        try:
            while True:
                time.sleep(poll_interval)
                current_states = self._get_current_states()
                
                if not current_states:
                    continue # Skip this loop if DB is temporarily unreachable
                    
                newly_broken = []

                for machine_id, new_status in current_states.items():
                    old_status = self.machine_states.get(machine_id)

                    # Detect if a machine JUST broke down
                    if new_status == 'Breakdown' and old_status != 'Breakdown':
                        logger.warning(f"🚨 ALERT: Machine ID {machine_id} has suffered a Breakdown!")
                        newly_broken.append(machine_id)

                    # Update our internal tracker
                    self.machine_states[machine_id] = new_status

                # Route through the SAME fuzzy triage gate the event handler
                # uses, per broken machine.
                #
                # BUG FIX: this used to call trigger_rescheduling() directly,
                # which re-plans all 1,410 orders — roughly 30 minutes of
                # compute — for any breakdown, however trivial, and bypassed
                # agent/fuzzy_logic.py entirely. Two independent code paths
                # were reacting to the same event with completely different
                # policies depending only on whether the breakdown arrived via
                # the dashboard or via this poller.
                #
                # It also skipped the standby reserve: measured on this
                # factory, a single machine failing is absorbed with zero
                # impact because 531 idle-capable alternates and 39 spares
                # exist, so the gate returns NO_ACTION for all 208 affected
                # operations. The old path escalated the same event to a
                # factory-wide reschedule.
                for machine_id in newly_broken:
                    self._handle_detected_breakdown(machine_id)
                if newly_broken:
                    logger.info("Breakdown handling complete. Resuming monitoring...")
                    
        except KeyboardInterrupt:
            logger.info("Factory Monitor shut down manually.")

if __name__ == "__main__":
    print("Starting Real-Time Factory Monitor...")
    monitor = FactoryMonitor()
    monitor.listen()