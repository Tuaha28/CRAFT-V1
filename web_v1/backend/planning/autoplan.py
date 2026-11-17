"""Auto-planning: CRAFT re-plans by itself after problems on the floor, Priority changes and urgent orders.

Triggers are stored as they happen (record). The planning worker calls tick() every minute:
for each factory with auto-planning on and an approved plan, it nets the triggers (a resolved
problem or a Priority switched back no longer counts), waits until things are quiet
(AUTO_PLAN_QUIET_MINUTES after the newest trigger, at most AUTO_PLAN_MAX_WAIT_MINUTES after
the oldest), and queues one Quick re-plan from the approved plan. A manager approves every plan;
a newer automatic plan replaces one still waiting for review, and a rejected one is not retried.
"""
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo
from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from . import jobs, services
from .models import AutoPlanSetting, AutoTrigger, Dataset, PlanRun

SYSTEM_USER = 'craft_auto'
log = logging.getLogger(__name__)


def factory_time(moment):
    """A moment on the factory's clock (FACTORY_TIME_ZONE)."""
    return moment.astimezone(ZoneInfo(settings.FACTORY_TIME_ZONE))


def system_user():
    """The requester of automatic runs: cannot sign in, may only request plans."""
    user, created = User.objects.get_or_create(username=SYSTEM_USER, defaults={'first_name': 'CRAFT'})
    if created:
        user.set_unusable_password()
        user.save(update_fields=['password'])
    if not user.has_perm('planning.request_plan'):
        user.user_permissions.add(Permission.objects.get(codename='request_plan', content_type__app_label='planning'))
        user = User.objects.get(pk=user.pk)  # fresh permission cache
    return user


def enabled(dataset_id, factory_id):
    return AutoPlanSetting.objects.filter(dataset_id=dataset_id, factory_id=int(factory_id), enabled=True).exists()


def record(dataset_id, factory_id, kind, ref):
    """Store a trigger when auto-planning is on for this factory; None when it is off."""
    if not enabled(dataset_id, factory_id):
        return None
    return AutoTrigger.objects.create(dataset_id=dataset_id, factory_id=int(factory_id), kind=kind, ref=str(ref))


def set_enabled(user, *, dataset_id, factory_id, on):
    services.authorize(user, 'request_plan')
    AutoPlanSetting.objects.update_or_create(dataset_id=dataset_id, factory_id=int(factory_id),
                                             defaults={'enabled': bool(on), 'set_by': user})
    services.event(None, user, 'AUTOPLAN_ON' if on else 'AUTOPLAN_OFF',
                   f'Auto-planning {"switched on" if on else "switched off"} for factory {factory_id}')
    return bool(on)


def _issue_text(issue):
    if issue.kind == 'MACHINE_DOWN':
        return f'Machine {issue.machine_id} down'
    if issue.kind == 'WORKER_ABSENT':
        return f'Worker {issue.worker_id} absent'
    if issue.kind == 'LINE_STOPPED':
        return f'Line {issue.line_id} stopped'
    when = f' until {services._day(issue.arrival_date.isoformat())}' if issue.arrival_date else ''
    return f'Material {issue.material_id} late{when}'


def _live(triggers, approved, current_priority):
    """The triggers that still ask for a change, and what they ask for."""
    from floor.models import Issue
    issues, on, off, promises, live = [], set(), set(), [], []
    planned_priority = set((approved.scenario or {}).get('priority_orders', []))
    for t in triggers:
        if t.kind == 'ISSUE':
            issue = Issue.objects.filter(pk=t.ref).select_related('dataset').first()
            if issue is None or issue.status == 'RESOLVED':
                continue
            issues.append(issue)
        elif t.kind == 'PRIORITY':
            now_on = t.ref in current_priority
            if now_on == (t.ref in planned_priority):
                continue  # switched back: the approved plan already has it this way
            (on if now_on else off).add(t.ref)
        elif t.kind == 'URGENT_ORDER':
            promise = PlanRun.objects.filter(pk=t.ref).first()
            if promise is None or not promise.scenario.get('new_order'):
                continue
            promises.append(promise)
        else:
            continue  # NEW_ORDERS is handled by the capture path (check_new_orders)
        live.append(t)
    return live, issues, sorted(on), sorted(off), promises


