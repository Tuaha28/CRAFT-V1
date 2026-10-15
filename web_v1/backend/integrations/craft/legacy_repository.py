"""Verified legacy schema reader. A single consistent READ ONLY transaction.

No Django, project DB engine, scheduler, or credentials are imported. The
caller supplies configuration. This repository has no persistence methods.
"""
from __future__ import annotations

import re

from .contracts import json_value


def verify_select_only_grants(grants):
    """Fail closed unless every effective grant is USAGE or SELECT only.

    Role grants and unrecognized grant syntax need a separate administrator
    review; they cannot be used as evidence of a SELECT-only account here.
    """
    statements = [str(value) for row in grants for value in row.values()]
    if not statements:
        raise PermissionError("No database grants were returned")
    select_on_source = False
    for statement in statements:
        match = re.fullmatch(
            r"GRANT (USAGE|SELECT) ON (\*\.\*|`?garment_fyp_db`?\.\*) TO .+",
            statement, flags=re.IGNORECASE,
        )
        if not match or "WITH GRANT OPTION" in statement.upper():
            raise PermissionError("Legacy account is not verified SELECT-only")
        if match.group(1).upper() == "SELECT":
            select_on_source = True
    if not select_on_source:
        raise PermissionError("Legacy account lacks verified SELECT access")


