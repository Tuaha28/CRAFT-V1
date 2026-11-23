"""Completing steps and orders.

A step ("<order>_<operation_id>") counts its pieces across every plan of the factory (as plan-ahead, the portal and
the simulator do). It is complete when its good pieces (recorded minus unreworked defective pieces) reach the step's
pieces, or when the line supervisor or production manager marks it complete, also with no hourly counts (the
missing pieces are recorded as one entry so every count stays right) or closed short with a reason. A step never
finishes before the step before it, nor makes more pieces than that step did.

An order is Sewn when every step is complete; then final inspection (QC), packed and shipped (merchandiser or
manager), closed (manager or planner). The production manager reopens steps and orders. Every action is logged, and
with demo copies switched on the factory data copy follows (erp.writer).
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone
from planning import services as planning_services
from planning.models import Dataset, PlanRun
from . import services
from .models import DefectCheck, Issue, OrderStatus, OutputEntry, StepStatus
from .plan import load

DONE = ('COMPLETE', 'SHORT')
INSPECTABLE = ('SEWN', 'QC_PASSED', 'QC_FAILED')
LOCKED = ('PACKED', 'SHIPPED', 'CLOSED')  # its steps may no longer be reopened
MAX_TEXT = 2000


# ---- time ---------------------------------------------------------------------------------------------------------

def _clock(dataset_id, factory_id):
    from django.apps import apps
    if apps.is_installed('sim'):
        from sim.runner import factory_now
        return factory_now(dataset_id, factory_id)
    return None


def factory_now(dataset_id, factory_id):
    """The factory's time (the simulated clock when one runs), timezone-aware for storing."""
    return _clock(dataset_id, factory_id) or timezone.now()


def _local_now(dataset_id, factory_id):
    """The factory's wall-clock time without a zone, comparable with a plan's start (as floor views use it)."""
    found = _clock(dataset_id, factory_id)
    return found.replace(tzinfo=None) if found is not None else datetime.now()


# ---- plans --------------------------------------------------------------------------------------------------------

def _order_ops(plan, order_id):
    return sorted((op for op in plan.ops.values() if str(op['order_id']) == str(order_id)), key=lambda o: int(o['step']))


def _previous(plan, op):
    before = [o for o in _order_ops(plan, op['order_id']) if int(o['step']) < int(op['step'])]
    return before[-1] if before else None


def plan_for(dataset_id, factory_id, order_id, newest=30):
    """The newest approved plan of the factory that has this order (finished orders drop out of later plans)."""
    runs = (PlanRun.objects.filter(dataset_id=dataset_id, factory_id=factory_id, kind='SCHEDULE', status='SUCCEEDED',
                                   decision='APPROVED').exclude(out_dir='').order_by('-decided_at')[:newest])
    for run in runs:
        try:
            plan = load(run)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if any(str(op['order_id']) == str(order_id) for op in plan.ops.values()):
            return plan
    return None


# ---- steps --------------------------------------------------------------------------------------------------------

