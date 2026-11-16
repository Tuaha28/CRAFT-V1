from django.db import migrations


def grant(apps, schema_editor):
    """Production managers and merchandisers answer customer requests. The permission is created here because
    Django adds new permissions only after all migrations have run."""
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')
    Group = apps.get_model('auth', 'Group')
    content_type, _ = ContentType.objects.get_or_create(app_label='portal', model='customeraccount')
    perm, _ = Permission.objects.get_or_create(content_type=content_type, codename='handle_request',
                                               defaults={'name': 'Review and answer customer requests'})
    for name in ('Production manager', 'Merchandiser'):
        group = Group.objects.filter(name=name).first()
        if group is not None:
            group.permissions.add(perm)


class Migration(migrations.Migration):
    dependencies = [('portal', '0001_initial'), ('floor', '0003_admin_role_b'),
                    ('contenttypes', '0002_remove_content_type_name')]
    operations = [migrations.RunPython(grant, migrations.RunPython.noop)]
