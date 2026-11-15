"""Planning transactions: request, cancel, claim, finish, approve or reject."""
from datetime import timedelta
from pathlib import Path
import time
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from .datasets import factory_of
from .models import Dataset, PlanEvent, PlanRun

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
def request_schedule(user, *, dataset_id, factory_id, orders, order_offset, mode):
    authorize(user, 'request_plan')
    dataset = Dataset.objects.filter(pk=dataset_id).first()
    if dataset is None:
        raise ValidationError('Dataset not found')
    factory = factory_of(dataset, factory_id)
    if factory is None:
        raise ValidationError(f'Factory {factory_id} is not in this capture')
    if mode not in ('FULL', 'QUICK'):
        raise ValidationError('Mode must be FULL or QUICK')
    if not 1 <= int(orders) <= MAX_ORDERS:
        raise ValidationError(f'Choose between 1 and {MAX_ORDERS} orders')
    if int(order_offset) < 0:
        raise ValidationError('The first order position cannot be negative')
    run = PlanRun.objects.create(kind='SCHEDULE', dataset=dataset, factory_id=int(factory_id), orders=int(orders),
                                 order_offset=int(order_offset), mode=mode, requested_by=user,
                                 stage='Waiting for the worker')
    event(run, user, 'PLAN_REQUESTED',
          f'{mode.title()} CRAFT V3 plan: {dataset.label}, factory {factory_id}, '
          f'{orders} orders from position {order_offset + 1}')
    return run


@transaction.atomic
def request_capture(user):
    authorize(user, 'request_plan')
    if PlanRun.objects.filter(kind='CAPTURE', status__in=ACTIVE).exists():
        raise ValidationError('A database capture is already queued or running')
    run = PlanRun.objects.create(kind='CAPTURE', requested_by=user, mode='FULL', stage='Waiting for the worker')
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


@transaction.atomic
def request_disruption(user, *, parent_id, machines_down=(), workers_absent=(), mode='QUICK'):
    """Re-plan the same orders with machines out of service and/or workers absent (whole plan)."""
    authorize(user, 'request_plan')
    parent = _finished_plan(parent_id)
    machines = sorted({int(m) for m in machines_down})
    workers = sorted({int(w) for w in workers_absent})
    if not machines and not workers:
        raise ValidationError('Choose at least one machine or worker that is unavailable')
    if mode not in ('FULL', 'QUICK'):
        raise ValidationError('Mode must be FULL or QUICK')
    ids, inherited = _plan_orders(parent)
    scenario = {k: v for k, v in inherited.items() if k != 'new_order'}
    scenario['machines_down'] = sorted(set(scenario.get('machines_down', [])) | set(machines))
    scenario['workers_absent'] = sorted(set(scenario.get('workers_absent', [])) | set(workers))
    if inherited.get('new_order'):
        scenario['new_order'] = inherited['new_order']
        ids = ids + [str(inherited['new_order']['order_id'])]
    run = PlanRun.objects.create(kind='SCHEDULE', purpose='DISRUPTION', parent=parent, dataset=parent.dataset,
                                 factory_id=parent.factory_id, orders=len(ids), order_offset=parent.order_offset,
                                 order_ids=ids, scenario=scenario, mode=mode, requested_by=user,
                                 stage='Waiting for the worker')
    parts = ([f'machine{"s" if len(machines) > 1 else ""} {", ".join(map(str, machines))} out of service'] if machines else []) + \
            ([f'worker{"s" if len(workers) > 1 else ""} {", ".join(map(str, workers))} absent'] if workers else [])
    event(run, user, 'DISRUPTION_REQUESTED', f'Re-plan after disruption: {" and ".join(parts)}')
    return run


@transaction.atomic
def request_promise(user, *, parent_id, product_id, quantity, release, due, routing, mode='QUICK', style_code=''):
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
    return run
