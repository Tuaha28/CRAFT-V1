"""ERP read models over the factory database (SELECT only, one factory at a time).

Dates are judged at the capture's reference date ("today" of the synthetic
factory). Quality and machine logs carry no meaningful event dates in the
generated data, so they are reported by line, operation and type, not as
trends over time.
"""
from datetime import date, timedelta
from decimal import Decimal
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from utils.shift_rotation import rest_weekday_for_batch, shift_band_for_batch  # noqa: E402

PAGE_MAX = 200
WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

# Orders owned by a factory (ownership ids are stored as text).
ORDERS_OF = ('FROM purchase_orders p JOIN production_orders o ON o.po_id = p.po_id '
             'JOIN order_factory_ownership f ON f.production_order_id = CAST(o.production_order_id AS CHAR) '
             'AND f.factory_id = %s')
LINES_OF = ('production_lines l JOIN departments d ON d.department_id = l.department_id AND d.factory_id = %s')


def _clean(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date,)):
        return value.isoformat()
    return value


def rows(cursor, sql, params=()):
    cursor.execute(sql, params)
    return [{k: _clean(v) for k, v in row.items()} for row in cursor.fetchall()]


def one(cursor, sql, params=()):
    found = rows(cursor, sql, params)
    return found[0] if found else {}


def page_args(page, size):
    size = max(1, min(PAGE_MAX, int(size or 50)))
    page = max(1, int(page or 1))
    return size, (page - 1) * size


def factories(cursor):
    return rows(cursor, 'SELECT factory_id, name, location FROM factories ORDER BY factory_id')


def summary(cursor, fid, ref):
    orders = rows(cursor, f'SELECT p.po_status AS status, COUNT(*) AS n {ORDERS_OF} GROUP BY p.po_status', (fid,))
    late_open = one(cursor, f"SELECT COUNT(*) AS n {ORDERS_OF} WHERE p.delivery_deadline < %s AND p.po_status <> 'Shipped'",
                    (fid, ref))['n']
    at_risk = one(cursor, f"SELECT COUNT(*) AS n {ORDERS_OF} WHERE p.delivery_deadline BETWEEN %s AND %s "
                          f"AND p.po_status = 'Confirmed'", (fid, ref, ref + timedelta(days=7)))['n']
    shipped = one(cursor, f'SELECT COUNT(*) AS shipped, SUM(DATE(dl.actual_shipment_date) <= p.delivery_deadline) AS on_time '
                          f'{ORDERS_OF} JOIN delivery_logistics dl ON dl.po_id = p.po_id '
                          f'WHERE dl.actual_shipment_date IS NOT NULL', (fid,))
    quality = one(cursor, f'SELECT SUM(q.inspected_quantity) AS inspected, SUM(q.defective_quantity) AS defective '
                          f'FROM quality_logs q JOIN {LINES_OF} ON l.line_id = q.line_id', (fid,))
    machines = rows(cursor, 'SELECT current_status AS status, COUNT(*) AS n FROM machinery_assets WHERE factory_id = %s '
                            'GROUP BY current_status', (fid,))
    people = rows(cursor, 'SELECT role, COUNT(*) AS n FROM employees WHERE factory_id = %s AND is_active = 1 GROUP BY role', (fid,))
    leave = one(cursor, 'SELECT COUNT(*) AS rostered, SUM(w.is_on_leave) AS on_leave FROM work_schedules w '
                        'JOIN employees e ON e.employee_id = w.employee_id AND e.factory_id = %s WHERE w.shift_date = %s',
                (fid, ref))
    wip = one(cursor, f'SELECT COUNT(DISTINCT w.production_order_id) AS orders, SUM(w.pieces_completed) AS pieces '
                      f'FROM wip_tracking w JOIN {LINES_OF} ON l.line_id = w.line_id', (fid,))
    lines = one(cursor, f'SELECT COUNT(*) AS n, AVG(l.target_efficiency_percentage) AS target_eff FROM {LINES_OF} '
                        f'WHERE l.is_active = 1', (fid,))
    inspected, defective = quality.get('inspected') or 0, quality.get('defective') or 0
    return {
        'reference_date': ref.isoformat(),
        'orders_by_status': {r['status']: r['n'] for r in orders}, 'orders_total': sum(r['n'] for r in orders),
        'orders_overdue': late_open, 'orders_due_7_days_not_started': at_risk,
        'shipped': shipped.get('shipped') or 0, 'shipped_on_time': int(shipped.get('on_time') or 0),
        'on_time_delivery_pct': round(100 * (shipped.get('on_time') or 0) / shipped['shipped'], 1) if shipped.get('shipped') else None,
        'dhu': round(100 * defective / inspected, 2) if inspected else None, 'pieces_inspected': inspected,
        'machines_by_status': {r['status']: r['n'] for r in machines},
        'people_by_role': {r['role']: r['n'] for r in people},
        'rostered_today': leave.get('rostered') or 0, 'on_leave_today': int(leave.get('on_leave') or 0),
        'wip_orders': wip.get('orders') or 0, 'wip_pieces': int(wip.get('pieces') or 0),
        'lines': lines.get('n') or 0, 'target_efficiency_pct': round(lines['target_eff'], 1) if lines.get('target_eff') else None,
    }


