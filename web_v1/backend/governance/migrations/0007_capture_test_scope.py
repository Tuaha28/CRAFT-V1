from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('governance', '0006_factory1_test_scope')]

    operations = [migrations.AlterField(
        model_name='schedulescope', name='environment',
        field=models.CharField(max_length=16, default='TEST', choices=[
            ('TEST', 'TEST'), ('TEST_SIM', 'TEST_SIM'),
            ('TEST_PRIORITY', 'TEST_PRIORITY'), ('TEST_RESOURCE', 'TEST_RESOURCE'),
            ('TEST_FACTORY1', 'TEST_FACTORY1'), ('TEST_CAPTURE', 'TEST_CAPTURE')]))]
