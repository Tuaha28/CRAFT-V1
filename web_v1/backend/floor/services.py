"""Shop-floor transactions. Every write checks the permission AND the user's factory/line scope here,
not only in the browser. Nothing is written to the factory database; every change is logged."""
import re
import secrets
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from planning import services as planning_services
from planning.models import Dataset
from . import lookup
from .models import DefectCheck, Issue, OutputEntry, Profile
from .plan import approved_plan, load
from .roles import LINE_REQUIRED, ROLES, ensure_groups

MAX_PIECES_PER_ENTRY = 100000


def require(user, perm):
    if not user.is_authenticated or not user.has_perm(perm):
        raise PermissionDenied('Permission required: ' + perm.split('.')[-1])


def profile_of(user):
    return Profile.objects.filter(user=user).first()


def check_scope(user, factory_id, line_id=None):
    """A user limited to a factory or line may act only there."""
    profile = profile_of(user)
    if profile is None:
        return
    if profile.factory_id and int(profile.factory_id) != int(factory_id):
        raise PermissionDenied(f'You work for factory {profile.factory_id}')
    if profile.line_id and line_id is not None and int(profile.line_id) != int(line_id):
        raise PermissionDenied(f'You work for line {profile.line_id}')


def scoped_line(user):
    profile = profile_of(user)
    return profile.line_id if profile else None


def floor_plan(dataset_id, factory_id):
    run = approved_plan(dataset_id, factory_id)
    if run is None:
        raise ValidationError('No plan has been approved for this factory yet. '
                              'The production manager approves a plan first.')
    return run, load(run)


def _dataset(dataset_id, factory_id):
    dataset = Dataset.objects.filter(pk=dataset_id).first()
    if dataset is None or not any(f['factory_id'] == int(factory_id) for f in dataset.factories):
        raise ValidationError('Factory data not found')
    return dataset


# ---------- issues ----------

@transaction.atomic
def report_issue(user, *, dataset_id, factory_id, kind, machine_id=None, worker_id=None, order_id='', line_id=None,
                 note='', material_id=None, arrival_date=None):
    require(user, 'floor.report_issue')
    dataset = _dataset(dataset_id, factory_id)
    if kind not in dict(Issue.KINDS):
        raise ValidationError('Unknown issue type')
    if line_id is None:
        line_id = scoped_line(user)
    subject = ''
    if kind == 'MACHINE_DOWN':
        if not machine_id:
            raise ValidationError('Choose the machine that is down')
        found = lookup.machine(dataset, machine_id)
        if not found or int(found['factory_id']) != int(factory_id):
            raise ValidationError(f'Machine {machine_id} is not in factory {factory_id}')
        line_id = found['line_id']
        if Issue.objects.filter(dataset=dataset, machine_id=machine_id).exclude(status='RESOLVED').exists():
            raise ValidationError(f'Machine {machine_id} is already reported down')
        subject = f'machine {machine_id} ({found["machine_model"]})'
    elif kind == 'WORKER_ABSENT':
        if not worker_id:
            raise ValidationError('Choose the worker who is absent')
        found = lookup.worker(dataset, worker_id)
        if not found or int(found['factory_id']) != int(factory_id):
            raise ValidationError(f'Worker {worker_id} is not in factory {factory_id}')
        line_id = found['line_id'] or line_id
        if Issue.objects.filter(dataset=dataset, worker_id=worker_id).exclude(status='RESOLVED').exists():
            raise ValidationError(f'Worker {worker_id} is already reported absent')
        subject = f'worker {worker_id} ({found["name"]})'
    elif kind == 'LINE_STOPPED':
        if not line_id:
            raise ValidationError('Choose the line that stopped')
        if not any(int(l['line_id']) == int(line_id) for l in lookup.lines(dataset, factory_id)):
            raise ValidationError(f'Line {line_id} is not in factory {factory_id}')
        if Issue.objects.filter(dataset=dataset, kind='LINE_STOPPED', line_id=line_id).exclude(status='RESOLVED').exists():
            raise ValidationError('This line is already reported stopped')
        subject = f'line {line_id}'
    elif kind == 'MATERIAL_SHORT':
        if not material_id or not arrival_date:
            raise ValidationError('Choose the material and when it is expected to arrive')
        subject = f'material {material_id}, expected {arrival_date}'
    if kind in ('QUALITY', 'OTHER') and not (note or '').strip() and not order_id:
        raise ValidationError('Describe the problem')
    check_scope(user, factory_id, line_id)
    issue = Issue.objects.create(dataset=dataset, factory_id=factory_id, line_id=line_id, kind=kind,
                                 machine_id=machine_id if kind == 'MACHINE_DOWN' else None,
                                 worker_id=worker_id if kind == 'WORKER_ABSENT' else None,
                                 order_id=str(order_id or '').strip(), note=(note or '').strip()[:2000],
                                 material_id=material_id if kind == 'MATERIAL_SHORT' else None,
                                 arrival_date=arrival_date if kind == 'MATERIAL_SHORT' else None,
                                 reported_by=user)
    issue.affected_orders = None  # material short: which planned orders wait for it (None = no approved plan)
    if kind == 'MATERIAL_SHORT':
        approved = approved_plan(dataset_id, factory_id)
        if approved is not None:
            issue.affected_orders = lookup.orders_using_material(dataset, material_id,
                                                                 planning_services._plan_orders(approved)[0])
    if kind == 'MACHINE_DOWN':  # the demo copy shows the machine broken (off unless write-back is on)
        from erp import writer
        transaction.on_commit(lambda: writer.set_machine_status(dataset, machine_id, 'Breakdown'))
    if kind in REPLANNABLE and issue.affected_orders != []:  # auto-planning (stored only when it is on)
        from planning import autoplan
        autoplan.record(dataset.pk, factory_id, 'ISSUE', issue.pk)
    label = dict(Issue.KINDS)[kind]
    planning_services.event(None, user, 'ISSUE_REPORTED',
                            f'{label}: {subject or ("order " + issue.order_id if issue.order_id else issue.note[:80])}'
                            f'{f" on line {line_id}" if line_id else ""} (issue {issue.pk})')
    return issue


