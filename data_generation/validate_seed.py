"""Read-only validation gate for v2 synthetic datasets."""
import json
from datetime import datetime, timedelta
from utils.shift_rotation import batch_available_at


def validate_seed(connection, *, expected_counts=None):
    cursor = connection.cursor(dictionary=True)
    checks = {}
    # Production defaults are company facts. Tiny lifecycle tests must declare
    # their smaller population explicitly; the staging runner never overrides.
    expected = {'employee_count': 9000, 'machine_count': 3000, 'factory_count': 6}
    if expected_counts is not None:
        if set(expected_counts) != set(expected) or any(type(v) is not int or v <= 0 for v in expected_counts.values()):
            raise ValueError('Explicit test population must supply positive employee/machine/factory counts')
        expected = dict(expected_counts)
    details = {}
    cursor.execute('SELECT DATABASE() database_name')
    database = cursor.fetchone()['database_name']
    cursor.execute("SELECT TABLE_NAME name FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
    # CASE-INSENSITIVE (found by probing a GOOD staging dataset).
    #
    # MySQL's lower_case_table_names is 1 or 2 on Windows, so CREATE TABLE
    # `Dataset_Calibration` is STORED as `dataset_calibration` and
    # information_schema reports it that way. An exact Python string
    # membership test therefore reported both artifacts missing on a dataset
    # that had just written them -- a FALSE NEGATIVE that fails a perfectly
    # good dataset.
    #
    # It survived the original probe because that ran against the known-BAD
    # staging database, where the artifacts really were absent and the check
    # was right for the wrong reason. Every SQL-side reader
    # (utils/factory_ownership.py, utils/derived_constants.py) was unaffected
    # throughout, because table references in SQL are resolved by the server
    # under the same setting -- so this was a gate-only defect, not a
    # runtime one.
    tables = {row['name'] for row in cursor.fetchall()}
    tables_ci = {name.lower() for name in tables}
    checks['missing_calibration_table'] = int('dataset_calibration' not in tables_ci)
    checks['missing_ownership_table'] = int('order_factory_ownership' not in tables_ci)
    calibration = None
    if not checks['missing_calibration_table']:
        cursor.execute('SELECT singleton_id,payload FROM Dataset_Calibration')
        rows = cursor.fetchall()
        try:
            if len(rows) != 1 or rows[0]['singleton_id'] != 1:
                raise ValueError('Expected one calibration singleton')
            from utils.derived_constants import validate_calibration
            calibration = validate_calibration(json.loads(rows[0]['payload']), database)
            checks['invalid_calibration'] = 0
        except (ValueError, TypeError, KeyError) as exc:
            checks['invalid_calibration'] = 1
            details['invalid_calibration'] = str(exc)
    cursor.execute('SELECT COUNT(*) n FROM Production_Orders')
    order_count = cursor.fetchone()['n']
    checks['empty_order_book'] = int(order_count == 0)
    if calibration is not None:
        checks['expected_order_count_mismatch'] = int(order_count != calibration['order_count'])

    queries = {
        'machine_count': 'SELECT COUNT(*) n FROM Machinery_Assets',
        'employee_count': 'SELECT COUNT(*) n FROM Employees',
        'factory_count': 'SELECT COUNT(*) n FROM Factories',
        'invalid_supervisors': "SELECT COUNT(*) n FROM employee_batches b JOIN Employees e ON e.employee_id=b.supervisor_id JOIN Production_Lines l ON l.line_id=b.line_id JOIN Departments d ON d.department_id=l.department_id WHERE e.factory_id<>d.factory_id OR e.role<>'Line_Supervisor'",
        'wrong_worker_factory': 'SELECT COUNT(*) n FROM Job_Shop_Schedules j JOIN Employees e ON e.employee_id=j.employee_id JOIN Production_Lines l ON l.line_id=j.line_id JOIN Departments d ON d.department_id=l.department_id WHERE e.factory_id<>d.factory_id',
        'wrong_machine_line': 'SELECT COUNT(*) n FROM Job_Shop_Schedules j JOIN Machinery_Assets m ON m.asset_id=j.asset_id WHERE j.line_id<>m.line_id',
        'missing_skill': 'SELECT COUNT(*) n FROM Job_Shop_Schedules j WHERE NOT EXISTS (SELECT 1 FROM Machine_Capabilities c JOIN Operator_Skill_Matrix s ON s.operation_type=c.operation_type WHERE c.asset_id=j.asset_id AND s.employee_id=j.employee_id)',
        'on_leave_assignments': 'SELECT COUNT(*) n FROM Job_Shop_Schedules j JOIN Work_Schedules w ON w.employee_id=j.employee_id WHERE w.is_on_leave=1',
        'wrong_batch_member': 'SELECT COUNT(*) n FROM Job_Shop_Schedules j WHERE j.batch_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM batch_members b WHERE b.batch_id=j.batch_id AND b.employee_id=j.employee_id)',
        'order_factory_transfers': 'SELECT COUNT(*) n FROM (SELECT j.production_order_id FROM Job_Shop_Schedules j JOIN Production_Lines l ON l.line_id=j.line_id JOIN Departments d ON d.department_id=l.department_id GROUP BY j.production_order_id HAVING COUNT(DISTINCT d.factory_id)>1) x',
        'quantity_mismatch': 'SELECT COUNT(*) n FROM (SELECT j.production_order_id,j.operation_id FROM Job_Shop_Schedules j JOIN Production_Orders p ON p.production_order_id=j.production_order_id GROUP BY j.production_order_id,j.operation_id HAVING SUM(j.lot_quantity)<>MAX(p.target_quantity)) x',
        'duplicate_lot_operation': 'SELECT COUNT(*) n FROM (SELECT production_order_id,lot_index,operation_id FROM Job_Shop_Schedules GROUP BY production_order_id,lot_index,operation_id HAVING COUNT(*)<>1) x',
        'invalid_completion': "SELECT COUNT(*) n FROM Production_Orders p WHERE p.status='Completed' AND EXISTS(SELECT 1 FROM Job_Shop_Schedules j WHERE j.production_order_id=p.production_order_id AND j.execution_status<>'Done')",
    }
    # Start from the expected product routing, not groups of existing lots.
    queries['orders_without_routing'] = "SELECT COUNT(*) n FROM Production_Orders p WHERE NOT EXISTS (SELECT 1 FROM Routing_Operations r WHERE r.product_id=p.product_id)"
    queries['missing_expected_operations'] = "SELECT COUNT(*) n FROM Production_Orders p JOIN Routing_Operations r ON r.product_id=p.product_id WHERE NOT EXISTS (SELECT 1 FROM Job_Shop_Schedules j WHERE j.production_order_id=p.production_order_id AND j.operation_id=r.operation_id)"
    queries['unexpected_operations'] = "SELECT COUNT(*) n FROM Job_Shop_Schedules j LEFT JOIN Production_Orders p ON p.production_order_id=j.production_order_id LEFT JOIN Routing_Operations r ON r.product_id=p.product_id AND r.operation_id=j.operation_id WHERE r.operation_id IS NULL"
    if not checks['missing_ownership_table']:
        queries['missing_order_ownership'] = "SELECT COUNT(*) n FROM Production_Orders p LEFT JOIN Order_Factory_Ownership o ON o.production_order_id=p.production_order_id WHERE o.production_order_id IS NULL"
        queries['invalid_order_ownership'] = "SELECT COUNT(*) n FROM Order_Factory_Ownership o LEFT JOIN Production_Orders p ON p.production_order_id=o.production_order_id LEFT JOIN Factories f ON f.factory_id=o.factory_id WHERE p.production_order_id IS NULL OR f.factory_id IS NULL"
        queries['ownership_schedule_disagreement'] = "SELECT COUNT(*) n FROM Job_Shop_Schedules j JOIN Order_Factory_Ownership o ON o.production_order_id=j.production_order_id LEFT JOIN Production_Lines l ON l.line_id=j.line_id LEFT JOIN Departments d ON d.department_id=l.department_id WHERE d.factory_id IS NULL OR d.factory_id<>o.factory_id"
        cursor.execute('SELECT p.production_order_id,r.required_machine_type,o.factory_id FROM Production_Orders p JOIN Routing_Operations r ON r.product_id=p.product_id JOIN Order_Factory_Ownership o ON o.production_order_id=p.production_order_id')
        expected_routes = cursor.fetchall()
        cursor.execute('SELECT m.asset_id,m.factory_id,m.line_id,m.current_status,c.operation_type,l.is_active FROM Machinery_Assets m LEFT JOIN Machine_Capabilities c ON c.asset_id=m.asset_id LEFT JOIN Production_Lines l ON l.line_id=m.line_id')
        import pandas as pd
        from utils.machine_reserve import build_reserve
        from utils.machine_taxonomy import machine_type_to_operation_type
        machines = pd.DataFrame(cursor.fetchall())
        reserved, _ = build_reserve(machines)
        # Same active-line/non-reserved eligibility basis as ChromosomeFactory.
        available = {(row.factory_id, str(row.operation_type).strip().lower())
                     for row in machines.itertuples() if row.is_active == 1 and row.asset_id not in reserved}
        checks['infeasible_owned_routings'] = sum(
            (r['factory_id'], machine_type_to_operation_type(r['required_machine_type'])) not in available
            for r in expected_routes)
    for timeline in ('planned', 'actual'):
        for resource in ('asset_id', 'employee_id'):
            queries[f'{timeline}_{resource}_overlaps'] = f"SELECT COUNT(*) n FROM (SELECT {timeline}_start_time s,MAX({timeline}_end_time) OVER (PARTITION BY {resource} ORDER BY {timeline}_start_time,schedule_item_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) prev FROM Job_Shop_Schedules) x WHERE s<prev"
        queries[f'{timeline}_precedence'] = f"SELECT COUNT(*) n FROM (SELECT j.{timeline}_start_time s,LAG(j.{timeline}_end_time) OVER(PARTITION BY j.production_order_id,j.lot_index ORDER BY r.operation_sequence_number) prev FROM Job_Shop_Schedules j JOIN Routing_Operations r ON r.operation_id=j.operation_id) x WHERE s<prev"
    for name, sql in queries.items():
        cursor.execute(sql)
        checks[name] = cursor.fetchone()['n']
    cursor.execute("SELECT TABLE_NAME,COLUMN_NAME,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL")
    foreign_keys = cursor.fetchall()
    for f in foreign_keys:
        t,c,rt,rc = [f[k] for k in ('TABLE_NAME','COLUMN_NAME','REFERENCED_TABLE_NAME','REFERENCED_COLUMN_NAME')]
        cursor.execute(f'SELECT COUNT(*) n FROM `{t}` a LEFT JOIN `{rt}` b ON a.`{c}`=b.`{rc}` WHERE a.`{c}` IS NOT NULL AND b.`{rc}` IS NULL')
        checks[f'fk_{t}_{c}'] = cursor.fetchone()['n']
    cursor.execute('SELECT j.planned_start_time,j.planned_end_time,j.actual_start_time,j.actual_end_time,j.planned_work_segments,j.actual_work_segments,b.rotation_offset,b.rest_day_offset FROM Job_Shop_Schedules j LEFT JOIN employee_batches b ON b.batch_id=j.batch_id')
    checks['bad_work_segments'] = 0
    for row in cursor:
        for timeline in ('planned', 'actual'):
            segments = [(datetime.fromisoformat(a),datetime.fromisoformat(b)) for a,b in json.loads(row[timeline+'_work_segments'])]
            valid = bool(segments) and segments[0][0] == row[timeline+'_start_time'] and segments[-1][1] == row[timeline+'_end_time']
            for i,(a,b) in enumerate(segments):
                valid = valid and a < b and (i == 0 or segments[i-1][1] <= a)
                if row['rotation_offset'] is not None:
                    args=(row['rotation_offset'],row['rest_day_offset'])
                    valid = valid and batch_available_at(*args,a) and batch_available_at(*args,b-timedelta(seconds=1))
            checks['bad_work_segments'] += int(not valid)
    cursor.close()
    errors = {k:v for k,v in checks.items() if v != expected.get(k, 0)}
    return {'valid':not errors,'checks':checks,'errors':errors,'details':details,'expected_counts':expected}
