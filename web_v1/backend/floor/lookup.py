"""Names and ownership from the factory database (SELECT only, through erp.db.read_only)."""
from erp.db import ErpUnavailable, cached, live_database, read_only
from erp.queries import LINES_OF, one, rows

__all__ = ['ErpUnavailable', 'lines', 'names', 'machine', 'worker', 'orders_using_material']


def lines(dataset, factory_id):
    """[{line_id, line_number}] of a factory's production lines."""
    def work():
        with read_only(live_database(dataset)) as cursor:
            return rows(cursor, f'SELECT l.line_id, l.line_number FROM {LINES_OF} ORDER BY l.line_number', (factory_id,))
    return cached((live_database(dataset), factory_id, 'floor-lines'), 600, work)


def names(dataset, worker_ids=(), machine_ids=()):
    """({worker id: name}, {machine id: model}); empty maps when the database is unavailable."""
    workers = sorted({int(w) for w in worker_ids})
    machines = sorted({int(m) for m in machine_ids})
    try:
        with read_only(live_database(dataset)) as cursor:
            people = {str(r['employee_id']): f"{r['first_name']} {r['last_name']}" for r in rows(
                cursor, 'SELECT employee_id, first_name, last_name FROM employees WHERE employee_id IN '
                        f'({",".join(["%s"] * len(workers))})', workers)} if workers else {}
            models = {str(r['asset_id']): r['machine_model'] for r in rows(
                cursor, 'SELECT asset_id, machine_model FROM machinery_assets WHERE asset_id IN '
                        f'({",".join(["%s"] * len(machines))})', machines)} if machines else {}
    except ErpUnavailable:
        return {}, {}
    return people, models


def machine(dataset, asset_id):
    """{asset_id, factory_id, line_id, machine_model, current_status} or None."""
    with read_only(live_database(dataset)) as cursor:
        return one(cursor, 'SELECT asset_id, factory_id, line_id, machine_model, current_status '
                           'FROM machinery_assets WHERE asset_id = %s', (int(asset_id),)) or None


def worker(dataset, employee_id):
    """{employee_id, factory_id, line_id (home crew's line, or None), name} or None."""
    with read_only(live_database(dataset)) as cursor:
        return one(cursor, "SELECT e.employee_id, e.factory_id, b.line_id, CONCAT(e.first_name, ' ', e.last_name) AS name "
                           'FROM employees e LEFT JOIN batch_members m ON m.employee_id = e.employee_id '
                           'LEFT JOIN employee_batches b ON b.batch_id = m.batch_id WHERE e.employee_id = %s LIMIT 1',
                   (int(employee_id),)) or None


def orders_using_material(dataset, material_id, order_ids):
    """Which of these orders need the material (bill of materials of their style)."""
    from erp.queries import orders_using_material as query
    with read_only(live_database(dataset)) as cursor:
        return query(cursor, material_id, order_ids)
