"""Applies the engine to each factory clock: called by the planning worker every few seconds.

Output and quality checks become the same records a supervisor and inspector enter; breakdowns and absences
are reported and resolved through the floor services as the craft_sim user, so auto-planning, e-mail and the
Problems page work as they do for real reports."""
import logging
import random
from datetime import timedelta, timezone as dt_timezone
from django.contrib.auth.models import Group, Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from .engine import HOUR, Rates, State, advance
from .models import FactoryClock

log = logging.getLogger(__name__)
SYSTEM_USER = 'craft_sim'
RATES = None  # tests set Rates(...) here; otherwise _rates() reads the settings
MAX_HOURS_PER_TICK = 24
PERMS = ('floor.enter_output', 'floor.record_defect', 'floor.report_issue', 'floor.handle_issue',
         'floor.repair_machine')


def _rates():
    from django.conf import settings
    return RATES or Rates(breakdown_per_hour=float(getattr(settings, 'SIM_BREAKDOWN_PER_HOUR', 0.0015)),
                          absence_per_day=float(getattr(settings, 'SIM_ABSENCE_PER_DAY', 0.02)))


def system_user():
    user, created = User.objects.get_or_create(username=SYSTEM_USER, defaults={'is_active': True})
    if created:
        user.set_unusable_password()
        user.save(update_fields=['password'])
    group, _ = Group.objects.get_or_create(name='Simulator')
    if group.permissions.count() != len(PERMS):
        group.permissions.set([Permission.objects.get(content_type__app_label=p.split('.')[0],
                                                      codename=p.split('.')[1]) for p in PERMS])
    user.groups.add(group)
    return User.objects.get(pk=user.pk)  # fresh permission cache


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc)


def plan_start(plan):
    return _aware(plan.reference)


def factory_now(dataset_id, factory_id):
    """The simulated factory time for this factory, or None when no clock exists."""
    clock = FactoryClock.objects.filter(dataset_id=dataset_id, factory_id=factory_id).first()
    return clock.now if clock else None


def _done(clock, plan):
    """Pieces recorded per operation key of this plan, across every plan run of the factory (human or simulated)."""
    from floor.models import OutputEntry
    rows = (OutputEntry.objects.filter(run__dataset_id=clock.dataset_id, run__factory_id=clock.factory_id,
                                       operation_key__in=list(plan.ops))
            .values('operation_key').annotate(n=Sum('pieces')))
    done = {r['operation_key']: int(r['n'] or 0) for r in rows}
    from floor.completion import finished_keys
    for key in finished_keys(clock.dataset_id, clock.factory_id, plan):  # closed short or past sewing: no more work
        done[key] = int(plan.ops[key].get('pieces') or 0)
    return done


def _open_issue(clock, **match):
    from floor.models import Issue
    return (Issue.objects.filter(dataset_id=clock.dataset_id, factory_id=clock.factory_id,
                                 reported_by__username=SYSTEM_USER, **match).exclude(status='RESOLVED').first())


