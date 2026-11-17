from collections import defaultdict
from datetime import date, datetime
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Sum
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response
from . import lookup, services
from .models import DefectCheck, Issue, OutputEntry, Profile
from .roles import LINE_REQUIRED, ROLES, role_of

ON_TRACK = 0.85  # a line below 85% of its planned output so far is behind


def _fail(exc):
    if isinstance(exc, PermissionDenied):
        return Response({'error': str(exc) or 'Permission required'}, status=403)
    if isinstance(exc, lookup.ErpUnavailable):
        return Response({'error': str(exc)}, status=503)
    return Response({'error': ' '.join(exc.messages)}, status=409)


ERRORS = (ValidationError, PermissionDenied, lookup.ErpUnavailable)


def _viewer(request, factory_id, line_id=None):
    services.require(request.user, 'floor.view_floor')
    services.check_scope(request.user, factory_id, line_id)


def _issue(i):
    return {'id': i.pk, 'kind': i.kind, 'kind_label': i.get_kind_display(), 'status': i.status,
            'status_label': i.get_status_display(), 'line_id': i.line_id, 'machine_id': i.machine_id,
            'worker_id': i.worker_id, 'order_id': i.order_id, 'note': i.note,
            'material_id': i.material_id, 'arrival_date': i.arrival_date,
            'reported_by': i.reported_by.get_username(), 'handled_by': i.handled_by.get_username() if i.handled_by else None,
            'resolution': i.resolution, 'created_at': i.created_at, 'resolved_at': i.resolved_at,
            'replan_id': str(i.replan_id) if i.replan_id else None}


def _now(plan):
    """The factory's time: the simulated factory clock when one runs for this factory, else the computer's clock."""
    from django.apps import apps
    if apps.is_installed('sim'):
        from sim.runner import factory_now
        found = factory_now(plan.run.dataset_id, plan.run.factory_id)
        if found is not None:
            return found.replace(tzinfo=None)
    return datetime.now()


def _now_hour(plan, day):
    """Hours of a plan day already passed: all for past days, none for future days."""
    now = _now(plan)
    today = plan.day_of(now.date())
    return 24 if day < today else 0 if day > today else now.hour + 1


def _day(request, plan, days):
    """?day=N, else today when the plan covers it, else its next working day."""
    raw = request.query_params.get('day')
    if raw is None:
        return _default_day(plan, days)
    if not raw.isdigit():
        raise ValidationError('day must be a plan day number')
    return int(raw)


def _today(plan):
    """The factory's date (see _now)."""
    return _now(plan).date()


def _default_day(plan, days):
    today = plan.day_of(_today(plan))
    if today in days:
        return today
    later = [d for d in days if d >= today]
    return later[0] if later else (days[-1] if days else 0)


def _line_day(run, plan, line, day):
    """Hourly target and actual pieces of a line on a plan day, and its quality."""
    targets = plan.hourly_targets(line, day)
    entries = OutputEntry.objects.filter(run=run, line_id=line, slot__gte=day * 24, slot__lt=(day + 1) * 24)
    actual = defaultdict(dict)
    for e in entries:
        actual[e.operation_key][e.slot - day * 24] = e.pieces
    hours = []
    for h in range(24):
        t = sum(v.get(h, 0) for v in targets.values())
        a = sum(v.get(h, 0) for v in actual.values())
        if t > 0 or a > 0:
            hours.append({'hour': h, 'target': round(t, 1), 'actual': a})
    passed = _now_hour(plan, day)
    target_so_far = sum(x['target'] for x in hours if x['hour'] < passed)
    actual_so_far = sum(x['actual'] for x in hours if x['hour'] < passed)
    entered = bool(entries)
    checks = DefectCheck.objects.filter(run=run, line_id=line, day=day).aggregate(c=Sum('checked'), d=Sum('defects'))
    if target_so_far <= 0:
        state = 'NOT_STARTED' if passed < 24 and any(x['hour'] >= passed for x in hours) else 'IDLE'
    elif not entered:
        state = 'NO_DATA'
    else:
        state = 'ON_TRACK' if actual_so_far >= ON_TRACK * target_so_far else 'BEHIND'
    return {'hours': hours, 'actual_by_op': {k: v for k, v in actual.items()},
            'target_day': round(sum(x['target'] for x in hours)), 'target_so_far': round(target_so_far),
            'actual_so_far': actual_so_far, 'actual_day': sum(x['actual'] for x in hours), 'hours_passed': passed,
            'efficiency_pct': round(100 * actual_so_far / target_so_far, 1) if target_so_far > 0 and entered else None,
            'state': state, 'checked': checks['c'] or 0, 'defects': checks['d'] or 0,
            'dhu': round(100 * checks['d'] / checks['c'], 2) if checks['c'] else None}


