import json
import sys
import tempfile
from pathlib import Path
from unittest import mock
from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from . import jobs, services
from .datasets import discover
from .models import Dataset, PlanEvent, PlanRun


def _capture(root, name, database='garment_fyp_db_seed_v2_data2_load60_x'):
    path = Path(root) / name
    path.mkdir()
    (path / 'report.json').write_text(json.dumps({
        'dataset': {'database': database, 'reference_time': '2026-09-10T00:00:00'},
        'factory_summaries': [{'factory_id': 4, 'orders': 147, 'operations': 900,
                               'resource_counts': {'lines': 2, 'machines': 46, 'workers': 102}},
                              {'factory_id': 5, 'orders': 3, 'operations': 9, 'resource_counts': {}}]}))
    (path / 'manifest.json').write_text(json.dumps({'factory4_source.json': 'abc', 'report.json': 'def'}))
    (path / 'factory4_readiness.json').write_text('{}')
    return path


def _user(name, *perms):
    user = User.objects.create_user(name)
    for perm in perms:
        user.user_permissions.add(Permission.objects.get(codename=perm))
    return User.objects.get(pk=user.pk)  # fresh permission cache


class Fixture(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _capture(self.tmp.name, 'capture_20261018T084010774436Z')
        self.settings_override = override_settings(PLANNING_CAPTURE_ROOTS=[self.tmp.name],
                                                   PLANNING_RUNS_DIR=str(Path(self.tmp.name) / 'runs'))
        self.settings_override.enable()
        self.dataset = discover()[0]
        self.planner = _user('planner', 'request_plan')
        self.manager = _user('manager', 'request_plan', 'approve_plan')
        self.viewer = _user('viewer')
        self.reader = _user('reader', 'view_plans')  # an office role that may see plans

    def tearDown(self):
        self.settings_override.disable()
        self.tmp.cleanup()

    def request(self, **overrides):
        values = {'dataset_id': self.dataset.pk, 'factory_id': 4, 'orders': 10, 'order_offset': 0, 'mode': 'QUICK'}
        return services.request_schedule(self.planner, **{**values, **overrides})


def _web_capture(root, name, database='garment_fyp_db_seed_v2_data2_load60_x'):
    path = _capture(root, name, database)
    (path / 'web_capture.json').write_text('{}')
    return path


class OrderbookCacheTests(Fixture):
    def test_orderbook_is_read_again_after_a_refresh(self):
        def fake(cmd, **kwargs):
            Path(cmd[-1]).write_text('{"eligible": []}')
            return mock.Mock(returncode=0, stderr='')
        with mock.patch('planning.jobs.subprocess.run', side_effect=fake) as ran:
            jobs.orderbook(self.dataset, 4)
            jobs.orderbook(self.dataset, 4)
            self.assertEqual(ran.call_count, 1)  # cached
            _web_capture(self.tmp.name, 'capture_20261116T160707000000Z')
            discover()
            jobs.orderbook(Dataset.objects.get(pk=self.dataset.pk), 4)
            self.assertEqual(ran.call_count, 2)  # new snapshot, read again


class DatasetTests(Fixture):
    def test_web_capture_refreshes_the_dataset_of_the_same_database(self):
        fresh = _web_capture(self.tmp.name, 'capture_20261116T160707000000Z')
        discover()
        self.assertEqual(Dataset.objects.filter(active=True).count(), 1)  # still one "60% load"
        dataset = Dataset.objects.get(pk=self.dataset.pk)
        self.assertEqual((dataset.path, dataset.captured_at), (str(fresh.resolve()), '20261116T160707'))
        discover()  # idempotent: the original snapshot folder does not take it back
        self.assertEqual(Dataset.objects.get(pk=self.dataset.pk).path, str(fresh.resolve()))
        self.assertEqual(Dataset.objects.count(), 1)

    def test_capture_of_the_demo_copy_refreshes_the_original_dataset(self):
        fresh = _web_capture(self.tmp.name, 'capture_20261116T160707000000Z',
                             database='garment_fyp_db_seed_v2_data2_load60_x_demo')
        discover()
        self.assertEqual(Dataset.objects.count(), 1)
        self.assertEqual(Dataset.objects.get(pk=self.dataset.pk).path, str(fresh.resolve()))

    def test_newest_web_capture_wins(self):
        newer = _web_capture(self.tmp.name, 'capture_20261117T020000000000Z')
        _web_capture(self.tmp.name, 'capture_20261116T160707000000Z')
        discover()
        discover()
        self.assertEqual(Dataset.objects.get(pk=self.dataset.pk).path, str(newer.resolve()))

    def test_a_plan_keeps_the_snapshot_it_was_requested_on(self):
        before = self.dataset.path
        run = self.request()
        _web_capture(self.tmp.name, 'capture_20261116T160707000000Z')
        discover()
        run.refresh_from_db()
        self.assertEqual(run.capture_path, before)
        self.assertNotEqual(self.request().capture_path, before)

    def test_fold_duplicates_merges_an_earlier_extra_entry(self):
        from .datasets import fold_duplicates
        fresh = _capture(self.tmp.name, 'capture_20261116T160707000000Z')
        extra = Dataset.objects.create(slug=fresh.name, label='60% load', origin='LIVE_CAPTURE',
                                       path=str(fresh.resolve()), database=self.dataset.database, load_pct=60,
                                       reference_time='2026-09-10T00:00:00', factories=self.dataset.factories,
                                       captured_at='20261116T160707')
        fold_duplicates()
        self.assertFalse(Dataset.objects.get(pk=extra.pk).active)
        self.assertEqual(Dataset.objects.get(pk=self.dataset.pk).path, str(fresh.resolve()))

    def test_discover_reads_load_and_only_manifest_factories(self):
        self.assertEqual(self.dataset.load_pct, 60)
        self.assertEqual(self.dataset.label, '60% load')
        self.assertEqual([f['factory_id'] for f in self.dataset.factories], [4])  # factory 5 has no pinned files
        self.assertEqual(self.dataset.origin, 'SNAPSHOT')
        discover()
        self.assertEqual(Dataset.objects.count(), 1)  # idempotent

    def test_v1_database_capture_is_hidden_when_first_found(self):
        _capture(self.tmp.name, 'capture_20261012T223420800478Z', database='garment_fyp_db')
        discover()
        self.assertFalse(Dataset.objects.get(database='garment_fyp_db').active)
        self.assertTrue(Dataset.objects.get(pk=self.dataset.pk).active)

    def test_hidden_dataset_is_not_listed_but_its_runs_still_load(self):
        _capture(self.tmp.name, 'capture_20261012T223420800478Z', database='garment_fyp_db')
        discover()
        old = Dataset.objects.get(database='garment_fyp_db')
        old.active = False
        old.save()
        run = self.request(dataset_id=old.pk)
        client = APIClient()
        client.force_authenticate(self.manager)
        listed = [d['id'] for d in client.get('/api/v1/planning/datasets').json()]
        self.assertNotIn(old.pk, listed)
        self.assertIn(self.dataset.pk, listed)
        self.assertFalse(Dataset.objects.get(pk=old.pk).active)  # listing re-discovers; must not re-activate
        self.assertEqual(client.get(f'/api/v1/planning/runs/{run.pk}').status_code, 200)


class RequestTests(Fixture):
    def test_valid_request_is_queued_with_event(self):
        run = self.request()
        self.assertEqual((run.status, run.mode, run.orders), ('QUEUED', 'QUICK', 10))
        self.assertEqual(run.events.get().action, 'PLAN_REQUESTED')

    def test_rejects_unknown_factory_bad_mode_and_size(self):
        for overrides in ({'factory_id': 5}, {'mode': 'FAST'}, {'orders': 0}, {'orders': 31}, {'order_offset': -1}):
            with self.assertRaises(ValidationError):
                self.request(**overrides)

    def test_needs_permission(self):
        with self.assertRaises(PermissionDenied):
            services.request_schedule(self.viewer, dataset_id=self.dataset.pk, factory_id=4, orders=5,
                                      order_offset=0, mode='QUICK')

    def test_one_capture_at_a_time(self):
        services.request_capture(self.planner)
        with self.assertRaises(ValidationError):
            services.request_capture(self.planner)


class LifecycleTests(Fixture):
    def test_claim_cancel_and_finish_are_compare_and_swap(self):
        run = self.request()
        self.assertEqual(services.claim().pk, run.pk)
        self.assertIsNone(services.claim())
        self.assertTrue(services.cancel(self.planner, run.pk, 'wrong orders'))
        self.assertFalse(services.finish(run.pk, 'SUCCEEDED'))  # a late worker cannot undo a cancel
        self.assertEqual(PlanRun.objects.get(pk=run.pk).status, 'CANCELLED')

    def test_decision_rules(self):
        run = self.request()
        with self.assertRaises(ValidationError):
            services.decide(self.manager, run.pk, 'APPROVED', 'ok')  # not finished
        services.claim()
        services.finish(run.pk, 'SUCCEEDED')
        with self.assertRaises(PermissionDenied):
            services.decide(self.planner, run.pk, 'APPROVED', 'ok')
        with self.assertRaises(ValidationError):
            services.decide(self.manager, run.pk, 'APPROVED', '  ')
        services.decide(self.manager, run.pk, 'APPROVED', 'Fewer late orders than EDD-Crew')
        with self.assertRaises(ValidationError):
            services.decide(self.manager, run.pk, 'REJECTED', 'changed my mind')
        self.assertEqual(PlanRun.objects.get(pk=run.pk).decision, 'APPROVED')

    def test_silent_worker_run_fails(self):
        run = self.request()
        services.claim()
        PlanRun.objects.filter(pk=run.pk).update(heartbeat_at='2020-01-01T00:00:00Z')
        services.expire_orphans()
        self.assertEqual(PlanRun.objects.get(pk=run.pk).status, 'FAILED')


class JobTests(Fixture):
    def test_modes_match_locked_v3_and_quick(self):
        run = self.request(mode='FULL', orders=10, order_offset=20)
        command = jobs.schedule_command(run, 'out')
        self.assertEqual(command[2:], ['4', 'out', '--orders', '10', '--order-offset', '20', '--seconds', '1800',
                                       '--horizon-extra-days', '90', '--mode', 'v3', '--rules', 'EDD'])
        env = jobs.scheduler_env(self.dataset.path, 'FULL')
        self.assertEqual((env['CRAFT_STAGE_SECONDS'], env['CRAFT_OBJECTIVE_SECONDS'], env['CRAFT_SOLVER_WORKERS'],
                          env['CRAFT_DISPATCH_WINDOW_DAYS'], env['CRAFT_WORKER_LINE_POLICY']),
                         ('10,20,30', '30', '8', '14', 'BATCH_LINE_THEN_FACTORY'))
        self.assertEqual(jobs.scheduler_env(self.dataset.path, 'QUICK')['CRAFT_STAGE_SECONDS'], '3,5,8')

    def test_scheduler_env_never_carries_database_or_django_secrets(self):
        with mock.patch.dict('os.environ', {'LEGACY_DB_PASSWORD': 'x', 'DJANGO_SECRET_KEY': 'y'}):
            env = jobs.scheduler_env(self.dataset.path, 'QUICK')
        self.assertNotIn('LEGACY_DB_PASSWORD', env)
        self.assertNotIn('DJANGO_SECRET_KEY', env)

    def test_progress_from_report(self):
        out = Path(self.tmp.name) / 'p'
        out.mkdir()
        self.assertEqual(jobs.progress(out), (0.0, 'EDD rule'))
        (out / 'report.json').write_text(json.dumps({'results': [{'policy': p} for p in ('EDD', 'EDD_CREW')]}))
        self.assertEqual(jobs.progress(out), (0.11, 'Savable-first with crews'))

    def test_summarize_compares_with_edd_crew(self):
        data = {'operations': 61, 'methods': [
            {'policy': 'EDD_CREW', 'late': 8, 'mean_tardiness_days': 14.85},
            {'policy': 'CRAFT_V3', 'late': 6, 'mean_tardiness_days': 11.09, 'complete': 10}],
            'craft_v3': {'kept_start': 'CRAFT_TW'}}
        summary = jobs.summarize(data)
        self.assertEqual(summary['vs_edd_crew'], {'late': -2, 'mean_tardiness_days': -3.76})
        self.assertEqual(summary['kept_start'], 'CRAFT_TW')


FAKE_RUNNER = '''import json, sys
from pathlib import Path
out = Path(sys.argv[2]); out.mkdir(parents=True)
(out / "report.json").write_text(json.dumps({"results": [{"policy": "EDD"}]}))
print("done")
'''


class WorkerTests(Fixture):
    def test_worker_runs_and_ingests(self):
        run = self.request()
        fake = Path(self.tmp.name) / 'fake_runner.py'
        fake.write_text(FAKE_RUNNER)
        digest = {'operations': 3, 'methods': [{'policy': 'CRAFT_V3', 'late': 1, 'mean_tardiness_days': 0.5}],
                  'craft_v3': {}}
        with override_settings(PLANNING_SCHEDULER_PYTHON=sys.executable), \
                mock.patch.object(jobs, 'schedule_command', lambda r, out: [sys.executable, str(fake), '4', str(out)]), \
                mock.patch.object(jobs, 'digest', return_value=digest), \
                mock.patch('planning.management.commands.planning_worker.POLL_SECONDS', 0.05):
            call_command('planning_worker', '--once')
        run.refresh_from_db()
        self.assertEqual((run.status, run.progress), ('SUCCEEDED', 1.0))
        self.assertEqual(run.summary['craft_v3']['late'], 1)
        self.assertIn('done', run.log_tail)

    def test_failed_runner_is_reported(self):
        run = self.request()
        with override_settings(PLANNING_SCHEDULER_PYTHON=sys.executable), \
                mock.patch.object(jobs, 'schedule_command',
                                  lambda r, out: [sys.executable, '-c', 'raise ValueError("NO_VALID_START_SCHEDULE")']), \
                mock.patch('planning.management.commands.planning_worker.POLL_SECONDS', 0.05):
            call_command('planning_worker', '--once')
        run.refresh_from_db()
        self.assertEqual(run.status, 'FAILED')
        self.assertIn('NO_VALID_START_SCHEDULE', run.error)


class ApiTests(Fixture):
    def test_anonymous_is_refused_and_session_reports_permissions(self):
        client = APIClient()
        self.assertEqual(client.get('/api/v1/planning/runs').status_code, 403)
        self.assertFalse(client.get('/api/v1/planning/session').json()['authenticated'])
        client.force_authenticate(self.manager)
        body = client.get('/api/v1/planning/session').json()
        self.assertTrue(body['can_approve'])
        self.assertFalse(body['production_activation'])

    def test_request_and_read_run(self):
        client = APIClient()
        client.force_authenticate(self.planner)
        created = client.post('/api/v1/planning/runs', {'dataset_id': self.dataset.pk, 'factory_id': 4,
                                                        'orders': 10, 'order_offset': 10, 'mode': 'FULL'},
                              format='json')
        self.assertEqual(created.status_code, 201)
        detail = client.get(f'/api/v1/planning/runs/{created.json()["id"]}').json()
        self.assertEqual((detail['status'], detail['order_offset'], detail['digest']), ('QUEUED', 10, None))
        bad = client.post('/api/v1/planning/runs', {'dataset_id': self.dataset.pk, 'factory_id': 9,
                                                    'orders': 10, 'order_offset': 0, 'mode': 'FULL'}, format='json')
        self.assertEqual(bad.status_code, 409)

    def test_reading_runs_expires_a_dead_worker_and_reports_no_worker(self):
        stuck, waiting = self.request(), None
        services.claim()
        PlanRun.objects.filter(pk=stuck.pk).update(heartbeat_at='2020-01-01T00:00:00Z')
        waiting = self.request(order_offset=10)
        client = APIClient()
        client.force_authenticate(self.reader)
        rows = {r['id']: r for r in client.get('/api/v1/planning/runs').json()}
        self.assertEqual(rows[str(stuck.pk)]['status'], 'FAILED')  # no worker needed to clear it
        self.assertTrue(rows[str(waiting.pk)]['worker_down'])
        services.worker_beat()
        self.assertFalse(client.get(f'/api/v1/planning/runs/{waiting.pk}').json()['worker_down'])

    def test_experiments_endpoint(self):
        client = APIClient()
        client.force_authenticate(self.reader)
        path = Path(self.tmp.name) / 'paper_data.json'
        path.write_text(json.dumps({'cases': [], 'v31_done': False}))
        with override_settings(PLANNING_EXPERIMENTS_DIR=self.tmp.name):
            self.assertEqual(client.get('/api/v1/planning/experiments').json()['v31_done'], False)


class CurrentPlanTests(Fixture):
    def _finished(self, decision='PENDING', minutes=0):
        run = self.request()
        services.claim()
        out = Path(self.tmp.name) / str(run.pk)
        out.mkdir()
        (out / 'digest.json').write_text(json.dumps({'gantt': {'CRAFT_V3': []}, 'run': str(run.pk)}))
        services.finish(run.pk, 'SUCCEEDED', out_dir=str(out))
        if decision != 'PENDING':
            services.decide(self.manager, run.pk, decision, 'test decision')
        return run

    def test_dashboard_prefers_the_approved_plan_and_skips_rejected(self):
        client = APIClient()
        client.force_authenticate(self.reader)
        url = f'/api/v1/planning/current/{self.dataset.pk}/4'
        self.assertIsNone(client.get(url).json()['run'])
        approved = self._finished('APPROVED')
        self._finished()  # newer, not decided
        rejected = self._finished('REJECTED')
        body = client.get(url).json()
        self.assertEqual(body['run']['id'], str(approved.pk))
        self.assertEqual(body['digest']['run'], str(approved.pk))
        self.assertNotEqual(body['run']['id'], str(rejected.pk))


class WhatIfTests(Fixture):
    def _finished(self):
        run = self.request()
        services.claim()
        out = Path(self.tmp.name) / str(run.pk)
        out.mkdir()
        (out / 'digest.json').write_text(json.dumps({'order_rows': [{'order_id': '11'}, {'order_id': '12'}]}))
        services.finish(run.pk, 'SUCCEEDED', out_dir=str(out))
        return PlanRun.objects.get(pk=run.pk)

    ROUTING = [{'operation_id': 7, 'operation_name': 'Side seam', 'sequence': 1,
                'machine_type': 'Overlock Serger (4-Thread)', 'sam': 1.5}]

    def test_disruption_replans_the_same_orders(self):
        parent = self._finished()
        run = services.request_disruption(self.planner, parent_id=parent.pk, machines_down=[1515, 1515], workers_absent=[])
        self.assertEqual((run.purpose, run.parent_id, run.order_ids), ('DISRUPTION', parent.pk, ['11', '12']))
        self.assertEqual(run.scenario, {'machines_down': [1515], 'workers_absent': []})
        self.assertEqual(jobs.schedule_command(run, 'out')[4:6], ['--order-ids', '11,12'])

    def test_disruption_merges_lines_release_and_priority(self):
        parent = self._finished()
        run = services.request_disruption(self.planner, parent_id=parent.pk, lines_stopped=[86, 86],
                                          release_after={'11': '2026-09-15'}, priority_orders=['12'],
                                          trigger={'source': 'MANUAL', 'reason': 'test'})
        self.assertEqual(run.scenario, {'machines_down': [], 'workers_absent': [], 'lines_stopped': [86],
                                        'release_after': {'11': '2026-09-15'}, 'priority_orders': ['12']})
        self.assertEqual(run.trigger, {'source': 'MANUAL', 'reason': 'test'})
        message = run.events.get(action='DISRUPTION_REQUESTED').message
        for words in ('line 86 stopped', 'order 11 waits for material until 15 Sep', 'order 12 marked Priority'):
            self.assertIn(words, message)

    def test_later_release_date_wins_when_inherited(self):
        parent = self._finished()
        first = services.request_disruption(self.planner, parent_id=parent.pk, release_after={'11': '2026-09-20'})
        PlanRun.objects.filter(pk=first.pk).update(status='SUCCEEDED', out_dir=parent.out_dir)
        second = services.request_disruption(self.planner, parent_id=first.pk, release_after={'11': '2026-09-15'})
        self.assertEqual(second.scenario['release_after'], {'11': '2026-09-20'})

    def test_disruption_with_nothing_is_rejected(self):
        parent = self._finished()
        with self.assertRaises(ValidationError):
            services.request_disruption(self.planner, parent_id=parent.pk, lines_stopped=[], release_after={},
                                        priority_orders=[])

    def test_disruption_needs_a_resource_and_a_finished_plan(self):
        parent = self._finished()
        with self.assertRaises(ValidationError):
            services.request_disruption(self.planner, parent_id=parent.pk)
        queued = self.request()
        with self.assertRaises(ValidationError):
            services.request_disruption(self.planner, parent_id=queued.pk, machines_down=[1])

    def test_promise_adds_one_new_order(self):
        parent = self._finished()
        run = services.request_promise(self.planner, parent_id=parent.pk, product_id=432, quantity=200,
                                       release='2026-09-12', due='2026-09-30', routing=self.ROUTING)
        new_id = run.scenario['new_order']['order_id']
        self.assertEqual(run.order_ids, ['11', '12', str(new_id)])
        for bad in ({'due': '2026-09-01'}, {'quantity': 0}, {'routing': []}):
            with self.assertRaises(ValidationError):
                services.request_promise(self.planner, parent_id=parent.pk, product_id=432,
                                         **{'quantity': 200, 'release': '2026-09-12', 'due': '2026-09-30',
                                            'routing': self.ROUTING, **bad})

    def test_promise_summary_reports_finish_against_due(self):
        parent = self._finished()
        run = services.request_promise(self.planner, parent_id=parent.pk, product_id=432, quantity=200,
                                       release='2026-09-12', due='2026-09-30', routing=self.ROUTING)
        oid = str(run.scenario['new_order']['order_id'])
        data = {'methods': [], 'order_rows': [{'order_id': oid, 'due_day': 20.0, 'status': 'Late', 'reason': 'x',
                                               'methods': {'CRAFT_V3': {'finish_day': 23.5, 'complete': True}}}]}
        self.assertEqual(jobs.summarize(data, run)['promise']['days_late'], 3.5)

    def test_worker_builds_the_scenario_before_planning(self):
        parent = self._finished()
        run = services.request_disruption(self.planner, parent_id=parent.pk, workers_absent=[1265])
        PlanRun.objects.filter(status='QUEUED').exclude(pk=run.pk).update(status='CANCELLED')
        fake = Path(self.tmp.name) / 'fake_runner.py'
        fake.write_text(FAKE_RUNNER)
        built = {}

        def scenario(r, base):
            built['scenario'] = r.scenario
            return str(Path(base) / 'scenario')
        with override_settings(PLANNING_SCHEDULER_PYTHON=sys.executable), \
                mock.patch.object(jobs, 'build_scenario', scenario), \
                mock.patch.object(jobs, 'schedule_command', lambda r, out: [sys.executable, str(fake), '4', str(out)]), \
                mock.patch.object(jobs, 'digest', return_value={'methods': [], 'craft_v3': {}}), \
                mock.patch('planning.management.commands.planning_worker.POLL_SECONDS', 0.05):
            call_command('planning_worker', '--once')
        run.refresh_from_db()
        self.assertEqual(run.status, 'SUCCEEDED')
        self.assertEqual(built['scenario']['workers_absent'], [1265])
        self.assertTrue(run.scenario_dir.endswith('scenario'))


class PriorityTests(Fixture):
    """A manager marks an order Priority; CRAFT then plans it first (due at the plan start)."""

    def _set(self, user, order_id, on, reason=''):
        return services.set_priority(user, dataset_id=self.dataset.pk, factory_id=4, order_id=order_id, on=on,
                                     reason=reason)

    def test_priority_orders_reach_new_plans(self):
        self._set(self.planner, '11', True, 'Client escalation')
        self.assertEqual(services.priority_orders(self.dataset.pk, 4), ['11'])
        run = self.request()
        self.assertEqual(run.scenario, {'priority_orders': ['11']})
        self.assertTrue(PlanEvent.objects.filter(action='PRIORITY_ON', message__contains='11').exists())

    def test_priority_off_removes_it(self):
        self._set(self.planner, '11', True)
        self._set(self.planner, '11', False)
        self.assertEqual(services.priority_orders(self.dataset.pk, 4), [])
        self.assertEqual(self.request().scenario, {})
        self.assertTrue(PlanEvent.objects.filter(action='PRIORITY_OFF').exists())

    def test_priority_needs_request_plan(self):
        with self.assertRaises(PermissionDenied):
            self._set(self.viewer, '11', True)

    def test_priority_reaches_disruption_runs(self):
        run = self.request()
        services.claim()
        out = Path(self.tmp.name) / str(run.pk)
        out.mkdir()
        (out / 'digest.json').write_text(json.dumps({'order_rows': [{'order_id': '11'}, {'order_id': '12'}]}))
        services.finish(run.pk, 'SUCCEEDED', out_dir=str(out))
        self._set(self.planner, '12', True)
        replan = services.request_disruption(self.planner, parent_id=run.pk, machines_down=[5])
        self.assertEqual(replan.scenario['priority_orders'], ['12'])

    def test_priority_api(self):
        client = APIClient()
        client.force_authenticate(self.planner)
        body = {'dataset': self.dataset.pk, 'factory': 4, 'order_id': '11', 'on': True, 'reason': 'Rush'}
        self.assertEqual(client.post('/api/v1/planning/priority', body, format='json').status_code, 200)
        listed = client.get(f'/api/v1/planning/priority?dataset={self.dataset.pk}&factory=4').json()
        self.assertEqual(listed, {'orders': ['11']})
