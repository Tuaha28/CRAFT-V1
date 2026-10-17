"""Read-only post-generation audit for a controlled operating-regime staging DB."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mysql.connector
from dotenv import load_dotenv
from data_generation.seed_database_fixed import db_config


def scalar(cursor, sql):
    cursor.execute(sql)
    return cursor.fetchone()[0]


def schedule_fingerprint(cursor, fields):
    cursor.execute(f"SELECT {','.join(fields)} FROM Job_Shop_Schedules ORDER BY production_order_id,operation_id,lot_index")
    digest = sha256()
    rows = 0
    for row in cursor:
        digest.update("|".join("" if v is None else str(v) for v in row).encode("utf-8"))
        digest.update(b"\n")
        rows += 1
    return {"rows": rows, "sha256": digest.hexdigest()}


def audit(database: str) -> dict:
    config = dict(db_config, database=database)
    connection = mysql.connector.connect(**config)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT payload FROM Dataset_Calibration WHERE singleton_id=1")
        calibration = json.loads(cursor.fetchone()[0])
        label_sql = """
            WITH a AS (
              SELECT production_order_id,operation_id,COUNT(*) lots_total
              FROM Job_Shop_Schedules GROUP BY production_order_id,operation_id
            ), o AS (
              SELECT production_order_id,operation_id,
                MAX(actual_end_time) latest_actual_end, MAX(planned_end_time) latest_planned_end,
                AVG(GREATEST(1,TIMESTAMPDIFF(SECOND,planned_start_time,planned_end_time)/60.0)) mean_lot_duration,
                COUNT(actual_end_time) lots_observed
              FROM Job_Shop_Schedules
              WHERE planned_start_time IS NOT NULL AND planned_end_time IS NOT NULL
                AND actual_start_time IS NOT NULL AND actual_end_time IS NOT NULL
              GROUP BY production_order_id,operation_id
            )
            SELECT o.production_order_id,o.operation_id,
              TIMESTAMPDIFF(SECOND,o.latest_planned_end,o.latest_actual_end)/60.0 lateness_mins,
              o.mean_lot_duration,o.lots_observed,a.lots_total,
              CASE WHEN o.lots_observed=a.lots_total AND TIMESTAMPDIFF(SECOND,o.latest_planned_end,o.latest_actual_end)/60.0 > .2*o.mean_lot_duration THEN 1 ELSE 0 END is_delayed,
              p.status
            FROM o JOIN a USING(production_order_id,operation_id)
            JOIN Production_Orders p USING(production_order_id)
        """
        cursor.execute(label_sql)
        labels = cursor.fetchall()
        valid = [row for row in labels if row[4] == row[5]]
        positive = sum(row[6] for row in valid)
        active = [row for row in valid if row[7] in ("Pending", "Processing", "Delayed")]
        active_positive = sum(row[6] for row in active)
        lateness = sorted(row[2] for row in valid)
        def quantile(p):
            if not lateness:
                return None
            return lateness[min(len(lateness)-1, round(p*(len(lateness)-1)))]
        checks = {
            "actual_before_planned_floor": scalar(cursor, "SELECT COUNT(*) FROM Job_Shop_Schedules WHERE actual_start_time < planned_start_time"),
            "negative_actual_duration": scalar(cursor, "SELECT COUNT(*) FROM Job_Shop_Schedules WHERE actual_end_time < actual_start_time"),
            "missing_actual_timing": scalar(cursor, "SELECT COUNT(*) FROM Job_Shop_Schedules WHERE actual_start_time IS NULL OR actual_end_time IS NULL"),
            "actual_precedence_violations": scalar(cursor, "SELECT COUNT(*) FROM (SELECT j.actual_start_time s,LAG(j.actual_end_time) OVER(PARTITION BY j.production_order_id,j.lot_index ORDER BY r.operation_sequence_number) p FROM Job_Shop_Schedules j JOIN Routing_Operations r ON r.operation_id=j.operation_id) q WHERE s<p"),
            "actual_machine_overlaps": scalar(cursor, "SELECT COUNT(*) FROM (SELECT actual_start_time s,MAX(actual_end_time) OVER(PARTITION BY asset_id ORDER BY actual_start_time,schedule_item_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) p FROM Job_Shop_Schedules) q WHERE s<p"),
            "actual_worker_overlaps": scalar(cursor, "SELECT COUNT(*) FROM (SELECT actual_start_time s,MAX(actual_end_time) OVER(PARTITION BY employee_id ORDER BY actual_start_time,schedule_item_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) p FROM Job_Shop_Schedules) q WHERE s<p"),
            "ownership_mismatch": scalar(cursor, "SELECT COUNT(*) FROM Job_Shop_Schedules j JOIN Order_Factory_Ownership o ON o.production_order_id=j.production_order_id JOIN Production_Lines l ON l.line_id=j.line_id JOIN Departments d ON d.department_id=l.department_id WHERE o.factory_id<>d.factory_id"),
        }
        cursor.execute("SELECT AVG(queue_delay_mins_at_dispatch),MAX(queue_delay_mins_at_dispatch),SUM(queue_delay_mins_at_dispatch>0),SUM(machine_breakdown_count_at_dispatch>0),AVG(machine_breakdown_count_at_dispatch),MAX(machine_breakdown_count_at_dispatch) FROM Job_Shop_Schedules")
        qmean,qmax,qrows,prior_rows,prior_mean,prior_max = cursor.fetchone()
        cursor.execute("SELECT execution_status,COUNT(*) FROM Job_Shop_Schedules GROUP BY execution_status ORDER BY execution_status")
        statuses = dict(cursor.fetchall())
        return {
            "audit_version": 1, "database": database, "audited_at": datetime.now(timezone.utc).isoformat(),
            "dataset_version": calibration.get("dataset_version"), "regime": calibration.get("operating_regime"),
            "counts": {"valid_operations": len(valid), "positive_operations": int(positive), "negative_operations": len(valid)-int(positive), "delay_prevalence": positive/len(valid), "active_valid_operations": len(active), "active_positive_operations": int(active_positive), "active_delay_prevalence": active_positive/len(active) if active else None},
            "lateness_minutes": {"min": lateness[0], "p50": quantile(.5), "p90": quantile(.9), "p99": quantile(.99), "max": lateness[-1]},
            "propagation_proxies": {"queue_delay_mean_minutes": float(qmean), "queue_delay_max_minutes": float(qmax), "rows_with_queue_delay": int(qrows), "rows_with_prior_breakdown": int(prior_rows), "prior_breakdown_mean": float(prior_mean), "prior_breakdown_max": int(prior_max), "dynamic_breakdown_repair_event_count": "not persisted; cannot reconstruct exactly"},
            "status_counts": statuses, "integrity_checks": checks,
            "planned_fingerprint": schedule_fingerprint(cursor, ["production_order_id","operation_id","lot_index","line_id","asset_id","employee_id","planned_start_time","planned_end_time","planned_work_segments"]),
            "actual_fingerprint": schedule_fingerprint(cursor, ["production_order_id","operation_id","lot_index","actual_start_time","actual_end_time","actual_work_segments"]),
        }
    finally:
        connection.close()


def main():
    load_dotenv(ROOT / ".env")
    parser=argparse.ArgumentParser()
    parser.add_argument("database")
    parser.add_argument("--output", required=True)
    args=parser.parse_args()
    result=audit(args.database)
    path=Path(args.output); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,indent=2,sort_keys=True),encoding="utf-8")
    print(json.dumps(result,indent=2,sort_keys=True))

if __name__ == "__main__": main()

