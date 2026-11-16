import json
from pathlib import Path
from django.apps import apps
from django.conf import settings
from django.contrib.auth import logout
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse
from django.middleware.csrf import get_token
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from . import jobs, services
from .datasets import discover, factory_of
from .models import Dataset, PlanEvent, PlanRun


def _dataset(d):
    return {'id': d.pk, 'slug': d.slug, 'label': d.label, 'origin': d.origin, 'database': d.database,
            'load_pct': d.load_pct, 'reference_time': d.reference_time, 'factories': d.factories,
            'captured_at': d.captured_at}


def _run(r, worker_alive=True):
    return {'id': str(r.pk), 'worker_down': r.status == 'QUEUED' and not worker_alive, 'kind': r.kind, 'status': r.status, 'stage': r.stage, 'progress': r.progress,
            'mode': r.mode, 'dataset': _dataset(r.dataset) if r.dataset else None, 'factory_id': r.factory_id,
            'orders': r.orders, 'order_offset': r.order_offset,
            'requested_by': r.requested_by.get_username(),
            'created_at': r.created_at, 'started_at': r.started_at, 'finished_at': r.finished_at,
            'heartbeat_at': r.heartbeat_at, 'error': r.error, 'summary': r.summary,
            'decision': r.decision, 'decided_by': r.decided_by.get_username() if r.decided_by else None,
            'decided_at': r.decided_at, 'decision_reason': r.decision_reason,
            'purpose': r.purpose, 'parent_id': str(r.parent_id) if r.parent_id else None, 'trigger': r.trigger,
            'scenario': {**{k: v for k, v in r.scenario.items() if k != 'new_order'},
                         **({'new_order': {k: v for k, v in r.scenario['new_order'].items() if k != 'routing'}}
                            if r.scenario.get('new_order') else {})}}


def _fail(exc):
    if isinstance(exc, PermissionDenied):
        return Response({'error': str(exc) or 'Permission required'}, status=403)
    return Response({'error': ' '.join(exc.messages)}, status=409)


def _writeback_on():
    """Approved plans are written into the demo copies of the factory databases."""
    from erp import writer
    return writer.enabled()


@api_view(['GET'])
@permission_classes([AllowAny])
def session(request):
    user = request.user
    return Response({'csrf_token': get_token(request), 'authenticated': user.is_authenticated,
                     'username': user.get_username() if user.is_authenticated else None,
                     'can_request': user.has_perm('planning.request_plan'),
                     'can_approve': user.has_perm('planning.approve_plan'),
                     'production_activation': bool(getattr(settings, 'CRAFT_PRODUCTION_ACTIVATION', False)),
                     'writeback': _writeback_on(),
                     **(_floor_session(user) if apps.is_installed('floor') else {})})


def _floor_session(user):
    from floor.roles import describe
    return describe(user)


@api_view(['POST'])
def sign_out(request):
    logout(request)
    return Response({'authenticated': False})


@api_view(['GET'])
def datasets(request):
    discover()
    return Response([_dataset(d) for d in Dataset.objects.filter(active=True).order_by('load_pct', 'slug')])


@api_view(['GET'])
def orderbook(request, dataset_id, factory_id):
    dataset = Dataset.objects.filter(pk=dataset_id).first()
    if dataset is None or factory_of(dataset, factory_id) is None:
        return Response({'error': 'Dataset or factory not found'}, status=404)
    try:
        return Response(jobs.orderbook(dataset, factory_id))
    except RuntimeError as exc:
        return Response({'error': str(exc)[:600]}, status=502)


class ScheduleRequest(serializers.Serializer):
    dataset_id = serializers.IntegerField()
    factory_id = serializers.IntegerField(min_value=1)
    orders = serializers.IntegerField(min_value=1, max_value=services.MAX_ORDERS)
    order_offset = serializers.IntegerField(min_value=0)
    mode = serializers.ChoiceField(choices=['FULL', 'QUICK'])


class Reason(serializers.Serializer):
    reason = serializers.CharField(max_length=1000)


class Decision(Reason):
    decision = serializers.ChoiceField(choices=['APPROVED', 'REJECTED'])