def step_states(dataset_id, factory_id, plan):
    """{key: state of every step of the plan}: NOT_STARTED, IN_PROGRESS, PAUSED (a machine of it is down),
    COMPLETE or SHORT, with its pieces needed, recorded, defective (unreworked), good and final."""
    keys = list(plan.ops)
    scope = {'run__dataset_id': dataset_id, 'run__factory_id': factory_id, 'operation_key__in': keys}
    recorded = dict(OutputEntry.objects.filter(**scope).values_list('operation_key').annotate(n=Sum('pieces')))
    bad = defaultdict(int)
    for c in DefectCheck.objects.filter(**scope, reworked_at__isnull=True, defects__gt=0).values(
            'operation_key', 'checked', 'defects'):
        bad[c['operation_key']] += min(c['checked'], c['defects'])
    rows = {r.operation_key: r for r in StepStatus.objects.filter(
        dataset_id=dataset_id, factory_id=factory_id, operation_key__in=keys).select_related('set_by')}
    down = {str(m) for m in Issue.objects.filter(dataset_id=dataset_id, factory_id=factory_id, kind='MACHINE_DOWN')
            .exclude(status='RESOLVED').values_list('machine_id', flat=True) if m is not None}
    states = {}
    for key, op in plan.ops.items():
        needed, rec = int(op.get('pieces') or 0), int(recorded.get(key) or 0)
        good, row = max(0, rec - bad[key]), rows.get(key)
        reopened = row is not None and row.state == 'OPEN'
        if reopened:
            row = None
        if row is not None:
            state = row.state
        elif needed and good >= needed and not reopened:
            state = 'COMPLETE'
        elif any(str(m) in down for m in op.get('machines') or ()):
            state = 'PAUSED'
        else:
            state = 'IN_PROGRESS' if rec > 0 else 'NOT_STARTED'
        states[key] = {
            'key': key, 'order_id': str(op['order_id']), 'step': int(op['step']), 'line': int(op['line']),
            'needed': needed, 'recorded': rec, 'defective': bad[key], 'good': good, 'state': state,
            'final': row.pieces if row else (needed if state == 'COMPLETE' else good),
            'marked': bool(row and row.marked), 'reason': row.reason if row else '', 'reopened': reopened,
            'by': row.set_by.get_username() if row else None,
            'finished_at': row.finished_at.isoformat() if row else None,
            'planned_end': (plan.reference + timedelta(seconds=int(op['end']))).isoformat()}
    for key, op in plan.ops.items():  # whether the step before is finished (a step cannot finish before it)
        prev = _previous(plan, op)
        states[key]['previous'] = int(prev['step']) if prev else None
        states[key]['ready'] = prev is None or states[prev['key']]['state'] in DONE
        # most pieces this step can have: never more than the step before finished
        states[key]['limit'] = min(states[key]['needed'], states[prev['key']]['final']) if prev and states[key]['ready'] \
            else states[key]['needed']
    return states


def finished_keys(dataset_id, factory_id, plan):
    """Step keys of the plan that need no more work: closed steps, and every step of an order past sewing."""
    keys = set(StepStatus.objects.filter(dataset_id=dataset_id, factory_id=factory_id, operation_key__in=list(plan.ops),
                                         state__in=DONE).values_list('operation_key', flat=True))
    orders = set(OrderStatus.objects.filter(dataset_id=dataset_id, factory_id=factory_id,
                                            order_id__in={str(op['order_id']) for op in plan.ops.values()})
                 .values_list('order_id', flat=True))
    return keys | {k for k, op in plan.ops.items() if str(op['order_id']) in orders}


def check_output(dataset_id, factory_id, plan, run, operation_key, slot, pieces):
    """Refuse hourly pieces that a finished step, or the step before it, does not allow."""
    op = plan.ops[operation_key]
    if StepStatus.objects.filter(dataset_id=dataset_id, factory_id=factory_id, operation_key=operation_key,
                                 state__in=DONE).exists():
        raise ValidationError('This step is already complete. The production manager can reopen it to change its pieces')
    if OrderStatus.objects.filter(dataset_id=dataset_id, factory_id=factory_id, order_id=str(op['order_id'])).exists():
        raise ValidationError('This order is already finished')
    before = (OutputEntry.objects.filter(run__dataset_id=dataset_id, run__factory_id=factory_id,
                                         operation_key=operation_key).exclude(run=run, slot=slot)
              .aggregate(n=Sum('pieces'))['n'] or 0)
    total, needed = before + int(pieces), int(op.get('pieces') or 0)
    if needed and total > needed:
        raise ValidationError(f'This step has {needed} pieces in total and {before} are already recorded')
    prev = _previous(plan, op)
    if prev is not None:
        p = step_states(dataset_id, factory_id, plan)[prev['key']]
        limit = p['final'] if p['state'] in DONE else p['good']
        if total > limit:
            raise ValidationError(f'Step {prev["step"]} has finished {limit} pieces so far, so this step can have at most '
                                  f'{limit}')


def _write(dataset_id, kind, statements, what):
    from erp import writer
    dataset = Dataset.objects.get(pk=dataset_id)
    transaction.on_commit(lambda: writer.write_status(dataset, kind, statements, what))


def _step_done_sql(key):
    from erp import writer
    order, _, operation = key.partition('_')
    return writer.step_done_statements(order, operation)


