"""Planning transactions: request, cancel, claim, finish, approve or reject."""
from datetime import timedelta
from pathlib import Path
import time
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from .datasets import factory_of
from .models import Dataset, OrderPriority, PlanEvent, PlanRun

ACTIVE = ('QUEUED', 'RUNNING')
WORKER_SILENCE_SECONDS = 60  # the worker beats every few seconds, idle or busy
MAX_ORDERS = 30


def authorize(user, permission):
    if not user.is_authenticated or not user.has_perm('planning.' + permission):
        raise PermissionDenied('Permission required: ' + permission)


def event(run, actor, action, message):
    PlanEvent.objects.create(run=run, actor=actor if actor and actor.is_authenticated else None,
                             action=action, message=message)


@transaction.atomic
def request_schedule(user, *, dataset_id, factory_id, orders, order_offset, mode, order_ids=None, trigger=None):
    """A plan of `orders` orders from position `order_offset`, or exactly `order_ids` when given."""
    authorize(user, 'request_plan')
    dataset = Dataset.objects.filter(pk=dataset_id).first()
    if dataset is None:
        raise ValidationError('Dataset not found')
    factory = factory_of(dataset, factory_id)
    if factory is None:
        raise ValidationError(f'Factory {factory_id} is not in this capture')
    if mode not in ('FULL', 'QUICK'):
        raise ValidationError('Mode must be FULL or QUICK')
    ids = [str(int(o)) for o in order_ids] if order_ids else []
    if ids:
        orders, order_offset = len(ids), 0
    if not 1 <= int(orders) <= MAX_ORDERS:
        raise ValidationError(f'Choose between 1 and {MAX_ORDERS} orders')
    if int(order_offset) < 0:
        raise ValidationError('The first order position cannot be negative')
    priority = priority_orders(dataset.pk, factory_id)
    run = PlanRun.objects.create(kind='SCHEDULE', dataset=dataset, factory_id=int(factory_id), orders=int(orders),
                                 order_offset=int(order_offset), mode=mode, requested_by=user,
                                 stage='Waiting for the worker', order_ids=ids, trigger=trigger or {},
                                 scenario={'priority_orders': priority} if priority else {})
    event(run, user, 'PLAN_REQUESTED',
          f'{mode.title()} CRAFT V3 plan: {dataset.label}, factory {factory_id}, '
          f'{orders} orders from position {order_offset + 1}')
    return run


@transaction.atomic
def request_capture(user, database=None):
    """Read-only capture of the factory database; database = which one (default: the repository .env's)."""
    authorize(user, 'request_plan')
    if PlanRun.objects.filter(kind='CAPTURE', status__in=ACTIVE).exists():
        raise ValidationError('A database capture is already queued or running')
    run = PlanRun.objects.create(kind='CAPTURE', requested_by=user, mode='FULL', stage='Waiting for the worker',
                                 scenario={'database': database} if database else {})
    event(run, user, 'CAPTURE_REQUESTED', 'Read-only capture of the factory database (SELECT-only account)')
    return run


@transaction.atomic
def cancel(user, run_id, reason):
    authorize(user, 'request_plan')
    if not reason.strip():
        raise ValidationError('Give a reason for cancelling')
    changed = PlanRun.objects.filter(pk=run_id, status__in=ACTIVE).update(status='CANCELLED',
                                                                         finished_at=timezone.now(),
                                                                         stage='Cancelled')
    if changed:
        event(PlanRun.objects.get(pk=run_id), user, 'RUN_CANCELLED', reason)
    return bool(changed)


