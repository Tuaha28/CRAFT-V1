"""Factory clock API: everyone on the staff side sees it; production managers and admins control it."""
from datetime import datetime, timezone as dt_timezone
from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response
from planning.models import Dataset
from planning.services import event
from . import runner
from .models import FactoryClock


def _scope(request):
    try:
        return Dataset.objects.get(pk=int(request.query_params['dataset'])), int(request.query_params['factory'])
    except (KeyError, ValueError, Dataset.DoesNotExist):
        return None, None


def _can_control(user):
    return user.has_perm('planning.approve_plan') or user.has_perm('floor.manage_users')


def _body(clock, dataset, factory_id, user):
    from floor.plan import approved_plan
    has_plan = approved_plan(dataset.pk, factory_id) is not None
    if clock is None:
        return {'now': None, 'speed': 0, 'running': False, 'seed': None, 'has_plan': has_plan,
                'can_control': _can_control(user)}
    return {'now': clock.now, 'speed': clock.speed, 'running': clock.speed > 0, 'seed': clock.seed,
            'has_plan': has_plan, 'can_control': _can_control(user)}


def _start_time(dataset, factory_id):
    """The approved plan's start, else the data's reference time."""
    from floor.plan import approved_plan, load
    run = approved_plan(dataset.pk, factory_id)
    if run is not None:
        return runner.plan_start(load(run))
    start = datetime.fromisoformat(dataset.reference_time)
    return start if start.tzinfo else start.replace(tzinfo=dt_timezone.utc)


def _reset(dataset, factory_id):
    from floor.models import DefectCheck, Issue, OutputEntry
    who = {'entered_by__username': runner.SYSTEM_USER}
    OutputEntry.objects.filter(run__dataset=dataset, run__factory_id=factory_id, **who).delete()
    DefectCheck.objects.filter(run__dataset=dataset, run__factory_id=factory_id, **who).delete()
    Issue.objects.filter(dataset=dataset, factory_id=factory_id, reported_by__username=runner.SYSTEM_USER).delete()
    FactoryClock.objects.filter(dataset=dataset, factory_id=factory_id).delete()


def _find(dataset, factory_id):
    return FactoryClock.objects.filter(dataset=dataset, factory_id=factory_id).first()


@api_view(['GET', 'POST'])
def clock(request):
    dataset, factory_id = _scope(request)
    if dataset is None:
        return Response({'error': 'dataset and factory are required'}, status=400)
    found = _find(dataset, factory_id)
    if request.method == 'GET':
        return Response(_body(found, dataset, factory_id, request.user))
    if not _can_control(request.user):
        return Response({'error': 'Only a production manager or an administrator controls the factory clock'},
                        status=403)
    action, speed = request.data.get('action'), request.data.get('speed')
    if action in ('start', 'speed') and speed is not None and speed not in FactoryClock.SPEEDS[1:]:
        return Response({'error': 'Speed must be 1, 10 or 60'}, status=400)
    with transaction.atomic():
        if action == 'reset':
            _reset(dataset, factory_id)
            event(None, request.user, 'SIM_RESET', f'Simulated factory clock reset for factory {factory_id} '
                                                   '(simulated records removed)')
            return Response(_body(None, dataset, factory_id, request.user))
        if action == 'start':
            found = found or FactoryClock.objects.create(dataset=dataset, factory_id=factory_id,
                                                         now=_start_time(dataset, factory_id),
                                                         seed=int(request.data.get('seed') or 1))
            new_speed = speed or found.speed or 1
        elif action == 'speed' and found is not None and speed is not None:
            new_speed = speed
        elif action == 'pause' and found is not None:
            new_speed = 0
        else:
            return Response({'error': 'Start the clock first, then pause it or change its speed'}, status=400)
        # only these fields: a tick may have moved now/state since this request read the row
        FactoryClock.objects.filter(pk=found.pk).update(speed=new_speed, last_tick=timezone.now(),
                                                        set_by=request.user, updated_at=timezone.now())
        found = FactoryClock.objects.get(pk=found.pk)
        event(None, request.user, 'SIM_CLOCK', f'Simulated factory clock for factory {factory_id}: '
                                               f'{"paused" if found.speed == 0 else f"{found.speed}x"}')
    return Response(_body(found, dataset, factory_id, request.user))