ORDER_SORTS = {'deadline': 'p.delivery_deadline', 'ordered': 'p.order_date', 'client': 'p.client_name',
               'quantity': 'o.target_quantity', 'po': 'p.po_number'}


def orders(cursor, fid, ref, *, q='', status='', page=1, size=50, sort='deadline'):
    size, offset = page_args(page, size)
    where, params = [], [fid]
    if q:
        where.append('(p.po_number LIKE %s OR p.client_name LIKE %s OR pr.style_code LIKE %s OR pr.name LIKE %s)')
        params += [f'%{q}%'] * 4
    if status == 'OVERDUE':
        where.append("p.delivery_deadline < %s AND p.po_status <> 'Shipped'")
        params.append(ref)
    elif status:
        where.append('p.po_status = %s')
        params.append(status)
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    base = f'{ORDERS_OF} JOIN products pr ON pr.product_id = o.product_id LEFT JOIN delivery_logistics dl ON dl.po_id = p.po_id{clause}'
    total = one(cursor, f'SELECT COUNT(*) AS n {base}', params)['n']
    items = rows(cursor, f'SELECT p.po_id, p.po_number, p.client_name, p.order_date, p.delivery_deadline, p.po_status, '
                         f'o.production_order_id, o.target_quantity, o.scheduled_start_date, o.status AS production_status, '
                         f'pr.product_id, pr.style_code, pr.name AS product, pr.category, dl.delivery_status, '
                         f'dl.carrier_name, dl.actual_shipment_date, DATEDIFF(p.delivery_deadline, %s) AS days_to_deadline '
                         f'{base} ORDER BY {ORDER_SORTS.get(sort, ORDER_SORTS["deadline"])}, p.po_id LIMIT %s OFFSET %s',
                 [ref, *params, size, offset])
    return {'total': total, 'items': items}


def orders_using_material(cursor, material_id, order_ids):
    """The ids among order_ids whose style's bill of materials contains the material."""
    ids = [int(o) for o in order_ids]
    if not ids:
        return []
    found = rows(cursor, 'SELECT DISTINCT o.production_order_id FROM production_orders o '
                         'JOIN bill_of_materials b ON b.product_id = o.product_id '
                         f'WHERE b.material_id = %s AND o.production_order_id IN ({",".join(["%s"] * len(ids))})',
                 [int(material_id), *ids])
    return sorted((str(r['production_order_id']) for r in found), key=int)


