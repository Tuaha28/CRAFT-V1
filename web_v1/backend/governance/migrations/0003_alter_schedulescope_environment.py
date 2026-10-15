from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('governance', '0002_scheduleversion_one_active_schedule_per_scope')]

    operations = [migrations.AlterField(
        model_name='schedulescope', name='environment',
        field=models.CharField(choices=[('TEST', 'TEST'), ('TEST_SIM', 'TEST_SIM')], default='TEST', max_length=12),
    )]