@transaction.atomic
def decide(user, run_id, decision, reason):
    """Manager decision on a finished plan. TEST approval only: production
    activation stays disabled (settings.CRAFT_PRODUCTION_ACTIVATION)."""
    authorize(user, 'approve_plan')
    run = PlanRun.objects.select_for_update().get(pk=run_id)
    if decision not in ('APPROVED', 'REJECTED'):
        raise ValidationError('Decision must be APPROVED or REJECTED')
    if not reason.strip():
        raise ValidationError('Give a reason for the decision')
    if run.kind != 'SCHEDULE' or run.status != 'SUCCEEDED':
        raise ValidationError('Only a finished, validated plan can be decided')
    if run.decision != 'PENDING':
        raise ValidationError(f'This plan was already {run.decision.lower()}')
    run.decision, run.decided_by, run.decided_at, run.decision_reason = decision, user, timezone.now(), reason
    run.save(update_fields=['decision', 'decided_by', 'decided_at', 'decision_reason'])
    event(run, user, 'PLAN_' + decision, reason)
    if decision == 'APPROVED':  # write into the demo copy after the decision is saved (off unless switched on)
        from erp import writer
        transaction.on_commit(lambda: writer.write_plan(run))
    return run


@transaction.atomic
def claim():
    """Compare-and-swap claim of the oldest queued run (safe on SQLite)."""
    run = PlanRun.objects.filter(status='QUEUED').order_by('created_at').first()
    if run is None:
        return None
    now = timezone.now()
    if PlanRun.objects.filter(pk=run.pk, status='QUEUED').update(status='RUNNING', started_at=now,
                                                                 heartbeat_at=now) != 1:
        return None
    run.refresh_from_db()
    event(run, None, 'RUN_STARTED', 'Worker started the run')
    return run


def still_running(run_id):
    return PlanRun.objects.filter(pk=run_id, status='RUNNING').exists()


def beat(run_id, **fields):
    PlanRun.objects.filter(pk=run_id, status='RUNNING').update(heartbeat_at=timezone.now(), **fields)


def finish(run_id, status, **fields):
    changed = PlanRun.objects.filter(pk=run_id, status='RUNNING').update(
        status=status, finished_at=timezone.now(), heartbeat_at=timezone.now(), **fields)
    if changed:
        run = PlanRun.objects.get(pk=run_id)
        done = ('Capture saved and checked' if run.kind == 'CAPTURE'
                else 'Finished; every plan passed the physical validation')
        text = {'SUCCEEDED': done,
                'FAILED': 'Failed: ' + (fields.get('error') or 'see the log')}.get(status, status.title())
        event(run, None, 'RUN_' + status, text)
    return bool(changed)


def _worker_file():
    return Path(settings.PLANNING_RUNS_DIR) / 'worker.alive'


def worker_beat():
    """Called by the planning worker on every loop, so the app can tell whether one is running."""
    path = _worker_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()), encoding='utf-8')


def worker_alive():
    try:
        return time.time() - _worker_file().stat().st_mtime < WORKER_SILENCE_SECONDS
    except OSError:
        return False


def expire_orphans(max_silence_seconds=180):
    """A RUNNING run whose worker stopped beating failed; never retried automatically."""
    cutoff = timezone.now() - timedelta(seconds=max_silence_seconds)
    for run in PlanRun.objects.filter(status='RUNNING', heartbeat_at__lt=cutoff):
        finish(run.pk, 'FAILED', stage='Interrupted', error='The worker stopped while this run was in progress. '
                                                             'Start a new run.')


def _plan_orders(parent):
    """Order ids of a finished plan (from its digest), so a what-if plans exactly the same orders."""
    import json
    from pathlib import Path
    path = Path(parent.out_dir) / 'digest.json' if parent.out_dir else None
    if not path or not path.exists():
        raise ValidationError('The original plan has no saved result')
    rows = json.loads(path.read_text(encoding='utf-8')).get('order_rows', ())
    ids = [r['order_id'] for r in rows]
    if parent.scenario.get('new_order'):
        ids = [i for i in ids if i != str(parent.scenario['new_order']['order_id'])]
    return ids, parent.scenario