@api_view(['GET', 'POST'])
def runs(request):
    if request.method == 'POST':
        data = ScheduleRequest(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            run = services.request_schedule(request.user, **data.validated_data)
        except (ValidationError, PermissionDenied) as exc:
            return _fail(exc)
        return Response(_run(run), status=201)
    services.expire_orphans()  # a run whose worker died must not look busy forever, even with no worker running
    alive = services.worker_alive()
    query = PlanRun.objects.select_related('dataset', 'requested_by', 'decided_by').order_by('-created_at')
    return Response([_run(r, alive) for r in query[:200]])


@api_view(['POST'])
def captures(request):
    try:
        run = services.request_capture(request.user)
    except (ValidationError, PermissionDenied) as exc:
        return _fail(exc)
    return Response(_run(run), status=201)


@api_view(['GET'])
def run_detail(request, run_id):
    services.expire_orphans()
    run = PlanRun.objects.select_related('dataset', 'requested_by', 'decided_by').filter(pk=run_id).first()
    if run is None:
        return Response({'error': 'Run not found'}, status=404)
    body = _run(run, services.worker_alive())
    body['log_tail'] = run.log_tail
    body['events'] = [{'action': e.action, 'message': e.message, 'actor': e.actor.get_username() if e.actor else None,
                       'created_at': e.created_at} for e in run.events.select_related('actor').order_by('created_at')]
    digest_path = Path(run.out_dir) / 'digest.json' if run.out_dir else None
    body['digest'] = (json.loads(digest_path.read_text(encoding='utf-8'))
                      if run.status == 'SUCCEEDED' and digest_path and digest_path.exists() else None)
    wrote = run.writebacks.filter(kind='PLAN').order_by('-created_at').first()
    body['writeback'] = ({'ok': wrote.ok, 'rows': wrote.rows, 'error': wrote.error[:300], 'at': wrote.created_at}
                         if wrote else None)
    return Response(body)


@api_view(['POST'])
def cancel(request, run_id):
    data = Reason(data=request.data)
    data.is_valid(raise_exception=True)
    try:
        changed = services.cancel(request.user, run_id, data.validated_data['reason'])
    except (ValidationError, PermissionDenied) as exc:
        return _fail(exc)
    return Response({'cancelled': changed})


@api_view(['POST'])
def decide(request, run_id):
    data = Decision(data=request.data)
    data.is_valid(raise_exception=True)
    try:
        run = services.decide(request.user, run_id, data.validated_data['decision'], data.validated_data['reason'])
    except PlanRun.DoesNotExist:
        return Response({'error': 'Run not found'}, status=404)
    except (ValidationError, PermissionDenied) as exc:
        return _fail(exc)
    return Response(_run(run))


@api_view(['GET'])
def workbook(request, run_id):
    run = PlanRun.objects.filter(pk=run_id, status='SUCCEEDED').first()
    path = Path(run.out_dir) / f'factory{run.factory_id}_craft_v3_plan.xlsx' if run and run.out_dir else None
    if path is None or not path.exists():
        return Response({'error': 'No workbook for this run'}, status=404)
    return FileResponse(path.open('rb'), as_attachment=True, filename=f'craft_plan_{str(run.pk)[:8]}.xlsx')


@api_view(['GET'])
def experiments(request):
    path = Path(settings.PLANNING_EXPERIMENTS_DIR) / 'paper_data.json'
    if not path.exists():
        return Response({'error': 'Experiment results not found'}, status=404)
    return Response(json.loads(path.read_text(encoding='utf-8')))


@api_view(['GET'])
def events(request):
    query = PlanEvent.objects.select_related('actor', 'run').order_by('-created_at')[:300]
    return Response([{'action': e.action, 'message': e.message, 'run_id': str(e.run_id) if e.run_id else None,
                      'actor': e.actor.get_username() if e.actor else None, 'created_at': e.created_at}
                     for e in query])


@api_view(['GET'])
def current_plan(request, dataset_id, factory_id):
    """The plan the dashboard shows: the latest approved plan, else the latest finished one."""
    finished = PlanRun.objects.filter(kind='SCHEDULE', status='SUCCEEDED', dataset_id=dataset_id, factory_id=factory_id)
    run = (finished.filter(decision='APPROVED').order_by('-decided_at').first()
           or finished.exclude(decision='REJECTED').order_by('-finished_at').first())
    if run is None:
        return Response({'run': None, 'digest': None})
    path = Path(run.out_dir) / 'digest.json'
    digest = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    return Response({'run': _run(run), 'digest': digest})


class DisruptionRequest(serializers.Serializer):
    machines_down = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    workers_absent = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    mode = serializers.ChoiceField(choices=['FULL', 'QUICK'], default='QUICK')


class PromiseRequest(serializers.Serializer):
    product_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1, max_value=20000)
    release = serializers.DateField()
    due = serializers.DateField()
    mode = serializers.ChoiceField(choices=['FULL', 'QUICK'], default='QUICK')