def _plan_info(run, plan):
    return {'run_id': str(run.pk), 'approved_by': run.decided_by.get_username() if run.decided_by else None,
            'approved_at': run.decided_at, 'purpose': run.purpose, 'reference_time': plan.reference.isoformat(),
            'lines': plan.lines()}


@api_view(['GET'])
def lines(request, dataset_id, factory_id):
    try:
        _viewer(request, factory_id)
        dataset = services._dataset(dataset_id, factory_id)
        known = lookup.lines(dataset, factory_id)
    except ERRORS as exc:
        return _fail(exc)
    run = services.approved_plan(dataset_id, factory_id)
    return Response({'lines': known, 'plan': _plan_info(run, services.load(run)) if run else None,
                     'scope_line': services.scoped_line(request.user)})


@api_view(['GET'])
def line_day(request, dataset_id, factory_id, line_id):
    try:
        _viewer(request, factory_id, line_id)
        dataset = services._dataset(dataset_id, factory_id)
        run, plan = services.floor_plan(dataset_id, factory_id)
    except ERRORS as exc:
        return _fail(exc)
    days = plan.work_days(line_id)
    try:
        day = _day(request, plan, days)
    except ValidationError as exc:
        return _fail(exc)
    ops = plan.day_ops(line_id, day)
    people, models = lookup.names(dataset, [w for o in ops for w in o['workers']], [m for o in ops for m in o['machines']])
    targets = plan.hourly_targets(line_id, day)
    done = dict(OutputEntry.objects.filter(run=run, operation_key__in=[o['key'] for o in ops])
                .values_list('operation_key').annotate(n=Sum('pieces')))
    for op in ops:
        op['worker_names'] = [people.get(str(w), f'Worker {w}') for w in op['workers']]
        op['machine_models'] = [models.get(str(m), '') for m in op['machines']]
        op['hourly_target'] = {h: round(v, 1) for h, v in targets.get(op['key'], {}).items()}
        op['recorded_total'] = done.get(op['key'], 0)
    issues = Issue.objects.filter(dataset_id=dataset_id, factory_id=factory_id, line_id=line_id).exclude(status='RESOLVED')
    return Response({'plan': _plan_info(run, plan), 'line_id': line_id, 'day': day, 'date': plan.date_of(day).isoformat(),
                     'days': days, 'today': plan.day_of(date.today()), 'operations': ops,
                     'summary': _line_day(run, plan, line_id, day),
                     'issues': [_issue(i) for i in issues.select_related('reported_by', 'handled_by')],
                     'can_enter': request.user.has_perm('floor.enter_output'),
                     'can_check': request.user.has_perm('floor.record_defect'),
                     'defect_types': DefectCheck.TYPES})


@api_view(['GET'])
def board(request, dataset_id, factory_id):
    """Every line of the approved plan on one day (dashboard, line board and alerts)."""
    try:
        _viewer(request, factory_id)
        dataset = services._dataset(dataset_id, factory_id)
    except ERRORS as exc:
        return _fail(exc)
    run = services.approved_plan(dataset_id, factory_id)
    open_issues = Issue.objects.filter(dataset_id=dataset_id, factory_id=factory_id).exclude(status='RESOLVED')
    counts = defaultdict(int)
    for i in open_issues:
        counts[i.line_id] += 1
    if run is None:
        return Response({'plan': None, 'lines': [], 'open_issues': open_issues.count()})
    plan = services.load(run)
    days = sorted({d for line in plan.lines() for d in plan.work_days(line)})
    try:
        day = _day(request, plan, days)
    except ValidationError as exc:
        return _fail(exc)
    scope = services.scoped_line(request.user)
    try:
        numbers = {int(r['line_id']): r['line_number'] for r in lookup.lines(dataset, factory_id)}
    except lookup.ErpUnavailable:
        numbers = {}
    rows = [{'line_id': line, 'line_number': numbers.get(line), 'open_issues': counts.get(line, 0),
             'operations': len(plan.day_ops(line, day)), **_line_day(run, plan, line, day)}
            for line in plan.lines() if scope is None or line == scope]
    for r in rows:
        r.pop('actual_by_op', None)
    return Response({'plan': _plan_info(run, plan), 'day': day, 'date': plan.date_of(day).isoformat(), 'days': days,
                     'today': plan.day_of(date.today()), 'lines': rows, 'open_issues': open_issues.count()})


