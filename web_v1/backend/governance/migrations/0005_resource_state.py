from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('governance', '0004_priority_scope')]

    operations = [
        migrations.AlterField(model_name='schedulescope', name='environment',
            field=models.CharField(max_length=16, default='TEST', choices=[
                ('TEST', 'TEST'), ('TEST_SIM', 'TEST_SIM'), ('TEST_PRIORITY', 'TEST_PRIORITY'),
                ('TEST_RESOURCE', 'TEST_RESOURCE')])),
        migrations.AddField(model_name='schedulescope', name='resource_state', field=models.JSONField(default=dict)),
        migrations.AddField(model_name='schedulescope', name='resource_revision',
                            field=models.PositiveIntegerField(default=0)),
    ]
