"""Write-back to the demo copies only: approved plans and machine status. Fake connections, no MySQL."""
import json
import tempfile
from pathlib import Path
from unittest import mock
from django.contrib.auth.models import Permission, User
from django.test import TestCase, override_settings
from planning import services
from planning.models import Dataset, PlanEvent, PlanRun, WriteBack
from . import writer

DB = 'garment_fyp_db_seed_v2_data2_load30_20261018_083214'
DEMO_GRANTS = [{'g': "GRANT USAGE ON *.* TO `craft_writer`@`localhost`"},
               {'g': f"GRANT ALL PRIVILEGES ON `{DB}_demo`.* TO `craft_writer`@`localhost`"}]


class FakeCursor:
    def __init__(self, log, grants, fail_on=None):
        self.log, self.grants, self.fail_on, self.rowcount = log, grants, fail_on, 0

    def execute(self, sql, params=()):
        self.log.append((sql.split()[0].upper() if sql.strip() else sql, sql, params))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError('MySQL went away')
        self.rowcount = 2 if sql.lstrip().upper().startswith('UPDATE') else 0

    def fetchall(self):
        return self.grants

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, grants=DEMO_GRANTS, fail_on=None):
        self.log, self.grants, self.fail_on, self.closed = [], grants, fail_on, False

    def cursor(self):
        return FakeCursor(self.log, self.grants, self.fail_on)

    def close(self):
        self.closed = True