def _queue(approved, live, issues, on, off, promises):
    from floor.models import Issue
    from floor.services import disruption_inputs
    unique = {i.pk: i for i in issues}
    issues = list(unique.values())
    plan_ids = services._plan_orders(approved)[0]
    inputs = disruption_inputs(issues, plan_ids)
    new_order = promises[-1].scenario['new_order'] if promises else None
    reasons = [_issue_text(i) for i in issues] + [f'Order {o} marked Priority' for o in on] + \
              [f'Order {o} no longer Priority' for o in off] + \
              ([f'Urgent order {new_order["order_id"]}'] if new_order else [])
    trigger = {'source': 'AUTO', 'reason': ', '.join(reasons) or 'Changes on the floor',
               'kinds': sorted({t.kind for t in live} | {i.kind for i in issues}),
               'issue_ids': sorted(unique), 'priority': {'on': on, 'off': off},
               'new_order_ids': [str(new_order['order_id'])] if new_order else []}
    extra = [str(new_order['order_id'])] if new_order else []
    run = services.request_disruption(system_user(), parent_id=approved.pk, mode='QUICK', trigger=trigger,
                                      new_order=new_order, priority_orders=on + extra,
                                      restored=_restored(approved), **inputs)
    Issue.objects.filter(pk__in=list(unique)).update(replan=run)
    return run


RESTORABLE = {'machines_down': ('MACHINE_DOWN', 'machine_id'), 'workers_absent': ('WORKER_ABSENT', 'worker_id'),
              'lines_stopped': ('LINE_STOPPED', 'line_id')}


def _restored(approved):
    """Machines, workers and lines the approved plan still leaves out although their problem was resolved
    and no other open problem keeps them out. Ones left out by hand (no problem report) stay out."""
    from floor.models import Issue
    _ids, inherited = services._plan_orders(approved)
    restored = {}
    for key, (kind, field) in RESTORABLE.items():
        out = set(inherited.get(key) or [])
        if not out:
            continue
        issues = Issue.objects.filter(dataset_id=approved.dataset_id, factory_id=approved.factory_id, kind=kind,
                                      **{f'{field}__in': out})
        resolved = set(issues.filter(status='RESOLVED').values_list(field, flat=True))
        still = set(issues.exclude(status='RESOLVED').values_list(field, flat=True))
        if resolved - still:
            restored[key] = sorted(resolved - still)
    return restored


def _supersede(dataset_id, factory_id):
    """Pending automatic plans for this factory are replaced; their triggers join the new run."""
    carried = []
    for old in PlanRun.objects.filter(dataset_id=dataset_id, factory_id=factory_id, kind='SCHEDULE',
                                      status='SUCCEEDED', decision='PENDING', trigger__source='AUTO'):
        old.decision = 'SUPERSEDED'
        old.save(update_fields=['decision'])
        services.event(old, None, 'PLAN_SUPERSEDED', 'Replaced by a newer CRAFT plan')
        carried += list(old.auto_triggers.all())
    return carried


# ---------- new orders in the factory database ----------

def _busy():
    """Any plan or capture queued or running: heavy work runs one at a time."""
    return PlanRun.objects.filter(status__in=services.ACTIVE).exists()


def _live_order_ids(dataset, factory_id):
    """Open orders of the factory in the database the app reads now (one light SELECT)."""
    from erp.db import live_database, read_only
    from erp.queries import ORDERS_OF, rows
    with read_only(live_database(dataset)) as cursor:
        found = rows(cursor, f"SELECT o.production_order_id {ORDERS_OF} WHERE o.status IN ('Pending', 'Processing')",
                     (int(factory_id),))
    return {str(r['production_order_id']) for r in found}


_captured_cache = {}


def _captured_orders(dataset, factory_id):
    """Order ids in the capture's source file (read once per capture and factory)."""
    import json
    from pathlib import Path
    key = (dataset.path, int(factory_id))
    if key not in _captured_cache:
        source = json.loads((Path(dataset.path) / f'factory{int(factory_id)}_source.json').read_text(encoding='utf-8'))
        _captured_cache[key] = {str(int(o['production_order_id'])) for o in source.get('orders', ())}
    return _captured_cache[key]