@transaction.atomic
def complete_step(user, *, dataset_id, factory_id, operation_key, pieces=None, reason=''):
    """Mark a step complete (all its pieces, recorded or not) or closed short (fewer pieces and a reason)."""
    services.require(user, 'floor.complete_step')
    run, plan = services.floor_plan(dataset_id, factory_id)
    op = plan.ops.get(operation_key)
    if op is None:
        raise ValidationError('This step is not in the approved plan')
    services.check_scope(user, factory_id, op['line'])
    if OrderStatus.objects.filter(dataset_id=dataset_id, factory_id=factory_id, order_id=str(op['order_id'])).exists():
        raise ValidationError('This order is already finished')
    states = step_states(dataset_id, factory_id, plan)
    me = states[operation_key]
    if me['state'] in DONE and me['by']:
        raise ValidationError('This step is already complete')
    limit = me['needed']
    prev = _previous(plan, op)
    if prev is not None:
        p = states[prev['key']]
        if p['state'] not in DONE:
            raise ValidationError(f'Complete step {prev["step"]} first: the step before must be finished')
        limit = min(limit, p['final'])
    count = limit if pieces in (None, '') else int(pieces)
    reason = (reason or '').strip()[:MAX_TEXT]
    if count < me['recorded']:
        raise ValidationError(f'{me["recorded"]} pieces are already recorded for this step')
    if count > limit:
        raise ValidationError(f'This step can have at most {limit} pieces' + (
            f' (step {prev["step"]} finished {limit})' if prev is not None and limit < me['needed'] else ''))
    short = count < me['needed']
    if short and not reason:
        raise ValidationError('Give a reason for closing the step with fewer pieces')
    missing, entry = count - me['recorded'], None
    if missing > 0:
        slot = max(0, int((_local_now(dataset_id, factory_id) - plan.reference).total_seconds() // 3600))
        entry = OutputEntry.objects.filter(run=run, operation_key=operation_key, slot=slot).first()
        if entry is None:
            entry = OutputEntry.objects.create(run=run, line_id=int(op['line']), operation_key=operation_key, slot=slot,
                                               pieces=missing, entered_by=user)
        else:
            entry.pieces += missing
            entry.save(update_fields=['pieces', 'updated_at'])
    values = {'state': 'SHORT' if short else 'COMPLETE', 'pieces': count, 'marked': True, 'filled': max(0, missing),
              'fill_entry': entry, 'reason': reason, 'set_by': user, 'finished_at': factory_now(dataset_id, factory_id)}
    try:
        with transaction.atomic():
            reopened = StepStatus.objects.filter(dataset_id=dataset_id, factory_id=factory_id,
                                                 operation_key=operation_key, state='OPEN')
            if not reopened.update(**values):
                StepStatus.objects.create(dataset_id=dataset_id, factory_id=factory_id, operation_key=operation_key,
                                          **values)
    except IntegrityError:  # completed at the same moment (a double click, or the simulated factory)
        raise ValidationError('This step is already complete')
    how = ('completed without hourly counts' if me['recorded'] == 0 else
           f'{missing} pieces added to the {me["recorded"]} recorded' if missing else 'all pieces recorded')
    planning_services.event(None, user, 'STEP_COMPLETED',
                            f'Order {op["order_id"]} step {op["step"]} (line {op["line"]}): '
                            f'{"closed short at" if short else "complete,"} {count} of {me["needed"]} pieces, {how}'
                            + (f'. Reason: {reason}' if reason else ''))
    _write(dataset_id, 'STEP', _step_done_sql(operation_key), f'order {op["order_id"]} step {op["step"]} done')
    refresh(dataset_id, factory_id, plan, user)
    return step_states(dataset_id, factory_id, plan)[operation_key]


@transaction.atomic
def reopen_step(user, *, dataset_id, factory_id, operation_key, reason=''):
    """Production manager: undo a step's completion (and the pieces Mark complete added for it)."""
    services.require(user, 'planning.approve_plan')
    services.check_scope(user, factory_id)
    row = (StepStatus.objects.select_for_update()
           .filter(dataset_id=dataset_id, factory_id=factory_id, operation_key=operation_key, state__in=DONE).first())
    if row is None:
        raise ValidationError('This step is not complete')
    order_id = operation_key.partition('_')[0]
    plan = plan_for(dataset_id, factory_id, order_id)
    if plan is not None and operation_key in plan.ops:
        mine = plan.ops[operation_key]
        later = [o for o in _order_ops(plan, order_id) if int(o['step']) > int(mine['step'])]
        if later and StepStatus.objects.filter(dataset_id=dataset_id, factory_id=factory_id, operation_key=later[0]['key'],
                                               state__in=DONE).exists():
            raise ValidationError(f'Step {later[0]["step"]} after it is complete. Reopen step {later[0]["step"]} first')
    order = OrderStatus.objects.filter(dataset_id=dataset_id, factory_id=factory_id, order_id=order_id).first()
    if order is not None and order.stage in LOCKED:
        raise ValidationError(f'Order {order_id} is already {order.get_stage_display().lower()}. Reopen the order first')
    sewn = order is not None
    if order is not None:
        order.delete()  # back in production
    if row.fill_entry_id and row.filled:
        entry = OutputEntry.objects.filter(pk=row.fill_entry_id).first()
        if entry is not None:
            entry.pieces = max(0, entry.pieces - row.filled)
            entry.delete() if entry.pieces == 0 else entry.save(update_fields=['pieces', 'updated_at'])
    reason = (reason or '').strip()[:MAX_TEXT]
    row.state, row.pieces, row.marked, row.filled, row.fill_entry, row.reason, row.set_by = (
        'OPEN', 0, False, 0, None, reason, user)
    row.save()  # stays open (never completes by itself) until someone marks it complete again
    from erp import writer
    _write(dataset_id, 'STEP', writer.step_reopen_statements(order_id, operation_key.partition('_')[2], sewn),
           f'order {order_id} step reopened')
    planning_services.event(None, user, 'STEP_REOPENED', f'Order {order_id} step {operation_key.partition("_")[2]} '
                                                         f'reopened' + (f'. Reason: {reason}' if reason else ''))


@transaction.atomic
def mark_reworked(user, check_id):
    """The defective pieces of a quality check were repaired: they count as done again."""
    check = DefectCheck.objects.select_for_update().select_related('run').filter(pk=check_id).first()
    if check is None:
        raise ValidationError('Quality check not found')
    if not (user.has_perm('floor.record_defect') or user.has_perm('floor.enter_output')):
        raise PermissionDenied('Permission required: record_defect')
    services.check_scope(user, check.run.factory_id, check.line_id)
    if check.reworked_at is not None:
        raise ValidationError('These pieces are already marked reworked')
    check.reworked_at, check.reworked_by = timezone.now(), user
    check.save(update_fields=['reworked_at', 'reworked_by'])
    order, _, step = check.operation_key.partition('_')
    planning_services.event(None, user, 'REWORK_DONE', f'Order {order}: {min(check.checked, check.defects)} defective '
                                                       f'pieces reworked (operation {step})')
    try:
        _run, plan = services.floor_plan(check.run.dataset_id, check.run.factory_id)
    except ValidationError:
        return check
    refresh(check.run.dataset_id, check.run.factory_id, plan, user)
    return check


def refresh(dataset_id, factory_id, plan, user, now=None):
    """Record steps that just filled up by themselves (their finish time) and orders whose every step is done."""
    now = now or factory_now(dataset_id, factory_id)
    states = step_states(dataset_id, factory_id, plan)
    for key, s in states.items():
        if s['state'] == 'COMPLETE' and s['by'] is None and not s['reopened']:
            StepStatus.objects.get_or_create(dataset_id=dataset_id, factory_id=factory_id, operation_key=key, defaults={
                'state': 'COMPLETE', 'pieces': s['needed'], 'set_by': user, 'finished_at': now})
            planning_services.event(None, user, 'STEP_COMPLETED', f'Order {s["order_id"]} step {s["step"]} (line '
                                                                  f'{s["line"]}): complete, all {s["needed"]} pieces')
            _write(dataset_id, 'STEP', _step_done_sql(key), f'order {s["order_id"]} step {s["step"]} done')
    for order_id in {s['order_id'] for s in states.values()}:
        _ensure_sewn(dataset_id, factory_id, order_id, states, user, now)


def _ensure_sewn(dataset_id, factory_id, order_id, states, user, now=None):
    """The order's status row, created as Sewn when every step of it is done; None while it is in production."""
    row = OrderStatus.objects.filter(dataset_id=dataset_id, factory_id=factory_id, order_id=order_id).first()
    mine = [s for s in states.values() if s['order_id'] == order_id]
    if row is not None or not mine or any(s['state'] not in DONE for s in mine):
        return row
    row, created = OrderStatus.objects.get_or_create(dataset_id=dataset_id, factory_id=factory_id, order_id=order_id,
                                                     defaults={'stage': 'SEWN', 'updated_by': user,
                                                               'sewn_at': now or factory_now(dataset_id, factory_id)})
    if created:
        planning_services.event(None, user, 'ORDER_SEWN', f'Order {order_id}: every step is complete; ready for final '
                                                          f'inspection')
        _order_write(dataset_id, row)
    return row


# ---- orders -------------------------------------------------------------------------------------------------------

def order_stage(dataset_id, factory_id, order_id):
    """NOT_STARTED, IN_PRODUCTION, SEWN, QC_PASSED, QC_FAILED, PACKED, SHIPPED or CLOSED."""
    row = OrderStatus.objects.filter(dataset_id=dataset_id, factory_id=factory_id, order_id=str(order_id)).first()
    if row is not None:
        return row.stage
    plan = plan_for(dataset_id, factory_id, order_id)
    if plan is None:
        return 'NOT_STARTED'
    mine = [s for s in step_states(dataset_id, factory_id, plan).values() if s['order_id'] == str(order_id)]
    if mine and all(s['state'] in DONE for s in mine):
        return 'SEWN'
    return 'IN_PRODUCTION' if any(s['recorded'] or s['state'] in DONE for s in mine) else 'NOT_STARTED'


def order_info(row):
    return {'order_id': row.order_id, 'stage': row.stage, 'stage_label': row.get_stage_display(),
            'sewn_at': row.sewn_at.isoformat() if row.sewn_at else None, 'inspection_note': row.inspection_note,
            'cartons': row.cartons, 'carrier': row.carrier, 'tracking': row.tracking,
            'shipped_on': row.shipped_on.isoformat() if row.shipped_on else None, 'close_reason': row.close_reason,
            'updated_by': row.updated_by.get_username(), 'updated_at': row.updated_at.isoformat()}


def _order(user, perm, dataset_id, factory_id, order_id):
    services.require(user, perm)
    services.check_scope(user, factory_id)
    services._dataset(dataset_id, factory_id)
    order_id = str(order_id).strip()
    plan = plan_for(dataset_id, factory_id, order_id)
    if plan is None:
        raise ValidationError('This order is not in an approved plan of this factory')
    row = OrderStatus.objects.select_for_update().filter(dataset_id=dataset_id, factory_id=factory_id,
                                                         order_id=order_id).first()
    if row is None:
        row = _ensure_sewn(dataset_id, factory_id, order_id, step_states(dataset_id, factory_id, plan), user)
    return order_id, row


def _save(row, user, action, message, write=True):
    row.updated_by = user
    row.save()
    planning_services.event(None, user, action, message)
    if write:
        _order_write(row.dataset_id, row)
    return order_info(row)


@transaction.atomic
def inspect_order(user, *, dataset_id, factory_id, order_id, result, note=''):
    order_id, row = _order(user, 'floor.record_defect', dataset_id, factory_id, order_id)
    if row is None or row.stage not in INSPECTABLE:
        raise ValidationError('Final inspection comes after every step of the order is sewn, before packing')
    result, note = str(result or '').upper(), (note or '').strip()[:MAX_TEXT]
    if result not in ('PASS', 'FAIL'):
        raise ValidationError('Choose pass or fail')
    if result == 'FAIL' and not note:
        raise ValidationError('Say what failed, so the line knows what to rework')
    row.stage, row.inspection_note = ('QC_PASSED' if result == 'PASS' else 'QC_FAILED'), note
    return _save(row, user, 'ORDER_INSPECTED', f'Order {order_id}: final inspection '
                                               f'{"passed" if result == "PASS" else "failed"}' + (f'. {note}' if note else ''),
                 write=False)


@transaction.atomic
def pack_order(user, *, dataset_id, factory_id, order_id, cartons):
    order_id, row = _order(user, 'floor.ship_order', dataset_id, factory_id, order_id)
    if row is None or row.stage != 'QC_PASSED':
        raise ValidationError('Pack the order after its final inspection passes')
    try:
        cartons = int(cartons)
    except (TypeError, ValueError):
        cartons = 0
    if not 1 <= cartons <= 100000:
        raise ValidationError('Enter the number of cartons')
    row.stage, row.cartons = 'PACKED', cartons
    return _save(row, user, 'ORDER_PACKED', f'Order {order_id}: packed in {cartons} cartons')


@transaction.atomic
def ship_order(user, *, dataset_id, factory_id, order_id, carrier, tracking='', shipped_on=None):
    order_id, row = _order(user, 'floor.ship_order', dataset_id, factory_id, order_id)
    if row is None or row.stage != 'PACKED':
        raise ValidationError('Ship the order after it is packed')
    carrier, tracking = (carrier or '').strip(), (tracking or '').strip()
    if not carrier or len(carrier) > 100 or len(tracking) > 100:
        raise ValidationError('Enter the carrier (and the tracking number, up to 100 characters)')
    if shipped_on:
        try:
            shipped_on = date.fromisoformat(str(shipped_on))
        except ValueError:
            raise ValidationError('Shipping date must look like 2026-11-23')
    else:
        shipped_on = _local_now(dataset_id, factory_id).date()
    row.stage, row.carrier, row.tracking, row.shipped_on = 'SHIPPED', carrier, tracking, shipped_on
    return _save(row, user, 'ORDER_SHIPPED', f'Order {order_id}: shipped {shipped_on} with {carrier}'
                                             + (f', tracking {tracking}' if tracking else ''))


@transaction.atomic
def close_order(user, *, dataset_id, factory_id, order_id, reason=''):
    order_id, row = _order(user, 'floor.close_order', dataset_id, factory_id, order_id)
    reason = (reason or '').strip()[:MAX_TEXT]
    if row is not None and row.stage == 'CLOSED':
        raise ValidationError('This order is already closed')
    if (row is None or row.stage != 'SHIPPED') and not reason:
        raise ValidationError('Give a reason for closing an order that has not shipped')
    if row is None:
        row = OrderStatus(dataset_id=dataset_id, factory_id=factory_id, order_id=order_id)
    row.stage, row.close_reason = 'CLOSED', reason
    return _save(row, user, 'ORDER_CLOSED', f'Order {order_id}: closed' + (f'. Reason: {reason}' if reason else ''),
                 write=False)


@transaction.atomic
def reopen_order(user, *, dataset_id, factory_id, order_id, reason=''):
    """Production manager: back to Sewn (or to production when it was closed before sewing finished)."""
    order_id, row = _order(user, 'planning.approve_plan', dataset_id, factory_id, order_id)
    reason = (reason or '').strip()[:MAX_TEXT]
    if row is None or row.stage == 'SEWN':
        raise ValidationError('There is nothing to reopen: the order is not past sewing')
    if not reason:
        raise ValidationError('Give a reason for reopening the order')
    message = f'Order {order_id}: reopened from {row.get_stage_display().lower()}. Reason: {reason}'
    if row.sewn_at is None:
        row.delete()
        planning_services.event(None, user, 'ORDER_REOPENED', message)
        _write(dataset_id, 'ORDER', _reopen_sql(order_id, sewn=False), f'order {order_id} reopened')
        return None
    row.stage, row.inspection_note, row.cartons, row.carrier, row.tracking, row.shipped_on, row.close_reason = (
        'SEWN', '', None, '', '', None, '')
    _save(row, user, 'ORDER_REOPENED', message, write=False)
    _write(dataset_id, 'ORDER', _reopen_sql(order_id, sewn=True), f'order {order_id} reopened')
    return order_info(row)


def _reopen_sql(order_id, sewn):
    from erp import writer
    return writer.order_reopen_statements(order_id, sewn)


def _order_write(dataset_id, row):
    from erp import writer
    statements = writer.order_statements(row.order_id, row.stage, carrier=row.carrier, tracking=row.tracking,
                                         shipped_on=row.shipped_on)
    if statements:
        _write(dataset_id, 'ORDER', statements, f'order {row.order_id} {row.get_stage_display().lower()}')


def statuses(dataset_id, factory_id=None):
    """{order id: status} of orders past sewing (for the Orders page and the portal)."""
    rows = OrderStatus.objects.filter(dataset_id=dataset_id).select_related('updated_by')
    if factory_id is not None:
        rows = rows.filter(factory_id=factory_id)
    return {r.order_id: order_info(r) for r in rows}