@override_settings(CRAFT_WRITEBACK=True, CRAFT_USE_DEMO_COPIES=True, CRAFT_DEMO_SUFFIX='_demo')
class WriterTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dataset = Dataset.objects.create(slug='d', label='30% load', origin='SNAPSHOT', path='x', database=DB,
                                              reference_time='2026-09-10T00:00:00', factories=[{'factory_id': 6}])
        self.manager = User.objects.create_user('manager')
        for code in ('request_plan', 'approve_plan'):
            self.manager.user_permissions.add(Permission.objects.get(codename=code))
        self.manager = User.objects.get(pk=self.manager.pk)
        out = Path(self.tmp.name) / 'craft'
        out.mkdir()
        gantt = [{'key': '477_1036', 'order_id': '477', 'step': 1, 'line': 86, 'start': 3600, 'end': 90000,
                  'machines': ['1409'], 'workers': ['1069']},
                 {'key': '477_1037', 'order_id': '477', 'step': 2, 'line': 86, 'start': 90000, 'end': 180000,
                  'machines': ['1410', '1411'], 'workers': ['1070', '1071']}]
        (out / 'digest.json').write_text(json.dumps({'reference_time': '2026-09-10T00:00:00',
                                                     'gantt': {'CRAFT_V3': gantt}, 'order_rows': []}))
        self.run = PlanRun.objects.create(kind='SCHEDULE', dataset=self.dataset, factory_id=6, status='SUCCEEDED',
                                          requested_by=self.manager, out_dir=str(out))

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_by_default_writes_nothing(self):
        connection = FakeConnection()
        with override_settings(CRAFT_WRITEBACK=False), mock.patch.object(writer, '_connect', return_value=connection):
            self.assertIsNone(writer.write_plan(self.run))
        self.assertEqual(connection.log, [])
        self.assertFalse(writer.enabled())

    def test_refuses_when_account_can_touch_an_original(self):
        connection = FakeConnection(grants=DEMO_GRANTS + [{'g': f"GRANT SELECT ON `{DB}`.* TO `craft_writer`@`localhost`"}])
        with mock.patch.object(writer, '_connect', return_value=connection):
            result = writer.write_plan(self.run)
        self.assertFalse(result.ok)
        self.assertFalse(any(kind == 'UPDATE' for kind, _sql, _p in connection.log))
        self.assertIn('original', result.error)

    def test_refuses_when_demo_copies_are_off(self):
        connection = FakeConnection()
        with override_settings(CRAFT_USE_DEMO_COPIES=False), mock.patch.object(writer, '_connect', return_value=connection):
            result = writer.write_plan(self.run)
        self.assertFalse(result.ok)
        self.assertEqual(connection.log, [])

    def test_write_plan_updates_orders_and_lots_in_one_transaction(self):
        connection = FakeConnection()
        with mock.patch.object(writer, '_connect', return_value=connection):
            result = writer.write_plan(self.run)
        self.assertTrue(result.ok, result.error)
        kinds = [k for k, _s, _p in connection.log]
        self.assertEqual(kinds[0], 'SHOW')
        self.assertEqual(kinds[1], 'START')
        self.assertEqual(kinds[-1], 'COMMIT')
        updates = [(s, p) for k, s, p in connection.log if k == 'UPDATE']
        self.assertEqual(len(updates), 3)  # two operations + one order
        lot_sql, lot_params = updates[0]
        self.assertIn('job_shop_schedules', lot_sql)
        self.assertIn("execution_status IN ('Scheduled', 'Delayed')", lot_sql)
        self.assertEqual(lot_params[:5], (86, 1409, 1069, '2026-09-10 01:00:00', '2026-09-11 01:00:00'))
        self.assertEqual(lot_params[-2:], (477, 1036))
        order_sql, order_params = updates[2]
        self.assertIn('production_orders', order_sql)
        self.assertEqual(order_params, ('2026-09-10', '2026-09-12', 477))
        self.assertEqual((result.rows, result.database, result.kind), (6, DB + '_demo', 'PLAN'))
        self.assertTrue(connection.closed)

    def test_writeback_failure_keeps_approval_and_records_event(self):
        connection = FakeConnection(fail_on='production_orders')
        with mock.patch.object(writer, '_connect', return_value=connection), \
                self.captureOnCommitCallbacks(execute=True):
            services.decide(self.manager, self.run.pk, 'APPROVED', 'Looks good')
        self.run.refresh_from_db()
        self.assertEqual(self.run.decision, 'APPROVED')
        self.assertIn('ROLLBACK', [k for k, _s, _p in connection.log])
        self.assertFalse(WriteBack.objects.get(run=self.run).ok)
        self.assertTrue(PlanEvent.objects.filter(run=self.run, action='WRITEBACK_FAILED').exists())

    def test_approval_writes_the_plan(self):
        connection = FakeConnection()
        with mock.patch.object(writer, '_connect', return_value=connection), \
                self.captureOnCommitCallbacks(execute=True):
            services.decide(self.manager, self.run.pk, 'APPROVED', 'Looks good')
        self.assertTrue(WriteBack.objects.get(run=self.run).ok)
        self.assertTrue(PlanEvent.objects.filter(run=self.run, action='WRITEBACK_DONE').exists())

    def test_bad_saved_schedule_is_recorded_not_raised(self):
        digest = Path(self.run.out_dir) / 'digest.json'
        digest.write_text(json.dumps({'reference_time': '2026-09-10T00:00:00', 'order_rows': [],
                                      'gantt': {'CRAFT_V3': [{'key': 'broken', 'line': 86, 'start': 0, 'end': 1}]}}))
        connection = FakeConnection()
        with mock.patch.object(writer, '_connect', return_value=connection):
            result = writer.write_plan(self.run)
        self.assertFalse(result.ok)
        self.assertTrue(PlanEvent.objects.filter(run=self.run, action='WRITEBACK_FAILED').exists())
        self.assertFalse(any(kind == 'UPDATE' for kind, _sql, _p in connection.log))

    def test_run_detail_shows_the_write_back_result(self):
        from rest_framework.test import APIClient
        with mock.patch.object(writer, '_connect', return_value=FakeConnection(fail_on='production_orders')), \
                self.captureOnCommitCallbacks(execute=True):
            services.decide(self.manager, self.run.pk, 'APPROVED', 'Looks good')
        client = APIClient()
        client.force_authenticate(self.manager)
        body = client.get(f'/api/v1/planning/runs/{self.run.pk}').json()
        self.assertFalse(body['writeback']['ok'])
        self.assertIn('MySQL went away', body['writeback']['error'])

    def test_machine_status_flips(self):
        connection = FakeConnection()
        with mock.patch.object(writer, '_connect', return_value=connection):
            self.assertTrue(writer.set_machine_status(self.dataset, 1409, 'Breakdown'))
        update = next((s, p) for k, s, p in connection.log if k == 'UPDATE')
        self.assertIn('machinery_assets', update[0])
        self.assertEqual(update[1], ('Breakdown', 1409))
        with self.assertRaises(ValueError):
            writer.set_machine_status(self.dataset, 1409, 'Exploded')


class ResetCommandTests(TestCase):
    """reset_demo_data rebuilds a demo copy from its original; without --run it only prints the commands."""

    def setUp(self):
        Dataset.objects.create(slug='d30', label='30% load', origin='SNAPSHOT', path='x', database=DB, load_pct=30,
                               reference_time='2026-09-10T00:00:00', factories=[{'factory_id': 6}])

    def test_prints_commands_without_running(self):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        with mock.patch('subprocess.run') as ran:
            call_command('reset_demo_data', '--load', '30', stdout=out)
        text = out.getvalue()
        self.assertIn(f'mysqldump', text)
        self.assertIn(f'{DB} ', text)
        self.assertIn(f'{DB}_demo', text)
        ran.assert_not_called()

    def test_unknown_load_is_refused(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('reset_demo_data', '--load', '45')