def apply(clock, run, plan, events):
    from floor import services as floor_services
    from floor.models import DefectCheck, OutputEntry
    user = system_user()
    for e in events:
        try:
            if e['type'] == 'OUTPUT':
                existing = OutputEntry.objects.filter(run=run, operation_key=e['key'], slot=e['slot']).first()
                if existing is not None and existing.entered_by_id != user.pk:
                    continue  # a supervisor entered this hour: theirs stands
                OutputEntry.objects.update_or_create(run=run, operation_key=e['key'], slot=e['slot'],
                                                     defaults={'line_id': e['line'], 'pieces': e['pieces'],
                                                               'entered_by': user})
            elif e['type'] == 'CHECK':
                defects = e['defects']
                kind = random.Random(f'{e["key"]}:{e["day"]}').choice(DefectCheck.TYPES[:-1]) if defects else ''
                DefectCheck.objects.update_or_create(
                    run=run, day=e['day'], operation_key=e['key'], entered_by=user,
                    defaults={'line_id': e['line'] or 0, 'checked': e['checked'], 'defects': defects,
                              'defect_type': kind, 'note': 'Simulated check, defects repaired on the line',
                              'reworked_at': timezone.now() if defects else None,
                              'reworked_by': user if defects else None})
            elif e['type'] == 'BREAKDOWN':
                with transaction.atomic():
                    floor_services.report_issue(user, dataset_id=clock.dataset_id, factory_id=clock.factory_id,
                                                kind='MACHINE_DOWN', machine_id=e['machine'],
                                                note='Simulated breakdown')
            elif e['type'] == 'ABSENT':
                with transaction.atomic():
                    floor_services.report_issue(user, dataset_id=clock.dataset_id, factory_id=clock.factory_id,
                                                kind='WORKER_ABSENT', worker_id=e['worker'],
                                                note='Simulated absence')
            elif e['type'] in ('REPAIRED', 'BACK'):
                issue = (_open_issue(clock, kind='MACHINE_DOWN', machine_id=e['machine']) if e['type'] == 'REPAIRED'
                         else _open_issue(clock, kind='WORKER_ABSENT', worker_id=e['worker']))
                if issue is not None:
                    floor_services.move_issue(user, issue.pk, 'RESOLVED', 'Repaired (simulated)'
                                              if e['type'] == 'REPAIRED' else 'Back at work (simulated)')
        except (ValidationError, PermissionDenied) as exc:  # e.g. a person already reported that machine
            log.info('Simulator skipped %s: %s', e['type'], exc)
        except Exception:  # the factory database is not reachable: skip this event, keep the clock going
            log.exception('Simulator could not apply %s', e['type'])


def _finish(clock, plan, now):
    """Steps that filled up and orders whose every step is done (finished at the end of these hours)."""
    from floor import completion
    try:
        with transaction.atomic():
            completion.refresh(clock.dataset_id, clock.factory_id, plan, system_user(), now=now)
    except Exception:  # never stop the clock over this; the next tick tries again
        log.exception('Simulator could not record finished steps')


def _run_hours(clock, hours):
    from floor.plan import approved_plan, load
    run = approved_plan(clock.dataset_id, clock.factory_id)
    engine = State.from_json(clock.state.get('engine'))
    if run is not None:
        plan = load(run)
        t0 = int((clock.now - plan_start(plan)).total_seconds()) // HOUR * HOUR
        if t0 >= 0:
            engine.done = _done(clock, plan)
            rng = random.Random(f'{clock.seed}:{clock.dataset_id}:{clock.factory_id}:{t0}')
            events = advance(plan, engine, t0, t0 + hours * HOUR, rng, _rates())
            apply(clock, run, plan, events)
            _finish(clock, plan, clock.now + timedelta(hours=hours))
    clock.now = clock.now + timedelta(hours=hours)
    clock.state = {**clock.state, 'engine': engine.to_json()}


def tick(real_now=None):
    """Advance every running clock by its speed since the last tick (whole factory hours, at most 24 a tick).
    Returns the factory hours simulated."""
    real_now = real_now or timezone.now()
    return sum(process(pk, real_now) for pk in FactoryClock.objects.values_list('pk', flat=True))


def process(pk, real_now):
    """One clock, all or nothing: re-read under a lock (a reset meanwhile means nothing is written), simulate,
    save. A crash mid-way rolls the records back with the clock, so nothing is replayed twice."""
    with transaction.atomic():
        clock = FactoryClock.objects.select_for_update().select_related('dataset').filter(pk=pk).first()
        if clock is None:
            return 0
        if clock.last_tick is None or clock.speed == 0:
            FactoryClock.objects.filter(pk=pk).update(last_tick=real_now)
            return 0
        due = (real_now - clock.last_tick).total_seconds() * clock.speed * 60 + float(clock.state.get('carry', 0))
        hours = int(due // HOUR)
        carry = 0.0 if hours >= MAX_HOURS_PER_TICK else due - hours * HOUR
        hours = min(hours, MAX_HOURS_PER_TICK)
        if hours:
            _run_hours(clock, hours)
        clock.state = {**clock.state, 'carry': carry}
        clock.last_tick = real_now
        clock.save(update_fields=['now', 'state', 'last_tick', 'updated_at'])
        return hours