def order_detail(cursor, fid, production_order_id):
    head = one(cursor, f'SELECT p.*, o.production_order_id, o.target_quantity, o.scheduled_start_date, o.scheduled_end_date, '
                       f'o.status AS production_status, pr.product_id, pr.style_code, pr.name AS product, pr.category '
                       f'{ORDERS_OF} JOIN products pr ON pr.product_id = o.product_id WHERE o.production_order_id = %s',
               (fid, production_order_id))
    if not head:
        return None
    routing = rows(cursor, 'SELECT r.operation_id, r.operation_sequence_number AS step, r.operation_name, '
                           'r.standard_allowed_minutes AS sam, r.required_machine_type, '
                           # Each table summed on its own: joining both at once multiplied every work row by the quality rows.
                           'COALESCE((SELECT SUM(w.pieces_completed) FROM wip_tracking w '
                           '          WHERE w.operation_id = r.operation_id AND w.production_order_id = %s), 0) AS pieces_done, '
                           'COALESCE(q.inspected, 0) AS inspected, COALESCE(q.defective, 0) AS defective '
                           'FROM routing_operations r '
                           'LEFT JOIN (SELECT operation_id, SUM(inspected_quantity) AS inspected, SUM(defective_quantity) AS defective '
                           '           FROM quality_logs WHERE production_order_id = %s GROUP BY operation_id) q ON q.operation_id = r.operation_id '
                           'WHERE r.product_id = %s ORDER BY r.operation_sequence_number',
                   (production_order_id, production_order_id, head['product_id']))
    materials = rows(cursor, 'SELECT m.name, m.material_type, m.unit_of_measurement AS unit, b.required_quantity AS per_piece, '
                             'b.wastage_allowance_percentage AS wastage_pct, '
                             'b.required_quantity * (1 + b.wastage_allowance_percentage / 100) * %s AS required '
                             'FROM bill_of_materials b JOIN raw_materials m ON m.material_id = b.material_id '
                             'WHERE b.product_id = %s ORDER BY m.material_type',
                     (head['target_quantity'], head['product_id']))
    delivery = one(cursor, 'SELECT carrier_name, tracking_number, actual_shipment_date, estimated_arrival_date, delivery_status '
                           'FROM delivery_logistics WHERE po_id = %s', (head['po_id'],))
    for step in routing:
        step['sam_total_minutes'] = round((step['sam'] or 0) * head['target_quantity'], 1)
    return {'order': head, 'routing': routing, 'materials': materials, 'delivery': delivery,
            'work_minutes': round(sum(s['sam_total_minutes'] for s in routing), 1)}


def styles(cursor, fid, *, q='', category='', page=1, size=50):
    size, offset = page_args(page, size)
    where, params = [], []
    if q:
        where.append('(pr.style_code LIKE %s OR pr.name LIKE %s)')
        params += [f'%{q}%'] * 2
    if category:
        where.append('pr.category = %s')
        params.append(category)
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    total = one(cursor, f'SELECT COUNT(*) AS n FROM products pr{clause}', params)['n']
    items = rows(cursor, f'SELECT pr.product_id, pr.style_code, pr.name, pr.category, '
                         f'(SELECT COUNT(*) FROM product_variants v WHERE v.product_id = pr.product_id) AS variants, '
                         f'(SELECT COUNT(*) FROM routing_operations r WHERE r.product_id = pr.product_id) AS steps, '
                         f'(SELECT SUM(r.standard_allowed_minutes) FROM routing_operations r WHERE r.product_id = pr.product_id) AS sam, '
                         f'(SELECT COUNT(*) FROM production_orders o JOIN order_factory_ownership f '
                         f' ON f.production_order_id = CAST(o.production_order_id AS CHAR) AND f.factory_id = %s '
                         f' WHERE o.product_id = pr.product_id) AS orders '
                         f'FROM products pr{clause} ORDER BY orders DESC, pr.style_code LIMIT %s OFFSET %s',
                 [fid, *params, size, offset])
    categories = rows(cursor, 'SELECT category, COUNT(*) AS n FROM products GROUP BY category ORDER BY n DESC')
    return {'total': total, 'items': items, 'categories': categories}


def style_detail(cursor, product_id):
    product = one(cursor, 'SELECT * FROM products WHERE product_id = %s', (product_id,))
    if not product:
        return None
    return {'product': product,
            'variants': rows(cursor, 'SELECT size, color, sku FROM product_variants WHERE product_id = %s ORDER BY color, size',
                             (product_id,)),
            'routing': rows(cursor, 'SELECT operation_id, operation_sequence_number AS step, operation_name, standard_allowed_minutes AS sam, '
                                    'required_machine_type FROM routing_operations WHERE product_id = %s '
                                    'ORDER BY operation_sequence_number', (product_id,)),
            'bom': rows(cursor, 'SELECT m.name, m.material_type, m.unit_of_measurement AS unit, b.required_quantity AS per_piece, '
                                'b.wastage_allowance_percentage AS wastage_pct FROM bill_of_materials b '
                                'JOIN raw_materials m ON m.material_id = b.material_id WHERE b.product_id = %s', (product_id,))}


