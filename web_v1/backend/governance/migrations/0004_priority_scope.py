from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('governance', '0003_alter_schedulescope_environment')]

    operations = [
        migrations.AlterField(
            model_name='schedulescope', name='environment',
            field=models.CharField(choices=[('TEST', 'TEST'), ('TEST_SIM', 'TEST_SIM'),
                                             ('TEST_PRIORITY', 'TEST_PRIORITY')], default='TEST', max_length=16),
        ),
        migrations.AddField(
            model_name='schedulescope', name='priority_state', field=models.JSONField(default=dict),
        ),
        migrations.AlterModelOptions(
            name='schedulescope',
            options={'permissions': [('request_run', 'Request scheduling run'),
                                     ('promote_schedule', 'Promote validated schedule'),
                                     ('change_priority', 'Change controlled order priority')]},
        ),
    ]
