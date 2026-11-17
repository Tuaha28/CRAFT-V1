from django.db import migrations

NEW = {'view_plans': 'See plans and their results', 'view_orders': 'See orders and styles',
       'view_workers': 'See the worker list (names and skills)', 'view_machines': 'See the machine list',
       'view_overview': 'See the factory overview, stock and quality summary',
       'view_activity': 'See the activity log (who did what)'}


def refresh(apps, schema_editor):
    """Create the page permissions now (Django adds new ones only after all migrations) and give every existing
    role group exactly the permissions in floor.roles: each role reads what its job needs."""
    from floor.roles import ROLES
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    Group = apps.get_model('auth', 'Group')
    profile, _ = ContentType.objects.get_or_create(app_label='floor', model='profile')
    for code, name in NEW.items():
        Permission.objects.get_or_create(content_type=profile, codename=code, defaults={'name': name})
    for _key, (name, perms) in ROLES.items():
        group = Group.objects.filter(name=name).first()
        if group is None:
            continue
        wanted = [Permission.objects.filter(content_type__app_label=p.split('.')[0], codename=p.split('.')[1]).first()
                  for p in perms]
        group.permissions.set([p for p in wanted if p is not None])


class Migration(migrations.Migration):
    dependencies = [('floor', '0004_view_permissions'), ('portal', '0002_handle_request_for_roles'),
                    ('contenttypes', '0002_remove_content_type_name')]
    operations = [migrations.RunPython(refresh, migrations.RunPython.noop)]