def check_new_orders(now=None):
    """Every AUTO_PLAN_NEW_ORDER_CHECK_MINUTES per factory with auto-planning on: if the database has open
    orders the capture lacks, take a fresh capture and record a NEW_ORDERS trigger. Returns how many factories."""
    from erp.db import live_database
    now = now or timezone.now()
    every = timedelta(minutes=settings.AUTO_PLAN_NEW_ORDER_CHECK_MINUTES)
    found = 0
    for setting in AutoPlanSetting.objects.filter(enabled=True).select_related('dataset'):
        if setting.last_order_check and now - setting.last_order_check < every:
            continue
        if _busy() or AutoTrigger.objects.filter(dataset=setting.dataset, factory_id=setting.factory_id,
                                                 kind='NEW_ORDERS', consumed_at__isnull=True).exists():
            continue
        AutoPlanSetting.objects.filter(pk=setting.pk).update(last_order_check=now)
        try:
            new = sorted(_live_order_ids(setting.dataset, setting.factory_id)
                         - _captured_orders(setting.dataset, setting.factory_id), key=int)
        except Exception:  # database down or capture folder missing: try this factory again next interval
            log.exception('New-order check failed for %s factory %s', setting.dataset.slug, setting.factory_id)
            continue
        if not new:
            continue
        capture = services.request_capture(system_user(), database=live_database(setting.dataset))
        AutoTrigger.objects.create(dataset=setting.dataset, factory_id=setting.factory_id, kind='NEW_ORDERS',
                                   ref=f'{capture.pk}:{",".join(new)}'[:200], created_at=now)
        found += 1
    return found


def nightly_capture(now=None):
    """Once a day after AUTO_CAPTURE_HOUR, refresh the factory data for each dataset with auto-planning on."""
    from erp.db import live_database
    now = now or timezone.now()
    local = factory_time(now)
    if local.hour < settings.AUTO_CAPTURE_HOUR or _busy():
        return None
    for setting in AutoPlanSetting.objects.filter(enabled=True).select_related('dataset'):
        if setting.last_capture and factory_time(setting.last_capture).date() >= local.date():
            continue
        run = services.request_capture(system_user(), database=live_database(setting.dataset))
        AutoPlanSetting.objects.filter(dataset=setting.dataset).update(last_capture=now)
        return run  # one capture at a time; the next dataset gets its turn on a later minute
    return None


def _new_order_plans(now):
    """NEW_ORDERS triggers whose capture finished: plan the approved plan's orders plus the new ones on the
    fresh data, and carry the auto-planning switch over to it."""
    from floor.plan import approved_plan
    queued = []
    for t in AutoTrigger.objects.filter(kind='NEW_ORDERS', consumed_at__isnull=True).select_related('dataset'):
        capture_id, _, ids = t.ref.partition(':')
        capture = PlanRun.objects.filter(pk=capture_id).first()
        if capture is None or capture.status in ('FAILED', 'CANCELLED'):
            AutoTrigger.objects.filter(pk=t.pk).update(consumed_at=now)
            continue
        if capture.status != 'SUCCEEDED' or _busy():
            continue
        fresh = Dataset.objects.filter(slug=(capture.summary or {}).get('dataset')).first()
        approved = approved_plan(t.dataset_id, t.factory_id)
        if fresh is None or approved is None:
            AutoTrigger.objects.filter(pk=t.pk).update(consumed_at=now)
            continue
        new = [i for i in ids.split(',') if i]
        planned = services._plan_orders(approved)[0]
        order_ids = planned + [i for i in new if i not in planned]
        trigger = {'source': 'AUTO', 'reason': ', '.join(f'New order {i}' for i in new), 'kinds': ['NEW_ORDERS'],
                   'issue_ids': [], 'priority': {'on': [], 'off': []}, 'new_order_ids': new}
        try:
            run = services.request_schedule(system_user(), dataset_id=fresh.pk, factory_id=t.factory_id,
                                            orders=len(order_ids), order_offset=0, mode='QUICK',
                                            order_ids=order_ids, trigger=trigger)
        except ValidationError as exc:
            services.event(None, None, 'AUTOPLAN_SKIPPED', f'Factory {t.factory_id}: new orders not planned '
                                                             f'({"; ".join(exc.messages)})')
            AutoTrigger.objects.filter(pk=t.pk).update(consumed_at=now)
            continue
        if fresh.pk != t.dataset_id:  # normally the capture refreshed this same dataset and nothing moves
            AutoPlanSetting.objects.update_or_create(dataset=fresh, factory_id=t.factory_id,
                                                     defaults={'enabled': True, 'set_by': run.requested_by})
            AutoPlanSetting.objects.filter(dataset_id=t.dataset_id, factory_id=t.factory_id).update(enabled=False)
            services.event(run, None, 'AUTOPLAN_MOVED', f'Auto-planning for factory {t.factory_id} now follows the '
                                                        f'fresh factory data ({fresh.label})')
        AutoTrigger.objects.filter(pk=t.pk).update(consumed_at=now, run=run)
        queued.append(run)
    return queued


