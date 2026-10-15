"""Read-only V2 integration preflight. Never import or execute the scheduler.

Run from the repository root with venv/Scripts/python.exe.
Only the report beside this script is written. Credentials are never emitted.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pymysql
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
SOURCES = [
    "database/db.py", "database/queries.py", "utils/config.py",
    "crafter/production/controller_v1.py", "crafter/repair/planning.py",
    "crafter/repair/solve.py", "crafter/repair/scoped_validation.py",
    "crafter/disruptions/benchmark.py", "crafter/evaluation/neutral_factory.py",
    "scratch/run_factory1_craft_frozen_v1.py",
    "scratch/run_factory1_window1_recovery_diagnostic.py",
    "scratch/audit_factory1_schedulable_workload_v1.py",
    "data_generation/seed_database_fixed.py",
    "web_v1/backend/config/settings.py", "web_v1/backend/api/views.py",
    "web_v1/frontend/src/features/SchedulePage.tsx",
    "crafter/contracts.py",
    "outputs/crafter/factory1_craft_workload_v1/optimizable_operations.json",
    "outputs/crafter/factory1_craft_workload_v1/workload_contract.json",
]


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def main():
    before = source_hashes()
    config = {**dotenv_values(ROOT / ".env"), **os.environ}
    connection = pymysql.connect(
        host=config.get("LEGACY_DB_HOST", config.get("DB_HOST", "localhost")),
        port=int(config.get("LEGACY_DB_PORT", config.get("DB_PORT", 3306))),
        user=config.get("LEGACY_DB_USER", config.get("DB_USER")),
        password=config.get("LEGACY_DB_PASSWORD", config.get("DB_PASS", "")),
        database="garment_fyp_db", connect_timeout=5, read_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")

            def read(sql, args=()):
                if not sql.lstrip().upper().startswith("SELECT ") or ";" in sql:
                    raise ValueError("Audit permits a single SELECT statement only")
                cursor.execute(sql, args)
                return cursor.fetchall()

            identity = read("SELECT DATABASE() AS database_name, VERSION() AS server_version, "
                            "@@session.transaction_read_only AS transaction_read_only")[0]
            if identity["database_name"] != "garment_fyp_db" or not identity["transaction_read_only"]:
                raise RuntimeError("Database identity/read-only guard failed")
            schema = read("SELECT table_name,column_name,data_type,is_nullable,column_key "
                          "FROM information_schema.columns WHERE table_schema=DATABASE() "
                          "ORDER BY table_name,ordinal_position")
            calibration = read("SELECT payload FROM dataset_calibration")[0]["payload"]
            calibration = json.loads(calibration) if isinstance(calibration, str) else calibration
            reference = datetime.fromisoformat(calibration["reference_time"])
            running = read("SELECT j.production_order_id,j.operation_id,j.lot_index,j.lot_quantity,"
                           "j.line_id,j.asset_id,j.employee_id,j.execution_status,j.actual_start_time,"
                           "j.actual_end_time,j.actual_work_segments,o.factory_id "
                           "FROM job_shop_schedules j JOIN order_factory_ownership o "
                           "ON o.production_order_id=j.production_order_id "
                           "WHERE j.actual_start_time<=%s AND j.actual_end_time>%s "
                           "ORDER BY o.factory_id,j.production_order_id,j.operation_id,j.lot_index",
                           (reference, reference))
            summaries = read("SELECT o.factory_id,j.production_order_id,j.operation_id,"
                             "COUNT(*) AS lot_rows,SUM(j.execution_status='Done') AS done_lots,"
                             "SUM(j.execution_status='Running') AS running_lots,"
                             "SUM(j.execution_status='Delayed') AS delayed_lots,"
                             "SUM(j.execution_status='Scheduled') AS scheduled_lots,"
                             "SUM(j.lot_quantity) AS lot_quantity_total,"
                             "SUM(CASE WHEN j.execution_status='Done' THEN j.lot_quantity ELSE 0 END) AS done_quantity,"
                             "MIN(j.actual_start_time) AS first_actual_start,"
                             "MAX(j.actual_end_time) AS aggregate_actual_end "
                             "FROM job_shop_schedules j JOIN order_factory_ownership o "
                             "ON o.production_order_id=j.production_order_id "
                             "GROUP BY o.factory_id,j.production_order_id,j.operation_id "
                             "HAVING SUM(j.actual_start_time<=%s AND j.actual_end_time>%s)>0 "
                             "ORDER BY o.factory_id,j.production_order_id,j.operation_id", (reference, reference))
            wip = read("SELECT production_order_id,operation_id,COUNT(*) AS wip_rows,"
                       "SUM(pieces_completed) AS completed_quantity,MIN(last_updated_time) AS first_update,"
                       "MAX(last_updated_time) AS last_update FROM wip_tracking "
                       "GROUP BY production_order_id,operation_id")
            wip_by_key = {(str(r["production_order_id"]), str(r["operation_id"])): r for r in wip}
            for row in summaries:
                key = str(row["production_order_id"]), str(row["operation_id"])
                row["operation_key"] = "_".join(key)
                row["wip"] = wip_by_key.get(key)
                row["wip_timestamp_after_reference"] = bool(row["wip"] and row["wip"]["first_update"] > reference)
            factory_counts = read("SELECT o.factory_id,COUNT(DISTINCT p.production_order_id) AS orders,"
                                  "COUNT(*) AS operation_instances FROM production_orders p "
                                  "JOIN order_factory_ownership o ON o.production_order_id=p.production_order_id "
                                  "JOIN routing_operations r ON r.product_id=p.product_id GROUP BY o.factory_id")
            app_database_exists = bool(read("SELECT schema_name FROM information_schema.schemata "
                                            "WHERE schema_name=%s", ("craft_web_db",)))
            schedule_statuses = read("SELECT execution_status,COUNT(*) AS rows_count FROM job_shop_schedules "
                                     "GROUP BY execution_status ORDER BY execution_status")
            frozen = json.loads((ROOT / "outputs/crafter/factory1_craft_workload_v1/optimizable_operations.json").read_text(encoding="utf-8"))
            frozen_keys = {r["operation_key"] for r in frozen["operations"]}
            conflicts = [r["operation_key"] for r in summaries
                         if r["factory_id"] == 1 and r["operation_key"] in frozen_keys]
            report = {
                "status": "BLOCKED_AT_EXECUTION_STATE_CONTRACT",
                "audited_at_utc": datetime.now(timezone.utc).isoformat(),
                "database": identity, "database_writes": 0,
                "craft_web_db_exists": app_database_exists,
                "reference_time": reference.isoformat(),
                "dataset_version": calibration.get("dataset_version"),
                "timeline_generation_contract": calibration.get("timeline_generation_contract"),
                "schema": schema, "factory_counts": factory_counts,
                "schedule_status_counts": schedule_statuses,
                "in_flight_lot_count": len(running),
                "in_flight_operation_count": len(summaries),
                "factory1_in_flight_operations_classified_optimizable": conflicts,
                "in_flight_lots": running, "in_flight_operation_summaries": summaries,
                "mixed_in_flight_and_scheduled_operations": [r["operation_key"] for r in summaries if r["scheduled_lots"]],
                "missing_wip_operations": [r["operation_key"] for r in summaries if not r["wip"]],
                "wip_after_reference_operations": [r["operation_key"] for r in summaries if r["wip_timestamp_after_reference"]],
                "scheduler_invoked": False,
                "live_integration_implemented": False,
                "source_hashes_before": before,
                "source_hashes_after": source_hashes(),
            }
    finally:
        connection.rollback()
        connection.close()
    report["inspected_sources_unchanged"] = report["source_hashes_before"] == report["source_hashes_after"]
    (OUT / "database_and_boundary_evidence.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [Path(__file__), OUT / "database_and_boundary_evidence.json", OUT / "integration_audit.md"]
                if p.exists()}
    (OUT / "artifact_hash_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verified = all(hashlib.sha256((OUT / name).read_bytes()).hexdigest() == digest
                   for name, digest in manifest.items())
    (OUT / "hash_verification.json").write_text(json.dumps({"all_hashes_verified": verified,
        "inspected_sources_unchanged": report["inspected_sources_unchanged"]}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ["status", "database", "reference_time", "craft_web_db_exists",
          "in_flight_lot_count", "in_flight_operation_count", "mixed_in_flight_and_scheduled_operations",
          "missing_wip_operations", "factory1_in_flight_operations_classified_optimizable",
          "inspected_sources_unchanged", "database_writes"]}, indent=2, default=str))


if __name__ == "__main__":
    main()
