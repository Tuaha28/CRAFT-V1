from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_protect
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from .models import AuditEvent, CraftRun, ScheduleScope, ScheduleVersion
from .serializers import (AuditEventSerializer, CandidateSerializer, DecisionSerializer,
                          CaptureSelectionSerializer, PriorityRequestSerializer, RunRequestSerializer,
                          RunSerializer, ScopeSerializer)
from . import services


def allowed_scopes(user):
    if user.is_superuser:
        return ScheduleScope.objects.all()
    return ScheduleScope.objects.filter(factoryaccess__user=user)


@api_view(['GET'])
@permission_classes([AllowAny])
def session(request):
    return Response({'csrf_token': get_token(request), 'authenticated': request.user.is_authenticated,
                     'username': request.user.get_username() if request.user.is_authenticated else None,
                     'can_request': request.user.has_perm('governance.request_run'),
                     'can_promote': request.user.has_perm('governance.promote_schedule'),
                     'can_change_priority': request.user.has_perm('governance.change_priority'),
                     'environment': 'ISOLATED_TEST', 'live_admission': False})


@csrf_protect
def sign_in(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'METHOD_NOT_ALLOWED'}, status=405)
    import json
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeError):
        return JsonResponse({'error': 'INVALID_JSON'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'error': 'INVALID_JSON'}, status=400)
    user = authenticate(request, username=data.get('username'), password=data.get('password'))
    if user is None:
        return JsonResponse({'error': 'INVALID_CREDENTIALS'}, status=401)
    login(request, user)
    return JsonResponse({'authenticated': True, 'csrf_token': get_token(request)})


class Scopes(generics.ListAPIView):
    serializer_class = ScopeSerializer
    def get_queryset(self):
        return allowed_scopes(self.request.user).order_by('id')


@api_view(['POST'])
def select_capture_work(request, scope_id):
    if not ScheduleScope.objects.filter(pk=scope_id, pk__in=allowed_scopes(request.user).values('pk')).exists():
        return Response({'error': 'SCOPE_NOT_FOUND'}, status=404)
    data = CaptureSelectionSerializer(data=request.data)
    data.is_valid(raise_exception=True)
    try:
        scope = services.select_test_capture_work(request.user, scope_id,
            data.validated_data['operation_keys'], window_size=data.validated_data.get('window_size'))
    except ValidationError as exc:
        return Response({'error': exc.messages}, status=409)
    return Response(ScopeSerializer(scope).data)


@api_view(['POST'])
def change_priority(request, scope_id):
    if not ScheduleScope.objects.filter(pk=scope_id, pk__in=allowed_scopes(request.user).values('pk')).exists():
        return Response({'error': 'SCOPE_NOT_FOUND'}, status=404)
    data = PriorityRequestSerializer(data=request.data)
    data.is_valid(raise_exception=True)
    try:
        scope = services.record_priority(request.user, scope_id, **data.validated_data)
    except ValidationError as exc:
        return Response({'error': exc.messages}, status=409)
    return Response(ScopeSerializer(scope).data)


class Events(generics.ListAPIView):
    serializer_class = AuditEventSerializer

    def get_queryset(self):
        return AuditEvent.objects.filter(scope__in=allowed_scopes(self.request.user)).select_related('actor').order_by('-created_at')


class Runs(generics.ListCreateAPIView):
    serializer_class = RunSerializer
    def get_queryset(self):
        return CraftRun.objects.filter(scope__in=allowed_scopes(self.request.user)).order_by('-created_at')
    def create(self, request, *args, **kwargs):
        data = RunRequestSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            run, created = services.request_run(request.user, data.validated_data['scope'],
                data.validated_data['idempotency_key'], data.validated_data['snapshot'])
        except ScheduleScope.DoesNotExist:
            return Response({'error': 'SCOPE_NOT_FOUND'}, status=404)
        except ValidationError as exc:
            return Response({'error': exc.messages}, status=409)
        return Response(RunSerializer(run).data, status=202 if created else 200)


class Candidates(generics.ListAPIView):
    serializer_class = CandidateSerializer
    def get_queryset(self):
        return ScheduleVersion.objects.filter(scope__in=allowed_scopes(self.request.user)).order_by('-created_at')