def _finished_plan(parent_id):
    parent = PlanRun.objects.filter(pk=parent_id).select_related('dataset').first()
    if parent is None or parent.kind != 'SCHEDULE' or parent.status != 'SUCCEEDED':
        raise ValidationError('Choose a finished plan to start from')
    return parent


def _day(iso):
    """'2026-09-15' -> '15 Sep' (for history notes and emails)."""
    from datetime import date
    return date.fromisoformat(iso).strftime('%d %b').lstrip('0')


@transaction.atomic
def request_disruption(user, *, parent_id, machines_down=(), workers_absent=(), lines_stopped=(), release_after=None,
                       restored=None,
                       priority_orders=None, new_order=None, mode='QUICK', trigger=None):
    """Re-plan the same orders around changes that last the whole plan: machines out of service,
    workers absent, whole lines stopped, orders that cannot start before their material arrives,
    and orders marked Priority. Inherited changes from the parent plan are kept."""
    authorize(user, 'request_plan')
    parent = _finished_plan(parent_id)
    machines = sorted({int(m) for m in machines_down})
    workers = sorted({int(w) for w in workers_absent})
    lines = sorted({int(x) for x in lines_stopped})
    release = {str(k): str(v)[:10] for k, v in (release_after or {}).items()}
    priority = sorted({str(o) for o in (priority_orders or ())})
    priority_removed = bool(((trigger or {}).get('priority') or {}).get('off'))
    if not (machines or workers or lines or release or priority or new_order or priority_removed):
        raise ValidationError('Choose at least one change to plan around')
    if mode not in ('FULL', 'QUICK'):
        raise ValidationError('Mode must be FULL or QUICK')
    ids, inherited = _plan_orders(parent)
    # Priority is today's list (marked orders), not the parent's: a removed Priority goes away.
    current_priority = sorted(set(priority) | set(_active_priorities(parent.dataset_id, parent.factory_id)))
    scenario = {k: v for k, v in inherited.items() if k not in ('new_order', 'priority_orders')}
    for key, back in (restored or {}).items():  # repaired / back at work / running again: no longer left out
        if key in ('machines_down', 'workers_absent', 'lines_stopped') and key in scenario:
            scenario[key] = [x for x in scenario[key] if x not in set(back)]
    scenario['machines_down'] = sorted(set(scenario.get('machines_down', [])) | set(machines))
    scenario['workers_absent'] = sorted(set(scenario.get('workers_absent', [])) | set(workers))
    if lines or scenario.get('lines_stopped'):
        scenario['lines_stopped'] = sorted(set(scenario.get('lines_stopped', [])) | set(lines))
    if release or scenario.get('release_after'):
        merged = dict(scenario.get('release_after', {}))
        for oid, when in release.items():
            merged[oid] = max(merged.get(oid, when), when)  # the later arrival wins
        scenario['release_after'] = merged
    if current_priority:
        scenario['priority_orders'] = current_priority
    added = new_order or inherited.get('new_order')
    if added:
        scenario['new_order'] = added
        ids = ids + [str(added['order_id'])]
    run = PlanRun.objects.create(kind='SCHEDULE', purpose='DISRUPTION', parent=parent, dataset=parent.dataset,
                                 factory_id=parent.factory_id, orders=len(ids), order_offset=parent.order_offset,
                                 order_ids=ids, scenario=scenario, mode=mode, requested_by=user,
                                 stage='Waiting for the worker', trigger=trigger or {})
    parts = ([f'machine{"s" if len(machines) > 1 else ""} {", ".join(map(str, machines))} out of service'] if machines else []) +             ([f'worker{"s" if len(workers) > 1 else ""} {", ".join(map(str, workers))} absent'] if workers else []) +             [f'line {x} stopped' for x in lines] +             [f'order {o} waits for material until {_day(d)}' for o, d in sorted(release.items())] +             [f'order {o} marked Priority' for o in priority] +             ([f'new order {new_order["order_id"]}'] if new_order else [])
    event(run, user, 'DISRUPTION_REQUESTED', f'Re-plan after disruption: {" and ".join(parts)}')
    return run