@api_view(['POST'])
def disruption(request, run_id):
    data = DisruptionRequest(data=request.data)
    data.is_valid(raise_exception=True)
    try:
        run = services.request_disruption(request.user, parent_id=run_id, **data.validated_data)
    except (ValidationError, PermissionDenied) as exc:
        return _fail(exc)
    return Response(_run(run), status=201)


@api_view(['POST'])
def promise(request, run_id):
    from erp import queries
    from erp.db import ErpUnavailable, live_database, read_only
    data = PromiseRequest(data=request.data)
    data.is_valid(raise_exception=True)
    parent = PlanRun.objects.filter(pk=run_id).select_related('dataset').first()
    if parent is None or parent.dataset is None:
        return Response({'error': 'Plan not found'}, status=404)
    try:
        with read_only(live_database(parent.dataset)) as cursor:
            style = queries.style_detail(cursor, data.validated_data['product_id'])
    except ErpUnavailable as exc:
        return Response({'error': str(exc)}, status=503)
    if style is None:
        return Response({'error': 'Style not found'}, status=404)
    routing = [{'operation_id': r['operation_id'], 'operation_name': r['operation_name'], 'sequence': r['step'],
                'machine_type': r['required_machine_type'], 'sam': r['sam']} for r in style['routing']]
    v = data.validated_data
    try:
        run = services.request_promise(request.user, parent_id=run_id, product_id=v['product_id'], quantity=v['quantity'],
                                       release=v['release'].isoformat(), due=v['due'].isoformat(),
                                       routing=routing, mode=v['mode'], style_code=style['product'].get('style_code', ''))
    except (ValidationError, PermissionDenied) as exc:
        return _fail(exc)
    return Response(_run(run), status=201)


class PriorityIn(serializers.Serializer):
    dataset = serializers.IntegerField()
    factory = serializers.IntegerField()
    order_id = serializers.CharField(max_length=20)
    on = serializers.BooleanField()
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')


@api_view(['GET', 'POST'])
def priority(request):
    """GET ?dataset=&factory= -> {'orders': [...]}; POST marks or unmarks one order (request_plan)."""
    if request.method == 'POST':
        data = PriorityIn(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        try:
            services.set_priority(request.user, dataset_id=v['dataset'], factory_id=v['factory'],
                                  order_id=v['order_id'], on=v['on'], reason=v['reason'])
        except (ValidationError, PermissionDenied) as exc:
            return _fail(exc)
        return Response({'orders': services.priority_orders(v['dataset'], v['factory'])})
    try:
        dataset_id, factory_id = int(request.query_params['dataset']), int(request.query_params['factory'])
    except (KeyError, ValueError):
        return Response({'error': 'dataset and factory are required'}, status=400)
    return Response({'orders': services.priority_orders(dataset_id, factory_id)})


@api_view(['GET', 'POST'])
def autoplan_setting(request):
    """GET/POST ?dataset=&factory= -> {'enabled': bool}. Switching needs request_plan."""
    from . import autoplan
    try:
        dataset_id, factory_id = int(request.query_params['dataset']), int(request.query_params['factory'])
    except (KeyError, ValueError):
        return Response({'error': 'dataset and factory are required'}, status=400)
    if request.method == 'POST':
        try:
            autoplan.set_enabled(request.user, dataset_id=dataset_id, factory_id=factory_id,
                                 on=bool(request.data.get('enabled')))
        except (ValidationError, PermissionDenied) as exc:
            return _fail(exc)
    from django.conf import settings as conf
    email = 'SMTP' if conf.EMAIL_BACKEND.endswith('smtp.EmailBackend') else 'OUTBOX'
    return Response({'enabled': autoplan.enabled(dataset_id, factory_id), 'email': email})
