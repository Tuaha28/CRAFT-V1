"""A customer's orders as the customer may see them: from the factory database (read-only, the demo copy when
that is switched on) and the approved CRAFT plan. Only customer-facing fields leave this module."""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from django.utils import timezone

ORDER_REF = re.compile(r'NC-(\d{4,12})')
_SELECT = ('SELECT o.production_order_id, o.target_quantity, o.scheduled_end_date, o.status AS production_status, '
           'p.po_number, p.po_status, p.order_date, p.delivery_deadline, p.client_name, pr.product_id, pr.style_code, '
           'pr.name AS product, pr.category, f.factory_id, d.carrier_name, d.tracking_number, d.actual_shipment_date, '
           'd.estimated_arrival_date, d.delivery_status '
           'FROM purchase_orders p JOIN production_orders o ON o.po_id = p.po_id '
           'JOIN products pr ON pr.product_id = o.product_id '
           'LEFT JOIN order_factory_ownership f ON f.production_order_id = CAST(o.production_order_id AS CHAR) '
           'LEFT JOIN delivery_logistics d ON d.po_id = p.po_id '
           'WHERE BINARY p.client_name = %s')  # exact: MySQL collations ignore case and trailing spaces


def order_ref(production_order_id):
    return f'NC-{int(production_order_id):04d}'


def parse_ref(ref):
    """The production order id for a reference we issued; None for anything else (so no other form matches)."""
    found = ORDER_REF.fullmatch(str(ref or ''))
    if not found:
        return None
    pid = int(found.group(1))
    return pid if order_ref(pid) == ref else None


def fetch(account, production_order_id=None):
    """(order rows, pieces through every step by order) for this customer's company. Read-only."""
    from erp.db import live_database, read_only
    from erp.queries import rows
    sql, params = _SELECT, [account.client_name]
    if production_order_id is not None:
        sql += ' AND o.production_order_id = %s'
        params.append(int(production_order_id))
    with read_only(live_database(account.dataset)) as cursor:
        found = rows(cursor, sql + ' ORDER BY p.delivery_deadline, o.production_order_id LIMIT 500', tuple(params))
        if not found:
            return [], {}, {}
        ids = [int(r['production_order_id']) for r in found]
        products = sorted({int(r['product_id']) for r in found})
        marks = ','.join(['%s'] * len(ids))
        work = rows(cursor, f'SELECT production_order_id, operation_id, SUM(pieces_completed) AS done FROM wip_tracking '
                            f'WHERE production_order_id IN ({marks}) GROUP BY production_order_id, operation_id', tuple(ids))
        steps = rows(cursor, f'SELECT product_id, operation_id FROM routing_operations '
                             f'WHERE product_id IN ({",".join(["%s"] * len(products))})', tuple(products))
    for (pid, op), n in app_pieces(account.dataset_id, ids).items():
        key = {'production_order_id': pid, 'operation_id': op}
        work.append({**key, 'done': max(n, next((int(w['done'] or 0) for w in work
                                                  if int(w['production_order_id']) == pid
                                                  and int(w['operation_id']) == op), 0))})
    done = {}
    by_product = {}
    for s in steps:
        by_product.setdefault(int(s['product_id']), []).append(int(s['operation_id']))
    per_op = {(int(w['production_order_id']), int(w['operation_id'])): int(w['done'] or 0) for w in work}
    share = {}
    for r in found:
        pid, ops = int(r['production_order_id']), by_product.get(int(r['product_id']), [])
        qty = max(int(r['target_quantity'] or 0), 1)
        done[pid] = min((per_op.get((pid, op), 0) for op in ops), default=0)  # through every step
        share[pid] = sum(min(1.0, per_op.get((pid, op), 0) / qty) for op in ops) / len(ops) if ops else 0.0
    return found, done, share


def app_pieces(dataset_id, order_ids):
    """{(production order id, operation id): pieces} recorded in the app (supervisors or the factory simulator)."""
    from django.db.models import Sum
    from floor.models import OutputEntry
    wanted = {str(int(i)) for i in order_ids}
    found = {}
    rows = (OutputEntry.objects.filter(run__dataset_id=dataset_id).values('operation_key').annotate(n=Sum('pieces')))
    for r in rows:
        order, _, op = r['operation_key'].partition('_')
        if order in wanted and op.isdigit():
            found[(int(order), int(op))] = int(r['n'] or 0)
    return found


def client_exists(dataset, client_name):
    from erp.db import live_database, read_only
    from erp.queries import rows
    with read_only(live_database(dataset)) as cursor:
        return bool(rows(cursor, 'SELECT 1 AS x FROM purchase_orders WHERE BINARY client_name = %s LIMIT 1', (client_name,)))


def clients(dataset, q, limit=20):
    """Company names for the account form (staff only)."""
    from erp.db import live_database, read_only
    from erp.queries import rows
    with read_only(live_database(dataset)) as cursor:
        found = rows(cursor, 'SELECT client_name, COUNT(*) AS orders FROM purchase_orders WHERE client_name LIKE %s '
                             'GROUP BY client_name ORDER BY orders DESC, client_name LIMIT %s', (f'%{q}%', int(limit)))
    return found