class OutputIn(serializers.Serializer):
    line_id = serializers.IntegerField()
    day = serializers.IntegerField(min_value=0)
    hour = serializers.IntegerField(min_value=0, max_value=23)
    operation_key = serializers.CharField(max_length=40)
    pieces = serializers.IntegerField(min_value=0)


@api_view(['POST'])
def output(request, dataset_id, factory_id):
    data = OutputIn(data=request.data)
    data.is_valid(raise_exception=True)
    try:
        entry = services.save_output(request.user, dataset_id=dataset_id, factory_id=factory_id, **data.validated_data)
    except ERRORS as exc:
        return _fail(exc)
    return Response({'operation_key': entry.operation_key, 'slot': entry.slot, 'pieces': entry.pieces})


class CheckIn(serializers.Serializer):
    line_id = serializers.IntegerField()
    day = serializers.IntegerField(min_value=0)
    operation_key = serializers.CharField(max_length=40)
    checked = serializers.IntegerField(min_value=1)
    defects = serializers.IntegerField(min_value=0)
    defect_type = serializers.CharField(max_length=40, required=False, allow_blank=True, default='')
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default='')


@api_view(['GET', 'POST'])
def checks(request, dataset_id, factory_id):
    if request.method == 'POST':
        data = CheckIn(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            check = services.record_check(request.user, dataset_id=dataset_id, factory_id=factory_id,
                                          **data.validated_data)
        except ERRORS as exc:
            return _fail(exc)
        return Response({'id': check.pk}, status=201)
    try:
        _viewer(request, factory_id)
    except ERRORS as exc:
        return _fail(exc)
    run = services.approved_plan(dataset_id, factory_id)
    if run is None:
        return Response({'plan': None, 'checks': [], 'by_line': [], 'by_type': []})
    plan = services.load(run)
    query = DefectCheck.objects.filter(run=run).select_related('entered_by').order_by('-created_at')
    scope = services.scoped_line(request.user)
    if scope:
        query = query.filter(line_id=scope)
    items = list(query[:300])
    by_line, by_type = defaultdict(lambda: [0, 0]), defaultdict(int)
    for c in query:
        by_line[c.line_id][0] += c.checked
        by_line[c.line_id][1] += c.defects
        if c.defects:
            by_type[c.defect_type] += c.defects
    return Response({
        'plan': _plan_info(run, plan),
        'checks': [{'id': c.pk, 'line_id': c.line_id, 'day': c.day, 'date': plan.date_of(c.day).isoformat(),
                    'order_id': plan.ops.get(c.operation_key, {}).get('order_id'),
                    'step': plan.ops.get(c.operation_key, {}).get('step'),
                    'operation': plan.ops.get(c.operation_key, {}).get('name'), 'checked': c.checked,
                    'defects': c.defects, 'dhu': round(100 * c.defects / c.checked, 2), 'defect_type': c.defect_type,
                    'note': c.note, 'entered_by': c.entered_by.get_username(), 'created_at': c.created_at}
                   for c in items],
        'by_line': [{'line_id': k, 'checked': v[0], 'defects': v[1], 'dhu': round(100 * v[1] / v[0], 2)}
                    for k, v in sorted(by_line.items())],
        'by_type': [{'defect_type': k, 'defects': v} for k, v in sorted(by_type.items(), key=lambda x: -x[1])]})


class IssueIn(serializers.Serializer):
    kind = serializers.ChoiceField(choices=[k for k, _ in Issue.KINDS])
    machine_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    worker_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    order_id = serializers.CharField(max_length=20, required=False, allow_blank=True, default='')
    line_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True, default='')
    material_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    arrival_date = serializers.DateField(required=False, allow_null=True, default=None)