def materials(cursor, fid):
    """Company-wide stock against what the factory's open orders still need."""
    stock = rows(cursor, 'SELECT m.material_id, m.name, m.material_type, m.unit_of_measurement AS unit, '
                         'COALESCE(SUM(s.current_quantity), 0) AS in_stock, COUNT(DISTINCT s.warehouse_location) AS locations, '
                         'MAX(s.last_restocked_date) AS last_restocked FROM raw_materials m '
                         'LEFT JOIN inventory_stock s ON s.material_id = m.material_id GROUP BY m.material_id ORDER BY m.material_type')
    need = {r['material_id']: r['required'] for r in rows(
        cursor, f"SELECT b.material_id, SUM(b.required_quantity * (1 + b.wastage_allowance_percentage / 100) * o.target_quantity) "
                f"AS required {ORDERS_OF} JOIN bill_of_materials b ON b.product_id = o.product_id "
                f"WHERE o.status IN ('Pending', 'Processing') GROUP BY b.material_id", (fid,))}
    for item in stock:
        item['required_open_orders'] = round(need.get(item['material_id'], 0.0), 2)
        item['shortage'] = round(max(0.0, item['required_open_orders'] - item['in_stock']), 2)
    suppliers = rows(cursor, 'SELECT su.supplier_id, su.name, su.contact_email, su.lead_time_days, su.reliability_score, '
                             'COUNT(s.stock_id) AS stock_lines, COALESCE(SUM(s.current_quantity), 0) AS quantity '
                             'FROM suppliers su LEFT JOIN inventory_stock s ON s.supplier_id = su.supplier_id '
                             'GROUP BY su.supplier_id ORDER BY su.reliability_score DESC')
    locations = rows(cursor, 'SELECT s.warehouse_location, m.name AS material, s.current_quantity, m.unit_of_measurement AS unit, '
                             'su.name AS supplier, s.last_restocked_date FROM inventory_stock s '
                             'JOIN raw_materials m ON m.material_id = s.material_id JOIN suppliers su ON su.supplier_id = s.supplier_id '
                             'ORDER BY s.warehouse_location, m.name')
    return {'materials': stock, 'suppliers': suppliers, 'stock': locations}