def tick(now=None):
    """Queue automatic re-plans that are due. Returns the runs queued."""
    from floor.plan import approved_plan
    now = now or timezone.now()
    quiet = timedelta(minutes=settings.AUTO_PLAN_QUIET_MINUTES)
    longest = timedelta(minutes=settings.AUTO_PLAN_MAX_WAIT_MINUTES)
    queued = _new_order_plans(now)
    for setting in AutoPlanSetting.objects.filter(enabled=True).select_related('dataset'):
        dataset_id, factory_id = setting.dataset_id, setting.factory_id
        pending = list(AutoTrigger.objects.filter(dataset_id=dataset_id, factory_id=factory_id, consumed_at__isnull=True)
                       .exclude(kind='NEW_ORDERS'))
        if not pending:
            continue
        approved = approved_plan(dataset_id, factory_id)
        if approved is None:
            continue
        current_priority = set(services.priority_orders(dataset_id, factory_id))
        live, issues, on, off, promises = _live(pending, approved, current_priority)
        if not live:
            AutoTrigger.objects.filter(pk__in=[t.pk for t in pending]).update(consumed_at=now)
            continue
        newest, oldest = max(t.created_at for t in live), min(t.created_at for t in live)
        if now - newest < quiet and now - oldest < longest:
            continue
        if PlanRun.objects.filter(status__in=services.ACTIVE).filter(
                kind='CAPTURE').exists() or PlanRun.objects.filter(
                dataset_id=dataset_id, factory_id=factory_id, status__in=services.ACTIVE).exists():
            continue
        try:
            with transaction.atomic():  # all or nothing: a failed queue leaves the waiting plan as it was
                carried = _supersede(dataset_id, factory_id)
                c_live, c_issues, c_on, c_off, c_promises = _live(carried, approved, current_priority)
                run = _queue(approved, live + c_live, issues + c_issues, sorted(set(on) | set(c_on)),
                             sorted(set(off) | set(c_off)), promises + c_promises)
                AutoTrigger.objects.filter(pk__in=[t.pk for t in pending + carried]).update(consumed_at=now, run=run)
        except ValidationError as exc:
            services.event(None, None, 'AUTOPLAN_SKIPPED',
                           f'Factory {factory_id}: no automatic plan ({"; ".join(exc.messages)})')
            AutoTrigger.objects.filter(pk__in=[t.pk for t in pending]).update(consumed_at=now)
            continue
        queued.append(run)
    return queued


def watch(now=None):
    """One pass of the worker's watcher: new orders, the nightly capture, then due re-plans. Each step runs
    even when an earlier one fails; returns the errors (the worker logs them)."""
    errors = []
    for name in ('check_new_orders', 'nightly_capture', 'tick', 'plan_ahead'):
        try:
            globals()[name](now)
        except Exception as exc:
            log.exception('Auto-planning step %s failed', name)
            errors.append(f'{name}: {type(exc).__name__}: {exc}')
    return errors


# ---------- plan ahead: a first plan, and the next plan before the current one runs out ----------

def _waiting(dataset_id, factory_id):
    """A plan of this factory is queued, running or waiting for a manager's decision."""
    return PlanRun.objects.filter(dataset_id=dataset_id, factory_id=factory_id, kind='SCHEDULE').filter(
        status__in=services.ACTIVE).exists() or PlanRun.objects.filter(
        dataset_id=dataset_id, factory_id=factory_id, kind='SCHEDULE', status='SUCCEEDED', decision='PENDING').exists()