@api_view(['GET', 'POST'])
def issues(request, dataset_id, factory_id):
    if request.method == 'POST':
        data = IssueIn(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            issue = services.report_issue(request.user, dataset_id=dataset_id, factory_id=factory_id,
                                          **data.validated_data)
        except ERRORS as exc:
            return _fail(exc)
        body = _issue(issue)
        if getattr(issue, 'affected_orders', None) == []:
            body['warning'] = 'No order in the current plan uses this material'
        return Response(body, status=201)
    try:
        _viewer(request, factory_id)
    except ERRORS as exc:
        return _fail(exc)
    query = Issue.objects.filter(dataset_id=dataset_id, factory_id=factory_id).select_related('reported_by', 'handled_by')
    scope = services.scoped_line(request.user)
    if scope:
        query = query.filter(line_id=scope)
    if request.query_params.get('open') == '1':
        query = query.exclude(status='RESOLVED')
    return Response({'issues': [_issue(i) for i in query.order_by('-created_at')[:300]],
                     'can_report': request.user.has_perm('floor.report_issue'),
                     'can_handle': request.user.has_perm('floor.handle_issue'),
                     'can_repair': request.user.has_perm('floor.repair_machine'),
                     'can_replan': request.user.has_perm('planning.request_plan'),
                     'has_plan': services.approved_plan(dataset_id, factory_id) is not None})


class MoveIn(serializers.Serializer):
    status = serializers.ChoiceField(choices=['IN_PROGRESS', 'RESOLVED'])
    resolution = serializers.CharField(max_length=2000, required=False, allow_blank=True, default='')


@api_view(['POST'])
def move(request, issue_id):
    data = MoveIn(data=request.data)
    data.is_valid(raise_exception=True)
    try:
        issue = services.move_issue(request.user, issue_id, **data.validated_data)
    except ERRORS as exc:
        return _fail(exc)
    return Response(_issue(issue))


class ReplanIn(serializers.Serializer):
    issue_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1, max_length=50)
    mode = serializers.ChoiceField(choices=['FULL', 'QUICK'], default='QUICK')


@api_view(['POST'])
def replan(request, dataset_id, factory_id):
    data = ReplanIn(data=request.data)
    data.is_valid(raise_exception=True)
    try:
        run = services.replan_after(request.user, dataset_id=dataset_id, factory_id=factory_id, **data.validated_data)
    except ERRORS as exc:
        return _fail(exc)
    return Response({'run_id': str(run.pk)}, status=201)


# ---------- users (administrator only) ----------

def _user(u):
    profile = Profile.objects.filter(user=u).first()
    role = role_of(u)
    return {'id': u.pk, 'username': u.username, 'role': role, 'role_label': ROLES[role][0] if role else None,
            'factory_id': profile.factory_id if profile else None, 'line_id': profile.line_id if profile else None,
            'active': u.is_active, 'last_login': u.last_login, 'date_joined': u.date_joined, 'email': u.email}


class UserIn(serializers.Serializer):
    role = serializers.ChoiceField(choices=list(ROLES))
    factory_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    line_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    dataset_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    email = serializers.EmailField(required=False, allow_blank=True, default='')  # for CRAFT's plan emails


class NewUserIn(UserIn):
    username = serializers.CharField(max_length=40)


class ChangeUserIn(UserIn):
    active = serializers.BooleanField(default=True)
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True, default=None)  # None = keep


@api_view(['GET', 'POST'])
def users(request):
    try:
        services.require(request.user, 'floor.manage_users')
    except ERRORS as exc:
        return _fail(exc)
    if request.method == 'POST':
        data = NewUserIn(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            user, password = services.create_user(request.user, **data.validated_data)
        except ERRORS as exc:
            return _fail(exc)
        return Response({**_user(user), 'password': password}, status=201)
    return Response({'users': [_user(u) for u in User.objects.order_by('username')],
                     'roles': [{'key': k, 'label': v[0], 'line_required': k in LINE_REQUIRED} for k, v in ROLES.items()]})


@api_view(['POST'])
def change_user(request, user_id):
    data = ChangeUserIn(data=request.data)
    data.is_valid(raise_exception=True)
    try:
        user = services.update_user(request.user, user_id, **data.validated_data)
    except ERRORS as exc:
        return _fail(exc)
    return Response(_user(user))


@api_view(['POST'])
def new_password(request, user_id):
    try:
        user, password = services.reset_password(request.user, user_id)
    except ERRORS as exc:
        return _fail(exc)
    return Response({'username': user.username, 'password': password})