def _plan_finish(account, factory_ids):
    """{production order id: planned ready date} from each factory's approved CRAFT plan."""
    from floor.plan import approved_plan
    ready = {}
    for fid in factory_ids:
        run = approved_plan(account.dataset_id, fid)
        if run is None or not run.out_dir:
            continue
        try:
            digest = json.loads((Path(run.out_dir) / 'digest.json').read_text(encoding='utf-8'))
            start = datetime.fromisoformat(digest['reference_time'])
        except (OSError, ValueError, KeyError):
            continue
        for row in digest.get('order_rows', ()):
            finish = ((row.get('methods') or {}).get('CRAFT_V3') or {}).get('finish_day')
            if finish is not None and (row.get('methods') or {}).get('CRAFT_V3', {}).get('complete', True):
                ready[str(row['order_id'])] = (start + timedelta(days=float(finish))).date().isoformat()
    return ready


def _today(account, factory_id):
    """The factory clock's date when the simulator runs for this factory, else the data's reference date."""
    from django.apps import apps
    if factory_id and apps.is_installed('sim'):
        from sim.runner import factory_now
        now = factory_now(account.dataset_id, int(factory_id))
        if now is not None:
            return now.date().isoformat()
    return (account.dataset.reference_time or '')[:10]


def _stage(r, done, qty, share=None):
    shipped = r.get('po_status') == 'Shipped' or r.get('delivery_status') == 'In_Transit' or bool(r.get('actual_shipment_date'))
    if shipped:
        return 'Shipped', 100
    progress = min(100, round(100 * done / qty)) if qty else 0
    if share is not None:  # the average share done across the order's steps: moves as soon as work starts
        progress = max(progress, min(100, int(100 * share)))
    if r.get('delivery_status') == 'Packing':
        return 'Packing', max(progress, 95)
    if r.get('production_status') == 'Completed' or (qty and done >= qty):
        return 'All pieces sewn', max(progress, 90)
    if r.get('production_status') == 'Processing' or r.get('po_status') == 'In_Production' or done > 0 or progress > 0:
        return 'In production', progress
    return 'Confirmed', 0


def _shape(account, r, done, ready, share=None):
    pid, qty = int(r['production_order_id']), int(r['target_quantity'] or 0)
    stage, progress = _stage(r, done, qty, share)
    planned = ready.get(str(pid))
    estimate = planned or r.get('scheduled_end_date')
    today = _today(account, r.get('factory_id'))  # the factory's now
    stale = bool(not planned and estimate and today and estimate < today and stage in ('Confirmed', 'In production'))
    if stale:
        estimate = None  # an old schedule date for work not finished: never promise a date already past
    due = r.get('delivery_deadline')
    late = bool(due and stage != 'Shipped' and ((estimate and estimate > due) or (today and due < today)))
    arrival = r.get('estimated_arrival_date')
    if arrival and stage != 'Shipped' and estimate and arrival < estimate:
        arrival = None  # an old logistics estimate earlier than the order can be made: not shown to the customer
    return {
        # the agent's contract (PublicOrder)
        'order_id': order_ref(pid), 'customer_id': account.customer_id, 'style_code': str(r.get('style_code') or ''),
        'quantity': max(qty, 1), 'colour': '', 'stage': stage, 'progress_percent': progress,
        'estimated_ready_date': estimate, 'estimated_arrival_date': arrival,
        'destination': None, 'carrier': r.get('carrier_name'), 'tracking_reference': r.get('tracking_number'),
        'invoice_number': None, 'invoice_currency': None, 'invoice_amount': None, 'payment_status': None,
        'customer_note': ('Planned by the factory scheduling system; dates are estimates.' if planned
                          else 'The factory will confirm a new ready date.' if stale
                          else 'Dates are estimates from the factory schedule.'),
        'updated_at': timezone.now().isoformat(),
        # extra customer-facing fields for the portal pages
        'po_number': r.get('po_number'), 'product': r.get('product'), 'category': r.get('category'),
        'ordered_on': r.get('order_date'), 'due_date': r.get('delivery_deadline'),
        'shipped_on': r.get('actual_shipment_date'), 'ready_from': 'plan' if planned else 'schedule', 'late': late,
    }


def customer_orders(account):
    found, done, *rest = fetch(account)
    share = rest[0] if rest else {}
    ready = _plan_finish(account, sorted({int(r['factory_id']) for r in found if r.get('factory_id')}))
    return [_shape(account, r, done.get(int(r['production_order_id']), 0), ready,
                   share.get(int(r['production_order_id']))) for r in found]


def customer_order(account, ref):
    pid = parse_ref(ref)
    if pid is None:
        return None
    found, done, *rest = fetch(account, pid)
    if not found:
        return None
    r = found[0]
    ready = _plan_finish(account, [int(r['factory_id'])] if r.get('factory_id') else [])
    return _shape(account, r, done.get(pid, 0), ready, (rest[0] if rest else {}).get(pid))