class LegacyExecutionRepository:
    def __init__(self, connection_factory):
        self.connection_factory = connection_factory

    @classmethod
    def from_config(cls, config):
        import pymysql

        if config.get("LEGACY_DB_NAME", "garment_fyp_db") != "garment_fyp_db":
            raise ValueError("Expected canonical garment_fyp_db")
        user = config.get("LEGACY_DB_USER")
        password = config.get("LEGACY_DB_PASSWORD")
        if not user or user.lower() == "root" or not password:
            raise PermissionError("An explicit non-root LEGACY_DB_USER/PASSWORD is required")
        return cls(lambda: pymysql.connect(
            host=config.get("LEGACY_DB_HOST", config.get("DB_HOST", "localhost")),
            port=int(config.get("LEGACY_DB_PORT", config.get("DB_PORT", 3306))),
            user=user,
            password=password,
            database="garment_fyp_db", connect_timeout=5, read_timeout=60,
            cursorclass=pymysql.cursors.DictCursor,
        ))

    def read_factory(self, factory_id, reference_time=None):
        import json

        factory_id = int(factory_id)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                # The first application query on this connection checks grants.
                # No data extraction or session setup occurs before this gate.
                # Bare SHOW GRANTS also includes mandatory roles in MySQL 8.
                cursor.execute("SHOW GRANTS")
                verify_select_only_grants(cursor.fetchall())
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")

                def read(sql, params=()):
                    if not sql.startswith("SELECT ") or ";" in sql:
                        raise ValueError("Only a single SELECT is permitted")
                    cursor.execute(sql, params)
                    return list(cursor.fetchall())

                identity = read("SELECT DATABASE() AS database_name, @@session.transaction_read_only AS read_only")[0]
                if identity != {"database_name": "garment_fyp_db", "read_only": 1}:
                    raise RuntimeError("Legacy database identity/read-only guard failed")
                engines = read("SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
                               "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'", ("garment_fyp_db",))
                if not engines or any(row["ENGINE"].upper() != "INNODB" for row in engines):
                    raise RuntimeError("Consistent InnoDB source snapshot is not established")
                raw_calibration = read("SELECT payload FROM dataset_calibration")
                if len(raw_calibration) != 1:
                    raise ValueError("Ambiguous dataset calibration identity")
                calibration = raw_calibration[0]["payload"]
                if isinstance(calibration, str):
                    calibration = json.loads(calibration)
                dataset = {"database": "garment_fyp_db", **{k: calibration.get(k) for k in
                           ("dataset_version", "reference_time", "timeline_generation_contract", "artifact_version")}}
                if not dataset["dataset_version"] or not dataset["reference_time"]:
                    raise ValueError("Missing calibrated dataset identity/clock")
                orders = read("SELECT p.production_order_id,p.product_id,p.target_quantity,p.scheduled_start_date,"
                              "p.scheduled_end_date,p.status,o.factory_id,po.delivery_deadline "
                              "FROM production_orders p JOIN order_factory_ownership o ON o.production_order_id=p.production_order_id "
                              "LEFT JOIN purchase_orders po ON po.po_id=p.po_id WHERE o.factory_id=%s", (factory_id,))
                operations = read("SELECT p.production_order_id,r.operation_id,r.operation_sequence_number,"
                                  "r.operation_name,r.standard_allowed_minutes,r.required_machine_type "
                                  "FROM production_orders p JOIN order_factory_ownership o ON o.production_order_id=p.production_order_id "
                                  "JOIN routing_operations r ON r.product_id=p.product_id WHERE o.factory_id=%s", (factory_id,))
                lots = read("SELECT j.schedule_item_id,j.production_order_id,j.operation_id,j.lot_index,j.lot_quantity,"
                            "j.line_id,j.asset_id,j.employee_id,j.batch_id,j.execution_status,j.actual_start_time,j.actual_end_time,"
                            "j.actual_work_segments,j.planned_start_time,j.planned_end_time,j.planned_work_segments "
                            "FROM job_shop_schedules j JOIN order_factory_ownership o ON o.production_order_id=j.production_order_id "
                            "WHERE o.factory_id=%s", (factory_id,))
                wip = read("SELECT w.wip_id,w.production_order_id,w.operation_id,w.line_id,w.pieces_completed,w.last_updated_time "
                           "FROM wip_tracking w JOIN order_factory_ownership o ON o.production_order_id=w.production_order_id "
                           "WHERE o.factory_id=%s", (factory_id,))
                resources = {
                    "machines": read("SELECT asset_id,factory_id,line_id,current_status FROM machinery_assets WHERE factory_id=%s", (factory_id,)),
                    "machine_capabilities": read("SELECT c.asset_id,c.operation_type FROM machine_capabilities c "
                                                 "JOIN machinery_assets m ON m.asset_id=c.asset_id WHERE m.factory_id=%s", (factory_id,)),
                    "workers": read("SELECT employee_id,factory_id,role,is_active FROM employees WHERE factory_id=%s", (factory_id,)),
                    "skills": read("SELECT s.employee_id,s.operation_type,s.efficiency_rating FROM operator_skill_matrix s "
                                   "JOIN employees e ON e.employee_id=s.employee_id WHERE e.factory_id=%s", (factory_id,)),
                    "work_schedules": read("SELECT w.schedule_id,w.employee_id,w.shift_date,w.start_time,w.end_time,w.is_on_leave "
                                            "FROM work_schedules w JOIN employees e ON e.employee_id=w.employee_id WHERE e.factory_id=%s", (factory_id,)),
                    "batches": read("SELECT b.batch_id,b.line_id,b.is_active,b.rotation_offset,b.rest_day_offset "
                                     "FROM employee_batches b JOIN production_lines l ON l.line_id=b.line_id "
                                     "JOIN departments d ON d.department_id=l.department_id WHERE d.factory_id=%s", (factory_id,)),
                    "batch_members": read("SELECT b.batch_id,b.employee_id FROM batch_members b JOIN employees e "
                                           "ON e.employee_id=b.employee_id WHERE e.factory_id=%s", (factory_id,)),
                    "lines": read("SELECT l.line_id,d.factory_id,l.is_active FROM production_lines l "
                                  "JOIN departments d ON d.department_id=l.department_id WHERE d.factory_id=%s", (factory_id,)),
                }
                return json_value({"factory_id": factory_id, "reference_time": reference_time or dataset["reference_time"],
                                   "dataset": dataset, "orders": orders, "operations": operations, "lots": lots,
                                   "wip": wip, "resources": resources})
        finally:
            connection.rollback()
            connection.close()