@transaction.atomic
def request_promise(user, *, parent_id, product_id, quantity, release, due, routing, mode='QUICK', style_code='',
                    priority=False):
    """Can we accept a new order? Plan it together with a finished plan's orders."""
    from datetime import date
    authorize(user, 'request_plan')
    parent = _finished_plan(parent_id)
    if mode not in ('FULL', 'QUICK'):
        raise ValidationError('Mode must be FULL or QUICK')
    if not 1 <= int(quantity) <= 20000:
        raise ValidationError('Quantity must be between 1 and 20,000 pieces')
    try:
        release_d, due_d = date.fromisoformat(str(release)), date.fromisoformat(str(due))
    except ValueError as exc:
        raise ValidationError('Dates must be YYYY-MM-DD') from exc
    if due_d < release_d:
        raise ValidationError('The due date cannot be before the release date')
    if not routing:
        raise ValidationError('This style has no routing')
    ids, inherited = _plan_orders(parent)
    order_id = 990000 + PlanRun.objects.filter(purpose='PROMISE').count() + 1
    scenario = {k: v for k, v in inherited.items() if k != 'new_order'}
    scenario['new_order'] = {'order_id': order_id, 'product_id': int(product_id), 'style_code': str(style_code or product_id),
                             'quantity': int(quantity),
                             'release': release_d.isoformat(), 'due': due_d.isoformat(), 'routing': routing}
    run = PlanRun.objects.create(kind='SCHEDULE', purpose='PROMISE', parent=parent, dataset=parent.dataset,
                                 factory_id=parent.factory_id, orders=len(ids) + 1, order_offset=parent.order_offset,
                                 order_ids=ids + [str(order_id)], scenario=scenario, mode=mode, requested_by=user,
                                 stage='Waiting for the worker')
    event(run, user, 'PROMISE_REQUESTED',
          f'Can we accept {quantity} pieces of style {style_code or product_id}, due {due_d.isoformat()}? (new order {order_id})')
    if priority:  # an urgent order: with auto-planning on, CRAFT also re-plans with it marked Priority
        from . import autoplan
        autoplan.record(parent.dataset_id, parent.factory_id, 'URGENT_ORDER', run.pk)
    return run


# ---------- priority orders ----------

def _active_priorities(dataset_id, factory_id):
    """Order ids marked Priority for this capture and factory (CRAFT plans them first)."""
    return sorted(OrderPriority.objects.filter(dataset_id=dataset_id, factory_id=int(factory_id), active=True)
                  .values_list('order_id', flat=True), key=lambda o: (len(o), o))


priority_orders = _active_priorities  # public name (request_disruption has a parameter of that name)


@transaction.atomic
def set_priority(user, *, dataset_id, factory_id, order_id, on, reason=''):
    """Mark an order Priority (on=True) or remove it; recorded in the history."""
    authorize(user, 'request_plan')
    if not Dataset.objects.filter(pk=dataset_id).exists():
        raise ValidationError('Dataset not found')
    order_id = str(order_id).strip()
    if not order_id.isdigit():
        raise ValidationError('Order numbers are digits only')
    row, _created = OrderPriority.objects.update_or_create(
        dataset_id=dataset_id, factory_id=int(factory_id), order_id=order_id,
        defaults={'active': bool(on), 'reason': (reason or '').strip()[:500], 'set_by': user})
    from . import autoplan
    autoplan.record(dataset_id, factory_id, 'PRIORITY', order_id)
    event(None, user, 'PRIORITY_ON' if on else 'PRIORITY_OFF',
          f'Order {order_id} {"marked Priority" if on else "no longer Priority"} (factory {factory_id})'
          + (f': {row.reason}' if on and row.reason else ''))
    return row