@api_view(['POST'])
def promote(request, candidate_id):
    if not ScheduleVersion.objects.filter(pk=candidate_id, scope__in=allowed_scopes(request.user)).exists():
        return Response({'error': 'NOT_FOUND'}, status=404)
    data = DecisionSerializer(data=request.data)
    data.is_valid(raise_exception=True)
    try:
        value = services.promote(request.user, candidate_id, data.validated_data['reason'])
    except ValidationError as exc:
        return Response({'error': exc.messages}, status=409)
    return Response(CandidateSerializer(value).data)


@api_view(['POST'])
def cancel(request, run_id):
    if not CraftRun.objects.filter(pk=run_id, scope__in=allowed_scopes(request.user)).exists():
        return Response({'error': 'NOT_FOUND'}, status=404)
    data = DecisionSerializer(data=request.data)
    data.is_valid(raise_exception=True)
    try:
        changed = services.cancel_run(request.user, run_id, data.validated_data['reason'])
    except ValidationError as exc:
        return Response({'error': exc.messages}, status=409)
    return Response({'cancelled': changed, 'active_schedule_preserved': True})


@api_view(['POST'])
def reject(request, candidate_id):
    if not ScheduleVersion.objects.filter(pk=candidate_id, scope__in=allowed_scopes(request.user)).exists():
        return Response({'error': 'NOT_FOUND'}, status=404)
    data = DecisionSerializer(data=request.data)
    data.is_valid(raise_exception=True)
    try:
        candidate = services.reject(request.user, candidate_id, data.validated_data['reason'])
    except ValidationError as exc:
        return Response({'error': exc.messages}, status=409)
    return Response(CandidateSerializer(candidate).data)


@api_view(['GET'])
def compare(request, candidate_id):
    candidate = ScheduleVersion.objects.filter(pk=candidate_id, scope__in=allowed_scopes(request.user)).select_related('scope__active_version').first()
    if candidate is None:
        return Response({'error': 'NOT_FOUND'}, status=404)
    active = candidate.scope.active_version
    before = {row['operation_key']: row for row in active.operations} if active else {}
    after = {row['operation_key']: row for row in candidate.operations}
    changes = [{'operation_key': key, 'before': before.get(key), 'after': after.get(key),
                'changed': before.get(key) != after.get(key)} for key in sorted(before.keys() | after.keys())]
    evidence = candidate.evidence or {}
    acceptance = evidence.get('acceptance', {})
    scope_validation = acceptance.get('validator', {}).get('scope_acceptance', {})
    validation = {
        'materialization_mismatch_count': acceptance.get('materialization', {}).get('mismatch_count'),
        'replay_status': acceptance.get('replay', {}).get('status', 'NOT_RUN'),
        'physical_status': scope_validation.get('physical_status', 'NOT_RUN'),
        'scope_status': scope_validation.get('scope_status', 'NOT_RUN'),
        'quantity_status': evidence.get('quantity', {}).get('status', 'NOT_RUN'),
        'candidate_validated': services.evidence_valid(evidence),
    }
    priority_impact = None
    if evidence.get('source') == 'SYNTHETIC_PRIORITY_FIXTURE':
        # Reconstruct comparison metrics from persisted assignments, independently
        # of the fixture's reported order-completion summary.
        finish_keys = {'A': 'A_finish', 'B': 'B_residual'}
        deadlines = {'A': 1050, 'B': 3000}
        def metrics(rows):
            by_key = {row['operation_key']: row for row in rows}
            if not all(key in by_key for key in finish_keys.values()):
                return None
            completion = {order: by_key[key]['end_seconds'] for order, key in finish_keys.items()}
            return {'completion_seconds': completion,
                    'tardiness_seconds': {order: max(0, completion[order] - deadlines[order])
                                          for order in finish_keys},
                    'makespan_seconds': max(row['end_seconds'] for row in rows)}
        priority_impact = {'active': metrics(active.operations) if active else None,
                           'candidate': metrics(candidate.operations),
                           'customer_deadline_seconds': deadlines,
                           'manager_override': evidence.get('manager_overrides', [])}
    return Response({'environment': candidate.scope.environment, 'active_version': str(active.pk) if active else None,
                     'candidate_version': str(candidate.pk), 'changes': changes,
                     'changed_operation_count': sum(row['changed'] for row in changes),
                     'validation': validation,
                     'reference_time': evidence.get('ledger', {}).get('source_evidence', {}).get('as_of'),
                     'priority_impact': priority_impact})