def workforce(cursor, fid, ref, *, q='', role='', page=1, size=50):
    size, offset = page_args(page, size)
    where, params = ['e.factory_id = %s'], [fid]
    if q:
        where.append("(CONCAT(e.first_name, ' ', e.last_name) LIKE %s OR CAST(e.employee_id AS CHAR) = %s)")
        params += [f'%{q}%', q]
    if role:
        where.append('e.role = %s')
        params.append(role)
    clause = ' WHERE ' + ' AND '.join(where)
    total = one(cursor, f'SELECT COUNT(*) AS n FROM employees e{clause}', params)['n']
    items = rows(cursor, f'SELECT e.employee_id, e.first_name, e.last_name, e.role, e.hire_date, e.is_active, '
                         f'b.batch_name AS crew, l.line_number AS line, b.rotation_offset, b.rest_day_offset, '
                         f'w.start_time, w.end_time, w.is_on_leave, '
                         f'(SELECT COUNT(*) FROM operator_skill_matrix s WHERE s.employee_id = e.employee_id) AS skills, '
                         f'(SELECT AVG(s.efficiency_rating) FROM operator_skill_matrix s WHERE s.employee_id = e.employee_id) AS efficiency '
                         f'FROM employees e LEFT JOIN batch_members bm ON bm.employee_id = e.employee_id '
                         f'LEFT JOIN employee_batches b ON b.batch_id = bm.batch_id LEFT JOIN production_lines l ON l.line_id = b.line_id '
                         f'LEFT JOIN work_schedules w ON w.employee_id = e.employee_id AND w.shift_date = %s'
                         f'{clause} ORDER BY e.employee_id LIMIT %s OFFSET %s', [ref, *params, size, offset])
    for person in items:
        # A crew member's shift follows the crew's weekly rotation; the generator
        # writes work_schedules as a fixed band that is NOT authoritative for
        # crew members (seed_database_fixed.shift_for_employee). Floaters keep it.
        if person['rotation_offset'] is not None:
            name, start, end = shift_band_for_batch(int(person['rotation_offset']), ref)
            person['start_time'], person['end_time'] = start.strftime('%H:%M'), end.strftime('%H:%M')
            person['shift_name'] = name
            person['rest_day'] = WEEKDAYS[rest_weekday_for_batch(int(person['rest_day_offset']), ref)]
            person['resting_today'] = person['rest_day'] == WEEKDAYS[ref.weekday()]
        else:
            person['shift_name'], person['rest_day'], person['resting_today'] = None, None, False
        person.pop('rotation_offset', None)
        person.pop('rest_day_offset', None)
    skills = rows(cursor, 'SELECT s.operation_type, COUNT(*) AS operators, AVG(s.efficiency_rating) AS mean_efficiency, '
                          'MIN(s.efficiency_rating) AS min_efficiency, MAX(s.efficiency_rating) AS max_efficiency '
                          'FROM operator_skill_matrix s JOIN employees e ON e.employee_id = s.employee_id AND e.factory_id = %s '
                          'GROUP BY s.operation_type ORDER BY operators DESC', (fid,))
    crews = rows(cursor, f'SELECT b.batch_id, b.batch_name, l.line_number AS line, b.primary_skill, b.is_active, '
                         f'b.rotation_offset, b.rest_day_offset, '
                         f'(SELECT COUNT(*) FROM batch_members m WHERE m.batch_id = b.batch_id) AS members '
                         f'FROM employee_batches b JOIN {LINES_OF} ON l.line_id = b.line_id ORDER BY l.line_number, b.batch_name',
                 (fid,))
    for crew in crews:
        name, start, end = shift_band_for_batch(int(crew['rotation_offset']), ref)
        crew['shift_today'] = f'{name} {start.strftime("%H:%M")}–{end.strftime("%H:%M")}'
        crew['rest_day'] = WEEKDAYS[rest_weekday_for_batch(int(crew['rest_day_offset']), ref)]
        crew['resting_today'] = crew['rest_day'] == WEEKDAYS[ref.weekday()]
    roles = rows(cursor, 'SELECT role, COUNT(*) AS n FROM employees WHERE factory_id = %s GROUP BY role', (fid,))
    return {'total': total, 'items': items, 'skills': skills, 'crews': crews, 'roles': roles}


def machines(cursor, fid, *, status='', line='', q='', page=1, size=50):
    size, offset = page_args(page, size)
    where, params = ['a.factory_id = %s'], [fid]
    if status:
        where.append('a.current_status = %s')
        params.append(status)
    if line:
        where.append('l.line_number = %s')
        params.append(line)
    if q:
        where.append('(a.machine_model LIKE %s OR a.serial_number LIKE %s OR CAST(a.asset_id AS CHAR) = %s)')
        params += [f'%{q}%', f'%{q}%', q]
    clause = ' WHERE ' + ' AND '.join(where)
    base = f'FROM machinery_assets a LEFT JOIN production_lines l ON l.line_id = a.line_id{clause}'
    total = one(cursor, f'SELECT COUNT(*) AS n {base}', params)['n']
    items = rows(cursor, f'SELECT a.asset_id, a.machine_model, a.serial_number, a.purchase_date, a.current_status, '
                         f'l.line_number AS line, (SELECT GROUP_CONCAT(c.operation_type SEPARATOR ", ") FROM machine_capabilities c '
                         f'WHERE c.asset_id = a.asset_id) AS capabilities, '
                         f'(SELECT COUNT(*) FROM machine_status_logs g WHERE g.asset_id = a.asset_id '
                         f"AND g.status_changed_to = 'Breakdown') AS breakdowns, "
                         f'(SELECT g.reason FROM machine_status_logs g WHERE g.asset_id = a.asset_id ORDER BY g.timestamp DESC, '
                         f'g.log_id DESC LIMIT 1) AS last_reason '
                         f'{base} ORDER BY FIELD(a.current_status, "Breakdown", "Under_Maintenance", "Idle", "Active"), a.asset_id '
                         f'LIMIT %s OFFSET %s', [*params, size, offset])
    by_line = rows(cursor, 'SELECT l.line_number AS line, COUNT(*) AS machines, SUM(a.current_status = "Active") AS active, '
                           'SUM(a.current_status = "Breakdown") AS breakdown, SUM(a.current_status = "Under_Maintenance") AS maintenance, '
                           'SUM(a.current_status = "Idle") AS idle FROM machinery_assets a '
                           'LEFT JOIN production_lines l ON l.line_id = a.line_id WHERE a.factory_id = %s '
                           'GROUP BY l.line_number ORDER BY l.line_number', (fid,))
    types = rows(cursor, 'SELECT c.operation_type, COUNT(DISTINCT a.asset_id) AS machines, '
                         'SUM(a.current_status = "Active") AS active FROM machinery_assets a '
                         'JOIN machine_capabilities c ON c.asset_id = a.asset_id WHERE a.factory_id = %s '
                         'GROUP BY c.operation_type ORDER BY machines DESC', (fid,))
    return {'total': total, 'items': items, 'by_line': by_line, 'by_type': types}


