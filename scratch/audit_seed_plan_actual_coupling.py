"""Read-only replay proving whether planned resource selection depends on actual clocks."""

from collections import defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import get_engine


def main():
    engine = get_engine()
    with engine.connect() as conn:
        calibration = json.loads(conn.execute(text(
            "SELECT payload FROM Dataset_Calibration WHERE singleton_id=1"
        )).scalar_one())
    origin = datetime.fromisoformat(calibration["reference_time"]) - timedelta(days=15)

    rows = pd.read_sql("""
        SELECT j.schedule_item_id, j.production_order_id, j.operation_id,
               j.asset_id, j.employee_id, j.planned_end_time, j.actual_end_time,
               ma.factory_id, mc.operation_type
        FROM Job_Shop_Schedules j
        JOIN Machinery_Assets ma ON ma.asset_id=j.asset_id
        JOIN Routing_Operations ro ON ro.operation_id=j.operation_id
        JOIN Machine_Capabilities mc ON mc.asset_id=j.asset_id
        ORDER BY j.schedule_item_id
    """, engine)
    machines = pd.read_sql("""
        SELECT ma.asset_id, ma.factory_id, ma.current_status, mc.operation_type
        FROM Machinery_Assets ma
        JOIN Machine_Capabilities mc ON mc.asset_id=ma.asset_id
    """, engine)
    active = machines[machines.current_status.eq("Active")]
    pools = {
        (str(op), int(fid)): sorted(g.asset_id.astype(int).tolist())
        for (op, fid), g in active.groupby(["operation_type", "factory_id"])
    }

    planned_free = defaultdict(lambda: origin)
    actual_free = defaultdict(lambda: origin)
    chosen_not_plan_earliest = 0
    chosen_not_actual_earliest = 0
    examples = []
    for row in rows.itertuples(index=False):
        key = (str(row.operation_type), int(row.factory_id))
        pool = pools[key]
        plan_choice = min(pool, key=lambda a: (planned_free[a], a))
        actual_choice = min(pool, key=lambda a: (actual_free[a], a))
        chosen = int(row.asset_id)
        if chosen != plan_choice:
            chosen_not_plan_earliest += 1
            if len(examples) < 10:
                examples.append({
                    "schedule_item_id": int(row.schedule_item_id),
                    "chosen_asset": chosen,
                    "plan_earliest_asset": plan_choice,
                    "actual_earliest_asset": actual_choice,
                    "chosen_plan_free": planned_free[chosen].isoformat(),
                    "plan_earliest_free": planned_free[plan_choice].isoformat(),
                })
        if chosen != actual_choice:
            chosen_not_actual_earliest += 1
        planned_free[chosen] = pd.Timestamp(row.planned_end_time).to_pydatetime()
        actual_free[chosen] = pd.Timestamp(row.actual_end_time).to_pydatetime()

    result = {
        "database": engine.url.database,
        "rows_replayed": len(rows),
        "chosen_not_plan_earliest": chosen_not_plan_earliest,
        "chosen_not_plan_earliest_pct": 100.0 * chosen_not_plan_earliest / len(rows),
        "chosen_not_actual_earliest": chosen_not_actual_earliest,
        "examples": examples,
        "interpretation": (
            "A nonzero plan-earliest mismatch with zero actual-earliest mismatch "
            "proves the stored plan's machine choice followed actual availability."
        ),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