@transaction.atomic
def move_issue(user, issue_id, status, resolution=''):
    issue = Issue.objects.select_for_update().filter(pk=issue_id).first()
    if issue is None:
        raise ValidationError('Issue not found')
    may_handle = user.has_perm('floor.handle_issue')
    may_repair = user.has_perm('floor.repair_machine') and issue.kind == 'MACHINE_DOWN'
    if not (may_handle or may_repair):
        raise PermissionDenied('Permission required: handle_issue')
    check_scope(user, issue.factory_id, issue.line_id)
    allowed = {'OPEN': {'IN_PROGRESS', 'RESOLVED'}, 'IN_PROGRESS': {'RESOLVED'}, 'RESOLVED': set()}
    if status not in allowed[issue.status]:
        raise ValidationError(f'An issue that is {issue.get_status_display().lower()} cannot become {status.lower()}')
    if status == 'RESOLVED' and not (resolution or '').strip():
        raise ValidationError('Say how it was resolved')
    issue.status, issue.handled_by = status, user
    if status == 'RESOLVED':
        issue.resolved_at, issue.resolution = timezone.now(), resolution.strip()[:2000]
    issue.save()
    planning_services.event(None, user, 'ISSUE_' + status,
                            f'Issue {issue.pk} ({issue.get_kind_display().lower()}) '
                            f'{"taken" if status == "IN_PROGRESS" else "resolved: " + issue.resolution[:120]}')
    if status == 'RESOLVED' and issue.kind == 'MACHINE_DOWN' and issue.machine_id:  # repaired, in the demo copy
        from erp import writer
        transaction.on_commit(lambda: writer.set_machine_status(issue.dataset, issue.machine_id, 'Active'))
    return issue


REPLANNABLE = ('MACHINE_DOWN', 'WORKER_ABSENT', 'LINE_STOPPED', 'MATERIAL_SHORT')


