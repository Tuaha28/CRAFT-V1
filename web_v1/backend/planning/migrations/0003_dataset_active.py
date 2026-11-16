from django.db import migrations, models


def hide_v1_database(apps, schema_editor):
    # The app offers only the 30/60/90% load data; the original v1 database stays for older runs.
    apps.get_model('planning', 'Dataset').objects.filter(database='garment_fyp_db').update(active=False)


class Migration(migrations.Migration):
    dependencies = [('planning', '0002_planrun_order_ids_planrun_parent_planrun_purpose_and_more')]

    operations = [
        migrations.AddField(model_name='dataset', name='active', field=models.BooleanField(default=True)),
        migrations.RunPython(hide_v1_database, migrations.RunPython.noop),
    ]
