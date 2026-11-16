from django.db import migrations


def refresh_groups(apps, schema_editor):
    """Give every role group exactly the permissions in floor.roles (the administrator no longer approves plans
    or does floor work). Groups that do not exist yet are left for ensure_groups()."""
    from floor.roles import ROLES
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    for _key, (name, perms) in ROLES.items():
        group = Group.objects.filter(name=name).first()
        if group is None:
            continue
        wanted = [Permission.objects.filter(content_type__app_label=perm.split('.')[0],
                                            codename=perm.split('.')[1]).first() for perm in perms]
        group.permissions.set([p for p in wanted if p is not None])


class Migration(migrations.Migration):
    dependencies = [
        ('floor', '0002_issue_line_stopped_material'),
        ('planning', '0009_run_source_path'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]
    operations = [migrations.RunPython(refresh_groups, migrations.RunPython.noop)]