def disruption_inputs(issues, plan_order_ids):
    """request_disruption keyword arguments for these issues: machines down, workers absent,
    lines stopped, and orders that wait for late material (release_after, the later arrival wins)."""
    release = {}
    for issue in issues:
        if issue.kind == 'MATERIAL_SHORT' and issue.material_id and issue.arrival_date:
            for oid in lookup.orders_using_material(issue.dataset, issue.material_id, list(plan_order_ids)):
                release[oid] = max(release.get(oid, ''), issue.arrival_date.isoformat()
                                   if hasattr(issue.arrival_date, 'isoformat') else str(issue.arrival_date))
    return {'machines_down': sorted({i.machine_id for i in issues if i.kind == 'MACHINE_DOWN'}),
            'workers_absent': sorted({i.worker_id for i in issues if i.kind == 'WORKER_ABSENT'}),
            'lines_stopped': sorted({i.line_id for i in issues if i.kind == 'LINE_STOPPED'}),
            'release_after': release}


@transaction.atomic
def replan_after(user, *, dataset_id, factory_id, issue_ids, mode='QUICK'):
    """Re-plan the approved plan's orders around open issues (machines, workers, lines, late material)."""
    planning_services.authorize(user, 'request_plan')
    check_scope(user, factory_id)
    issues = list(Issue.objects.select_for_update().filter(pk__in=issue_ids, dataset_id=dataset_id, factory_id=factory_id)
                  .exclude(status='RESOLVED'))
    if not issues or len(issues) != len(set(issue_ids)):
        raise ValidationError('Choose open issues of this factory')
    if any(i.kind not in REPLANNABLE for i in issues):
        raise ValidationError('Only machine, worker, line and material issues change the plan')
    parent, _plan = floor_plan(dataset_id, factory_id)
    inputs = disruption_inputs(issues, planning_services._plan_orders(parent)[0])
    run = planning_services.request_disruption(
        user, parent_id=parent.pk, mode=mode, **inputs,
        trigger={'source': 'MANUAL', 'reason': 'Re-plan requested from the Problems board',
                 'kinds': sorted({i.kind for i in issues}), 'issue_ids': [i.pk for i in issues]})
    Issue.objects.filter(pk__in=[i.pk for i in issues]).update(replan=run)
    return run


# ---------- hourly output and quality checks ----------

def _plan_op(plan, line_id, day, operation_key):
    op = plan.ops.get(operation_key)
    if op is None or int(op['line']) != int(line_id):
        raise ValidationError('This operation is not planned on this line')
    if not any(r['key'] == operation_key for r in plan.day_ops(int(line_id), int(day))):
        raise ValidationError('This operation is not planned on this day')
    return op


@transaction.atomic
def save_output(user, *, dataset_id, factory_id, line_id, day, hour, operation_key, pieces):
    require(user, 'floor.enter_output')
    check_scope(user, factory_id, line_id)
    run, plan = floor_plan(dataset_id, factory_id)
    if not 0 <= int(hour) <= 23 or int(day) < 0:
        raise ValidationError('Choose an hour of a plan day')
    op = _plan_op(plan, line_id, day, operation_key)
    pieces = int(pieces)
    if not 0 <= pieces <= MAX_PIECES_PER_ENTRY:
        raise ValidationError('Pieces must be zero or more')
    slot = int(day) * 24 + int(hour)
    others = (OutputEntry.objects.filter(run=run, operation_key=operation_key).exclude(slot=slot)
              .aggregate(n=Sum('pieces'))['n'] or 0)
    if op['pieces'] and others + pieces > int(op['pieces']):
        raise ValidationError(f'This operation has {op["pieces"]} pieces in total and {others} are already recorded')
    from . import completion
    completion.check_output(dataset_id, factory_id, plan, run, operation_key, slot, pieces)
    entry, created = OutputEntry.objects.update_or_create(
        run=run, operation_key=operation_key, slot=slot,
        defaults={'line_id': int(line_id), 'pieces': pieces, 'entered_by': user})
    planning_services.event(None, user, 'OUTPUT_ENTERED',
                            f'Line {line_id}, {plan.date_of(int(day))} {int(hour):02d}:00: {pieces} pieces of order '
                            f'{op["order_id"]} step {op["step"]}{"" if created else " (corrected)"}')
    completion.refresh(dataset_id, factory_id, plan, user)
    return entry


