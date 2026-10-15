"""
Corrected follow-up to disruption_stress_test.py's Part A: that script's
before/after comparison joined on (production_order_id, operation_id) only,
but Job_Shop_Schedules is LOT-level (many rows share an operation_id), so
the join matched far more rows than intended and produced a nonsensical
negative "reassigned" count -- a bug in the diagnostic script, not in
production code. Re-checking directly and simply: how many real rows still
show asset_id=2310 (the machine Part A broke) right now, in the same clone
DB, after that real handle_machine_breakdown(2310) call already ran.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import text
from utils.config import DB_NAME
from database.db import get_engine

assert 'postmortem_test' in DB_NAME, f"refusing non-clone DB_NAME={DB_NAME}"
engine = get_engine()

still_there = pd.read_sql(text(
    "SELECT COUNT(*) AS n FROM Job_Shop_Schedules WHERE asset_id = 2310 "
    "AND execution_status IN ('Scheduled','Running')"
), engine).iloc[0]['n']
print(f"Real rows still pointing at asset 2310 (Scheduled/Running) right now: {still_there}")
print("(Part A found 36 real rows on this asset before the breakdown call; if NO_ACTION truly "
      "wrote nothing, this should still be 36.)")

status = pd.read_sql(text(
    "SELECT current_status FROM Machinery_Assets WHERE asset_id = 2310"
), engine).iloc[0]['current_status']
print(f"Machinery_Assets.current_status for 2310: {status}")

sample = pd.read_sql(text(
    "SELECT production_order_id, operation_id, asset_id, employee_id, execution_status, "
    "planned_start_time, planned_end_time FROM Job_Shop_Schedules WHERE asset_id = 2310 "
    "AND execution_status IN ('Scheduled','Running') LIMIT 5"
), engine)
print("\nSample rows still on the broken machine:")
print(sample.to_string(index=False))
