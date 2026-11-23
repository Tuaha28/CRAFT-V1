"""Factory roles: what each kind of user may do, and which line or factory they see.

Roles are Django groups (so permissions stay in the standard auth tables); a
user's Profile stores the role and an optional factory/line scope. The planner
and production manager groups keep the names planning_setup already uses.
"""
from django.contrib.auth.models import Group, Permission

# What office roles read: plans, orders and styles, the worker and machine lists, the overview.
OFFICE = ['floor.view_plans', 'floor.view_orders', 'floor.view_workers', 'floor.view_machines', 'floor.view_overview']

ROLES = {
    # Runs the system (accounts, factory data, captures, auto-planning settings) and sees everything, but does
    # not approve plans or do floor work: approval stays with the production manager (separation of duties).
    'admin': ('Administrator', ['planning.request_plan', 'floor.manage_users', 'floor.view_floor', *OFFICE,
                                'floor.view_activity']),
    'manager': ('Production manager', ['planning.request_plan', 'planning.approve_plan', 'floor.view_floor',
                                       'floor.report_issue', 'floor.handle_issue', 'portal.handle_request', *OFFICE,
                                       'floor.view_activity', 'floor.complete_step', 'floor.ship_order',
                                       'floor.close_order']),
    'planner': ('Planner', ['planning.request_plan', 'floor.view_floor', 'floor.handle_issue', *OFFICE,
                            'floor.close_order']),
    # floor roles: their own work only (no plans, orders lists or worker names)
    'supervisor': ('Line supervisor', ['floor.view_floor', 'floor.report_issue', 'floor.enter_output',
                                       'floor.complete_step']),
    'qc': ('Quality inspector', ['floor.view_floor', 'floor.report_issue', 'floor.record_defect', 'floor.view_orders']),
    'maintenance': ('Maintenance technician', ['floor.view_floor', 'floor.report_issue', 'floor.repair_machine',
                                               'floor.view_machines']),
    'merchandiser': ('Merchandiser', ['portal.handle_request', 'floor.view_plans', 'floor.view_orders',
                                      'floor.view_overview', 'floor.ship_order']),
}
# A line supervisor always works for one line; the others may be limited to one.
LINE_REQUIRED = {'supervisor'}


def ensure_groups():
    """Create or refresh one group per role with exactly its permissions."""
    groups = {}
    for key, (name, perms) in ROLES.items():
        group, _ = Group.objects.get_or_create(name=name)
        wanted = []
        for perm in perms:
            app, codename = perm.split('.')
            wanted.append(Permission.objects.get(content_type__app_label=app, codename=codename))
        group.permissions.set(wanted)
        groups[key] = group
    return groups


def role_of(user):
    """The user's role key: from the profile, else inferred from the planner groups."""
    if getattr(user, 'customer_account', None) is not None:
        return 'customer'
    profile = getattr(user, 'floor_profile', None)
    if profile is not None:
        return profile.role
    if user.is_superuser:
        return 'admin'
    names = set(user.groups.values_list('name', flat=True))
    for key in ('manager', 'planner'):
        if ROLES[key][0] in names:
            return key
    return None


def describe(user):
    """Session fields the web app uses to show the right pages."""
    if not user.is_authenticated:
        return {'role': None, 'role_label': None, 'scope_factory': None, 'scope_line': None, 'perms': []}
    role = role_of(user)
    if role == 'customer':  # a customer company's sign-in: the portal only
        account = user.customer_account
        return {'role': 'customer', 'role_label': 'Customer', 'scope_factory': None, 'scope_line': None, 'perms': [],
                'customer': {'customer_id': account.customer_id, 'client_name': account.client_name}}
    profile = getattr(user, 'floor_profile', None)
    return {'role': role, 'role_label': ROLES[role][0] if role else None,
            'scope_factory': profile.factory_id if profile else None,
            'scope_line': profile.line_id if profile else None,
            'perms': sorted(p for p in user.get_all_permissions() if p.startswith(('planning.', 'floor.', 'portal.')))}