def _recently_rejected(dataset_id, factory_id, kind, now):
    for run in PlanRun.objects.filter(dataset_id=dataset_id, factory_id=factory_id, decision='REJECTED',
                                      trigger__source='AUTO', decided_at__gte=now - timedelta(hours=24)):
        if kind in (run.trigger or {}).get('kinds', ()):
            return True
    return False


def _progress(approved):
    """(share of the plan's pieces recorded, unfinished order ids in plan order)."""
    from django.db.models import Sum
    from floor.models import OutputEntry
    from floor.plan import load
    plan = load(approved)
    done = {r['operation_key']: int(r['n'] or 0) for r in OutputEntry.objects.filter(
        run__dataset_id=approved.dataset_id, run__factory_id=approved.factory_id, operation_key__in=list(plan.ops))
        .values('operation_key').annotate(n=Sum('pieces'))}
    total = sum(int(op.get('pieces') or 0) for op in plan.ops.values())
    finished = sum(min(done.get(k, 0), int(op.get('pieces') or 0)) for k, op in plan.ops.items())
    orders = list(dict.fromkeys(op['order_id'] for op in sorted(plan.ops.values(), key=lambda o: (o['start'], o['key']))))
    unfinished = [o for o in orders if any(done.get(k, 0) < int(op.get('pieces') or 0)
                                           for k, op in plan.ops.items() if op['order_id'] == o)]
    return (finished / total if total else 1.0), unfinished


def _next_orders(dataset, factory_id, after, count):
    """The next `count` orders in the factory's queue after the last of `after` (release order)."""
    book = [str(o['order_id']) for o in jobs.orderbook(dataset, factory_id).get('eligible', ())]
    taken = set(after)
    last = max((book.index(o) for o in after if o in book), default=-1)
    return [o for o in book[last + 1:] if o not in taken][:count]


def plan_ahead(now=None):
    """Queue a first plan for a factory without one, or the next plan when the approved plan is nearly done.
    One heavy run at a time, smallest factory first. Returns the runs queued (at most one)."""
    from floor.plan import approved_plan
    now = now or timezone.now()
    if _busy():
        return []
    settings_on = list(AutoPlanSetting.objects.filter(enabled=True).select_related('dataset'))
    size = lambda s: next((f.get('orders') or 0 for f in s.dataset.factories if f['factory_id'] == s.factory_id), 0)
    for setting in sorted(settings_on, key=lambda s: (size(s), s.dataset_id, s.factory_id)):
        dataset, factory_id = setting.dataset, setting.factory_id
        if _waiting(dataset.pk, factory_id):
            continue
        approved = approved_plan(dataset.pk, factory_id)
        count = int(settings.AUTO_PLAN_ORDERS)
        try:
            if approved is None:
                if _recently_rejected(dataset.pk, factory_id, 'FIRST_PLAN', now):
                    continue
                trigger = {'source': 'AUTO', 'reason': f'First plan for factory {factory_id}', 'kinds': ['FIRST_PLAN'],
                           'issue_ids': [], 'priority': {'on': [], 'off': []}, 'new_order_ids': []}
                run = services.request_schedule(system_user(), dataset_id=dataset.pk, factory_id=factory_id,
                                                orders=count, order_offset=0, mode='QUICK', trigger=trigger)
            else:
                share, unfinished = _progress(approved)
                if share < float(settings.AUTO_PLAN_NEXT_AT) or _recently_rejected(dataset.pk, factory_id, 'NEXT_PLAN', now):
                    continue
                planned = services._plan_orders(approved)[0]
                new = _next_orders(dataset, factory_id, planned, count)
                if not new and not unfinished:
                    continue
                trigger = {'source': 'AUTO', 'reason': f'Next orders for factory {factory_id} '
                                                       f'(current plan {round(100 * share)}% done)',
                           'kinds': ['NEXT_PLAN'], 'issue_ids': [], 'priority': {'on': [], 'off': []},
                           'new_order_ids': new}
                run = services.request_schedule(system_user(), dataset_id=dataset.pk, factory_id=factory_id,
                                                orders=len(unfinished + new), order_offset=0, mode='QUICK',
                                                order_ids=unfinished + new, trigger=trigger)
        except (ValidationError, RuntimeError, OSError) as exc:
            log.warning('Plan ahead skipped factory %s: %s', factory_id, exc)
            continue
        return [run]  # one heavy run at a time
    return []