@transaction.atomic
def record_check(user, *, dataset_id, factory_id, line_id, day, operation_key, checked, defects, defect_type='', note=''):
    require(user, 'floor.record_defect')
    check_scope(user, factory_id, line_id)
    run, plan = floor_plan(dataset_id, factory_id)
    op = _plan_op(plan, line_id, day, operation_key)
    checked, defects = int(checked), int(defects)
    if not 1 <= checked <= MAX_PIECES_PER_ENTRY:
        raise ValidationError('Pieces checked must be at least 1')
    if not 0 <= defects <= checked * 10:
        raise ValidationError('Defects must be between 0 and ten per checked piece')
    if defect_type and defect_type not in DefectCheck.TYPES:
        raise ValidationError('Unknown defect type')
    if defects and not defect_type:
        raise ValidationError('Choose the main defect type')
    check = DefectCheck.objects.create(run=run, line_id=int(line_id), day=int(day), operation_key=operation_key,
                                       checked=checked, defects=defects, defect_type=defect_type,
                                       note=(note or '').strip()[:2000], entered_by=user)
    planning_services.event(None, user, 'QUALITY_CHECK',
                            f'Line {line_id}, order {op["order_id"]} step {op["step"]}: {defects} defects in {checked} '
                            f'pieces ({100 * defects / checked:.1f} DHU)')
    return check


# ---------- users ----------

USERNAME = re.compile(r'[A-Za-z0-9_.-]{3,40}')


def _assign(user, role, factory_id, line_id, groups):
    user.groups.remove(*Group.objects.filter(name__in=[name for name, _ in ROLES.values()]))
    user.groups.add(groups[role])
    Profile.objects.update_or_create(user=user, defaults={'role': role, 'factory_id': factory_id, 'line_id': line_id})


def _validate_role(role, factory_id, line_id, dataset_id):
    if role not in ROLES:
        raise ValidationError('Unknown role')
    if role in LINE_REQUIRED and not line_id:
        raise ValidationError('A line supervisor needs a line')
    if line_id:
        if not factory_id:
            raise ValidationError('Choose the factory of the line')
        dataset = _dataset(dataset_id, factory_id)
        try:
            known = {int(r['line_id']) for r in lookup.lines(dataset, int(factory_id))}
        except lookup.ErpUnavailable as exc:
            raise ValidationError(f'Cannot check the line: {exc}') from exc
        if int(line_id) not in known:
            raise ValidationError(f'Line {line_id} is not in factory {factory_id}')


@transaction.atomic
def create_user(admin, *, username, role, factory_id=None, line_id=None, dataset_id=None, email=''):
    """Returns (user, password); the password is shown once to the administrator and never stored in clear."""
    require(admin, 'floor.manage_users')
    if not USERNAME.fullmatch(username or ''):
        raise ValidationError('User names are 3-40 letters, digits, dots, dashes or underscores')
    if User.objects.filter(username__iexact=username).exists():
        raise ValidationError('This user name is taken')
    _validate_role(role, factory_id, line_id, dataset_id)
    password = secrets.token_urlsafe(12)
    user = User.objects.create_user(username, password=password, email=(email or '').strip())
    _assign(user, role, factory_id, line_id, ensure_groups())
    planning_services.event(None, admin, 'USER_CREATED', f'{username} added as {ROLES[role][0].lower()}'
                            f'{f" for line {line_id}" if line_id else ""}')
    return user, password


@transaction.atomic
def update_user(admin, user_id, *, role, factory_id=None, line_id=None, active=True, dataset_id=None, email=None):
    require(admin, 'floor.manage_users')
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        raise ValidationError('User not found')
    if user.pk == admin.pk and (role != 'admin' or not active):
        raise ValidationError('You cannot remove your own administrator access')
    _validate_role(role, factory_id, line_id, dataset_id)
    _assign(user, role, factory_id, line_id, ensure_groups())
    user.is_active = bool(active)
    if email is not None:
        user.email = email.strip()
    user.save(update_fields=['is_active', 'email'])
    planning_services.event(None, admin, 'USER_CHANGED', f'{user.username}: {ROLES[role][0].lower()}'
                            f'{f", line {line_id}" if line_id else ""}{"" if active else ", deactivated"}')
    return user


@transaction.atomic
def reset_password(admin, user_id):
    require(admin, 'floor.manage_users')
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        raise ValidationError('User not found')
    password = secrets.token_urlsafe(12)
    user.set_password(password)
    user.save(update_fields=['password'])
    planning_services.event(None, admin, 'PASSWORD_RESET', f'New password issued for {user.username}')
    return user, password