def quality(cursor, fid):
    join = f'FROM quality_logs q JOIN {LINES_OF} ON l.line_id = q.line_id'
    dhu = 'ROUND(100 * SUM(q.defective_quantity) / NULLIF(SUM(q.inspected_quantity), 0), 2) AS dhu'
    totals = one(cursor, f'SELECT SUM(q.inspected_quantity) AS inspected, SUM(q.defective_quantity) AS defective, {dhu} {join}', (fid,))
    by_line = rows(cursor, f'SELECT l.line_number AS line, l.target_efficiency_percentage AS target_eff, '
                           f'SUM(q.inspected_quantity) AS inspected, SUM(q.defective_quantity) AS defective, {dhu} '
                           f'{join} GROUP BY l.line_number, l.target_efficiency_percentage ORDER BY dhu DESC', (fid,))
    by_operation = rows(cursor, f'SELECT r.operation_name, SUM(q.inspected_quantity) AS inspected, '
                                f'SUM(q.defective_quantity) AS defective, {dhu} {join} '
                                f'JOIN routing_operations r ON r.operation_id = q.operation_id '
                                f'GROUP BY r.operation_name HAVING inspected >= 100 ORDER BY dhu DESC LIMIT 25', (fid,))
    by_type = rows(cursor, f'SELECT q.defect_type_details AS defect, SUM(q.defective_quantity) AS defective, COUNT(*) AS logs '
                           f'{join} WHERE q.defective_quantity > 0 GROUP BY q.defect_type_details ORDER BY defective DESC LIMIT 15',
                   (fid,))
    worst = rows(cursor, f'SELECT q.production_order_id, p.po_number, p.client_name, SUM(q.inspected_quantity) AS inspected, '
                         f'SUM(q.defective_quantity) AS defective, {dhu} {join} '
                         f'JOIN production_orders o ON o.production_order_id = q.production_order_id '
                         f'JOIN purchase_orders p ON p.po_id = o.po_id GROUP BY q.production_order_id, p.po_number, p.client_name '
                         f'HAVING inspected >= 100 ORDER BY dhu DESC LIMIT 15', (fid,))
    return {'totals': totals, 'by_line': by_line, 'by_operation': by_operation, 'by_type': by_type, 'worst_orders': worst}


def search(cursor, fid, q):
    like = f'%{q}%'
    return {
        'orders': rows(cursor, f'SELECT o.production_order_id, p.po_number, p.client_name, p.delivery_deadline {ORDERS_OF} '
                               f'WHERE p.po_number LIKE %s OR p.client_name LIKE %s OR CAST(o.production_order_id AS CHAR) = %s '
                               f'ORDER BY p.delivery_deadline LIMIT 8', (fid, like, like, q)),
        'people': rows(cursor, "SELECT employee_id, first_name, last_name, role FROM employees WHERE factory_id = %s AND "
                               "(CONCAT(first_name, ' ', last_name) LIKE %s OR CAST(employee_id AS CHAR) = %s) LIMIT 8",
                       (fid, like, q)),
        'machines': rows(cursor, 'SELECT asset_id, machine_model, current_status FROM machinery_assets WHERE factory_id = %s AND '
                                 '(machine_model LIKE %s OR serial_number LIKE %s OR CAST(asset_id AS CHAR) = %s) LIMIT 8',
                         (fid, like, like, q)),
        'styles': rows(cursor, 'SELECT product_id, style_code, name FROM products WHERE style_code LIKE %s OR name LIKE %s LIMIT 8',
                       (like, like)),
    }
