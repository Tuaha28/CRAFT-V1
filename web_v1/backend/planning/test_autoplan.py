"""Auto-planning: stored triggers become one Quick re-plan per burst; a manager approves it."""
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from floor import services as floor_services
from floor.models import Issue
from . import autoplan, services
from .models import AutoPlanSetting, AutoTrigger, Dataset, PlanRun

T0 = timezone.now().replace(microsecond=0)


def _user(name, *perms):
    user = User.objects.create_user(name)
    for perm in perms:
        app, code = perm.split('.')
        user.user_permissions.add(Permission.objects.get(codename=code, content_type__app_label=app))
    return User.objects.get(pk=user.pk)


@override_settings(AUTO_PLAN_QUIET_MINUTES=15, AUTO_PLAN_MAX_WAIT_MINUTES=60)
class AutoPlanBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dataset = Dataset.objects.create(slug='d', label='30% load', origin='SNAPSHOT', path='x',
                                              database='garment_fyp_db_seed_v2_data2_load30_x',
                                              reference_time='2026-09-10T00:00:00', factories=[{'factory_id': 6}])
        self.manager = _user('manager', 'planning.request_plan', 'planning.approve_plan', 'floor.report_issue')
        out = Path(self.tmp.name) / 'approved'
        out.mkdir()
        (out / 'digest.json').write_text(json.dumps({'order_rows': [{'order_id': '1'}, {'order_id': '2'}]}))
        self.approved = PlanRun.objects.create(kind='SCHEDULE', dataset=self.dataset, factory_id=6, status='SUCCEEDED',
                                               requested_by=self.manager, decision='APPROVED', decided_at=T0,
                                               out_dir=str(out))

    def tearDown(self):
        self.tmp.cleanup()

    def enable(self):
        AutoPlanSetting.objects.create(dataset=self.dataset, factory_id=6, enabled=True, set_by=self.manager)

    def machine_down(self, machine_id, at):
        issue = Issue.objects.create(dataset=self.dataset, factory_id=6, kind='MACHINE_DOWN', machine_id=machine_id,
                                     line_id=86, reported_by=self.manager)
        trigger = autoplan.record(self.dataset.pk, 6, 'ISSUE', str(issue.pk))
        if trigger is not None:
            AutoTrigger.objects.filter(pk=trigger.pk).update(created_at=at)
        return issue

    def finish(self, run, decision='PENDING'):
        out = Path(self.tmp.name) / str(run.pk)
        out.mkdir()
        (out / 'digest.json').write_text(json.dumps({'order_rows': [{'order_id': '1'}, {'order_id': '2'}]}))
        PlanRun.objects.filter(pk=run.pk).update(status='SUCCEEDED', out_dir=str(out), decision=decision)
        return PlanRun.objects.get(pk=run.pk)


@override_settings(AUTO_PLAN_QUIET_MINUTES=15, AUTO_PLAN_MAX_WAIT_MINUTES=60)
class AutoPlanTests(AutoPlanBase):
    def test_off_by_default_records_nothing(self):
        self.machine_down(12, T0)
        self.assertEqual(AutoTrigger.objects.count(), 0)
        self.assertEqual(autoplan.tick(T0 + timedelta(hours=2)), [])

    def test_burst_of_triggers_makes_one_run(self):
        self.enable()
        for i in range(5):
            self.machine_down(10 + i, T0 + timedelta(minutes=i))
        self.assertEqual(autoplan.tick(T0 + timedelta(minutes=10)), [])
        runs = autoplan.tick(T0 + timedelta(minutes=20))
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run.scenario['machines_down'], [10, 11, 12, 13, 14])
        self.assertEqual((run.mode, run.parent_id, run.requested_by.username), ('QUICK', self.approved.pk, 'craft_auto'))
        self.assertEqual(run.trigger['source'], 'AUTO')
        self.assertIn('Machine 10 down', run.trigger['reason'])
        self.assertEqual(set(Issue.objects.values_list('replan_id', flat=True)), {run.pk})
        self.assertFalse(AutoTrigger.objects.filter(consumed_at__isnull=True).exists())

    def test_max_wait_caps_a_trickle(self):
        self.enable()
        for i in range(7):
            self.machine_down(10 + i, T0 + timedelta(minutes=10 * i))
        self.assertEqual(len(autoplan.tick(T0 + timedelta(minutes=61))), 1)

    def test_resolved_issue_inside_window_makes_no_run(self):
        self.enable()
        issue = self.machine_down(12, T0)
        Issue.objects.filter(pk=issue.pk).update(status='RESOLVED')
        self.assertEqual(autoplan.tick(T0 + timedelta(minutes=20)), [])
        self.assertFalse(PlanRun.objects.filter(purpose='DISRUPTION').exists())
        self.assertFalse(AutoTrigger.objects.filter(consumed_at__isnull=True).exists())

    def test_priority_on_then_off_nets_to_nothing(self):
        self.enable()
        services.set_priority(self.manager, dataset_id=self.dataset.pk, factory_id=6, order_id='1', on=True)
        services.set_priority(self.manager, dataset_id=self.dataset.pk, factory_id=6, order_id='1', on=False)
        AutoTrigger.objects.update(created_at=T0)
        self.assertEqual(AutoTrigger.objects.count(), 2)
        self.assertEqual(autoplan.tick(T0 + timedelta(minutes=20)), [])
        self.assertFalse(PlanRun.objects.filter(purpose='DISRUPTION').exists())

    def test_priority_on_makes_a_run_with_it(self):
        self.enable()
        services.set_priority(self.manager, dataset_id=self.dataset.pk, factory_id=6, order_id='2', on=True)
        AutoTrigger.objects.update(created_at=T0)
        run = autoplan.tick(T0 + timedelta(minutes=20))[0]
        self.assertEqual(run.scenario['priority_orders'], ['2'])
        self.assertEqual(run.trigger['priority'], {'on': ['2'], 'off': []})

    def test_skips_while_any_run_is_active(self):
        self.enable()
        self.machine_down(12, T0)
        PlanRun.objects.create(kind='SCHEDULE', dataset=self.dataset, factory_id=6, status='RUNNING',
                               requested_by=self.manager)
        self.assertEqual(autoplan.tick(T0 + timedelta(minutes=20)), [])
        self.assertTrue(AutoTrigger.objects.filter(consumed_at__isnull=True).exists())

    def test_pending_auto_run_is_superseded(self):
        self.enable()
        first_issue = self.machine_down(12, T0)
        first = self.finish(autoplan.tick(T0 + timedelta(minutes=20))[0])
        second_issue = self.machine_down(13, T0 + timedelta(minutes=30))
        second = autoplan.tick(T0 + timedelta(minutes=50))[0]
        first.refresh_from_db()
        self.assertEqual(first.decision, 'SUPERSEDED')
        self.assertEqual(sorted(second.trigger['issue_ids']), sorted([first_issue.pk, second_issue.pk]))
        self.assertEqual(second.scenario['machines_down'], [12, 13])
        self.assertTrue(first.events.filter(action='PLAN_SUPERSEDED').exists())

    def test_rejected_auto_run_is_not_retried(self):
        self.enable()
        self.machine_down(12, T0)
        run = self.finish(autoplan.tick(T0 + timedelta(minutes=20))[0])
        services.decide(self.manager, run.pk, 'REJECTED', 'Not needed')
        self.assertEqual(autoplan.tick(T0 + timedelta(hours=3)), [])

    def test_no_approved_plan_means_no_run(self):
        self.enable()
        PlanRun.objects.filter(pk=self.approved.pk).update(decision='PENDING')
        self.machine_down(12, T0)
        self.assertEqual(autoplan.tick(T0 + timedelta(minutes=20)), [])

    def test_material_used_by_no_planned_order_makes_no_trigger(self):
        self.enable()
        with mock.patch('floor.lookup.orders_using_material', return_value=[]):
            floor_services.report_issue(self.manager, dataset_id=self.dataset.pk, factory_id=6, kind='MATERIAL_SHORT',
                                        material_id=7, arrival_date='2026-09-15')
        self.assertEqual(AutoTrigger.objects.count(), 0)

    def test_reported_issue_records_a_trigger(self):
        self.enable()
        with mock.patch('floor.lookup.lines', return_value=[{'line_id': 86, 'line_number': 1}]):
            floor_services.report_issue(self.manager, dataset_id=self.dataset.pk, factory_id=6, kind='LINE_STOPPED',
                                        line_id=86)
        self.assertEqual(list(AutoTrigger.objects.values_list('kind', flat=True)), ['ISSUE'])

    def test_setting_api(self):
        client = APIClient()
        client.force_authenticate(self.manager)
        url = f'/api/v1/planning/autoplan?dataset={self.dataset.pk}&factory=6'
        with override_settings(EMAIL_BACKEND='django.core.mail.backends.filebased.EmailBackend'):
            self.assertEqual(client.get(url).json(), {'enabled': False, 'email': 'OUTBOX'})
            self.assertEqual(client.post(url, {'enabled': True}, format='json').json(), {'enabled': True, 'email': 'OUTBOX'})
        with override_settings(EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend'):
            self.assertEqual(client.get(url).json()['email'], 'SMTP')
        viewer = User.objects.create_user('viewer')
        client.force_authenticate(viewer)
        self.assertEqual(client.post(url, {'enabled': False}, format='json').status_code, 403)

    def test_runs_show_their_trigger(self):
        run = PlanRun.objects.create(kind='SCHEDULE', dataset=self.dataset, factory_id=6, requested_by=self.manager,
                                     trigger={'source': 'AUTO', 'reason': 'Machine 12 down'})
        client = APIClient()
        client.force_authenticate(self.manager)
        body = client.get(f'/api/v1/planning/runs/{run.pk}').json()
        self.assertEqual(body['trigger'], {'source': 'AUTO', 'reason': 'Machine 12 down'})

    def test_urgent_order_with_priority_becomes_an_automatic_plan(self):
        self.enable()
        routing = [{'operation_id': 7, 'operation_name': 'Side seam', 'sequence': 1,
                    'machine_type': 'Overlock Serger (4-Thread)', 'sam': 1.5}]
        promise = services.request_promise(self.manager, parent_id=self.approved.pk, product_id=432, quantity=200,
                                           release='2026-09-12', due='2026-09-30', routing=routing, priority=True)
        AutoTrigger.objects.update(created_at=T0)
        self.assertEqual(autoplan.tick(T0 + timedelta(minutes=20)), [])  # waits for the promise run itself
        self.finish(promise)
        run = autoplan.tick(T0 + timedelta(minutes=20))[0]
        new_id = str(promise.scenario['new_order']['order_id'])
        self.assertEqual(run.order_ids[-1], new_id)
        self.assertIn(new_id, run.scenario['priority_orders'])
        self.assertIn(f'Urgent order {new_id}', run.trigger['reason'])

    def test_promise_without_priority_is_not_a_trigger(self):
        self.enable()
        routing = [{'operation_id': 7, 'operation_name': 'Side seam', 'sequence': 1,
                    'machine_type': 'Overlock Serger (4-Thread)', 'sam': 1.5}]
        services.request_promise(self.manager, parent_id=self.approved.pk, product_id=432, quantity=200,
                                 release='2026-09-12', due='2026-09-30', routing=routing)
        self.assertEqual(AutoTrigger.objects.count(), 0)



@override_settings(AUTO_PLAN_NEW_ORDER_CHECK_MINUTES=15, AUTO_CAPTURE_HOUR=2, AUTO_PLAN_QUIET_MINUTES=15,
                   AUTO_PLAN_MAX_WAIT_MINUTES=60)
class NewOrderTests(AutoPlanBase):
    """New orders in the factory database: one light check every 15 minutes, a capture, then a plan with them."""

    def check(self, now, live=('1', '2', '3'), captured=('1', '2')):
        with mock.patch.object(autoplan, '_live_order_ids', return_value=set(live)),                 mock.patch.object(autoplan, '_captured_orders', return_value=set(captured)):
            return autoplan.check_new_orders(now)

    def test_new_orders_request_one_capture_and_record_trigger(self):
        self.enable()
        self.assertEqual(self.check(T0), 1)
        capture = PlanRun.objects.get(kind='CAPTURE')
        self.assertEqual(capture.requested_by.username, 'craft_auto')
        self.assertTrue(capture.scenario['database'].startswith('garment_fyp_db'))
        trigger = AutoTrigger.objects.get(kind='NEW_ORDERS')
        self.assertEqual(trigger.ref, f'{capture.pk}:3')

    def test_no_new_orders_no_capture(self):
        self.enable()
        self.assertEqual(self.check(T0, live=('1', '2')), 0)
        self.assertFalse(PlanRun.objects.filter(kind='CAPTURE').exists())

    def test_check_runs_at_most_every_15_minutes(self):
        self.enable()
        self.check(T0, live=('1', '2'))
        with mock.patch.object(autoplan, '_live_order_ids') as looked:
            autoplan.check_new_orders(T0 + timedelta(minutes=10))
        looked.assert_not_called()
        self.assertEqual(self.check(T0 + timedelta(minutes=16)), 1)

    def test_never_captures_while_a_plan_runs(self):
        self.enable()
        PlanRun.objects.create(kind='SCHEDULE', dataset=self.dataset, factory_id=6, status='RUNNING',
                               requested_by=self.manager)
        self.assertEqual(self.check(T0), 0)
        self.assertFalse(PlanRun.objects.filter(kind='CAPTURE').exists())

    @override_settings(FACTORY_TIME_ZONE='UTC')
    def test_nightly_capture_once_per_day(self):
        self.enable()
        night = T0.replace(hour=1, minute=0)
        autoplan.nightly_capture(night)
        self.assertFalse(PlanRun.objects.filter(kind='CAPTURE').exists())  # before 02:00
        autoplan.nightly_capture(night.replace(hour=3))
        self.assertEqual(PlanRun.objects.filter(kind='CAPTURE').count(), 1)
        PlanRun.objects.filter(kind='CAPTURE').update(status='SUCCEEDED')
        autoplan.nightly_capture(night.replace(hour=5))
        self.assertEqual(PlanRun.objects.filter(kind='CAPTURE').count(), 1)  # once a day

    def test_finished_capture_queues_a_plan_with_the_new_orders(self):
        self.enable()
        self.check(T0)
        capture = PlanRun.objects.get(kind='CAPTURE')
        fresh = Dataset.objects.create(slug='capture_new', label='30% load', origin='LIVE_CAPTURE', path='y',
                                       database=self.dataset.database, reference_time='2026-09-11T00:00:00',
                                       factories=[{'factory_id': 6}])
        PlanRun.objects.filter(pk=capture.pk).update(status='SUCCEEDED', summary={'dataset': 'capture_new'})
        AutoTrigger.objects.update(created_at=T0)
        runs = autoplan.tick(T0 + timedelta(minutes=20))
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual((run.dataset_id, run.order_ids, run.mode), (fresh.pk, ['1', '2', '3'], 'QUICK'))
        self.assertEqual(run.trigger['source'], 'AUTO')
        self.assertIn('New order 3', run.trigger['reason'])
        self.assertTrue(AutoPlanSetting.objects.get(dataset=fresh, factory_id=6).enabled)


class ReviewFixTests(AutoPlanBase):
    """Fixes from the whole-branch review."""

    def check(self, now, live=('1', '2', '3'), captured=('1', '2')):
        with mock.patch.object(autoplan, '_live_order_ids', return_value=set(live)), \
                mock.patch.object(autoplan, '_captured_orders', return_value=set(captured)):
            return autoplan.check_new_orders(now)

    def new_order_plan(self):
        self.enable()
        self.check(T0)
        capture = PlanRun.objects.get(kind='CAPTURE')
        fresh = Dataset.objects.create(slug='capture_new', label='30% load', origin='LIVE_CAPTURE', path='y',
                                       database=self.dataset.database, reference_time='2026-09-11T00:00:00',
                                       factories=[{'factory_id': 6}])
        PlanRun.objects.filter(pk=capture.pk).update(status='SUCCEEDED', summary={'dataset': 'capture_new'})
        AutoTrigger.objects.update(created_at=T0)
        run = autoplan.tick(T0 + timedelta(minutes=20))[0]
        return fresh, run

    def test_new_order_capture_into_the_same_dataset_keeps_the_switch_on(self):
        self.enable()
        self.check(T0)
        capture = PlanRun.objects.get(kind='CAPTURE')
        PlanRun.objects.filter(pk=capture.pk).update(status='SUCCEEDED', summary={'dataset': self.dataset.slug})
        AutoTrigger.objects.update(created_at=T0)
        run = autoplan.tick(T0 + timedelta(minutes=20))[0]
        self.assertEqual((run.dataset_id, run.order_ids), (self.dataset.pk, ['1', '2', '3']))
        self.assertTrue(AutoPlanSetting.objects.get(dataset=self.dataset, factory_id=6).enabled)
        self.assertEqual(AutoPlanSetting.objects.count(), 1)

    def test_new_order_plan_moves_the_switch_so_the_check_does_not_repeat(self):
        fresh, run = self.new_order_plan()
        self.assertFalse(AutoPlanSetting.objects.get(dataset=self.dataset, factory_id=6).enabled)
        PlanRun.objects.filter(pk=run.pk).update(status='SUCCEEDED', decision='APPROVED')
        captured = lambda dataset, factory: {'1', '2', '3'} if dataset.pk == fresh.pk else {'1', '2'}
        with mock.patch.object(autoplan, '_live_order_ids', return_value={'1', '2', '3'}), \
                mock.patch.object(autoplan, '_captured_orders', side_effect=captured):
            self.assertEqual(autoplan.check_new_orders(T0 + timedelta(minutes=40)), 0)
        self.assertEqual(PlanRun.objects.filter(kind='CAPTURE').count(), 1)

    def test_failed_queue_keeps_the_waiting_plan(self):
        self.enable()
        self.machine_down(12, T0)
        first = self.finish(autoplan.tick(T0 + timedelta(minutes=20))[0])
        self.machine_down(13, T0 + timedelta(minutes=30))
        with mock.patch.object(autoplan, '_queue', side_effect=ValidationError('No machines left')):
            self.assertEqual(autoplan.tick(T0 + timedelta(minutes=50)), [])
        first.refresh_from_db()
        self.assertEqual(first.decision, 'PENDING')
        self.assertFalse(first.events.filter(action='PLAN_SUPERSEDED').exists())
        from .models import PlanEvent
        self.assertTrue(PlanEvent.objects.filter(action='AUTOPLAN_SKIPPED').exists())

    def test_watch_runs_tick_even_when_the_order_check_fails(self):
        with mock.patch.object(autoplan, 'check_new_orders', side_effect=RuntimeError('MySQL down')), \
                mock.patch.object(autoplan, 'nightly_capture') as nightly, \
                mock.patch.object(autoplan, 'tick', return_value=[]) as ticked:
            errors = autoplan.watch(T0)
        nightly.assert_called_once()
        ticked.assert_called_once()
        self.assertEqual(len(errors), 1)

    def test_one_failing_factory_does_not_stop_the_order_check_for_others(self):
        self.enable()
        other = Dataset.objects.create(slug='d60', label='60% load', origin='SNAPSHOT', path='z',
                                       database='garment_fyp_db_seed_v2_data2_load60_x',
                                       reference_time='2026-09-10T00:00:00', factories=[{'factory_id': 6}])
        AutoPlanSetting.objects.create(dataset=other, factory_id=6, enabled=True, set_by=self.manager)

        def live(dataset, factory):
            if dataset.pk == self.dataset.pk:
                raise RuntimeError('capture folder missing')
            return {'1', '2', '3'}
        with mock.patch.object(autoplan, '_live_order_ids', side_effect=live), \
                mock.patch.object(autoplan, '_captured_orders', return_value={'1', '2'}):
            self.assertEqual(autoplan.check_new_orders(T0), 1)
        self.assertEqual(AutoTrigger.objects.get(kind='NEW_ORDERS').dataset_id, other.pk)

    def test_repaired_machine_and_returned_worker_come_back_into_the_plan(self):
        self.enable()
        PlanRun.objects.filter(pk=self.approved.pk).update(scenario={'machines_down': [12, 77], 'workers_absent': [5]})
        Issue.objects.create(dataset=self.dataset, factory_id=6, kind='MACHINE_DOWN', machine_id=12, line_id=86,
                             reported_by=self.manager, status='RESOLVED')
        Issue.objects.create(dataset=self.dataset, factory_id=6, kind='WORKER_ABSENT', worker_id=5, line_id=86,
                             reported_by=self.manager, status='RESOLVED')
        self.machine_down(13, T0)
        run = autoplan.tick(T0 + timedelta(minutes=20))[0]
        # 12 was repaired and 5 came back; 77 was taken out by hand (no issue) and stays out
        self.assertEqual(run.scenario['machines_down'], [13, 77])
        self.assertEqual(run.scenario['workers_absent'], [])

    def test_machine_still_down_on_another_open_issue_stays_out(self):
        self.enable()
        PlanRun.objects.filter(pk=self.approved.pk).update(scenario={'machines_down': [12]})
        Issue.objects.create(dataset=self.dataset, factory_id=6, kind='MACHINE_DOWN', machine_id=12, line_id=86,
                             reported_by=self.manager, status='RESOLVED')
        Issue.objects.create(dataset=self.dataset, factory_id=6, kind='MACHINE_DOWN', machine_id=12, line_id=86,
                             reported_by=self.manager, status='OPEN')
        self.machine_down(13, T0)
        run = autoplan.tick(T0 + timedelta(minutes=20))[0]
        self.assertEqual(run.scenario['machines_down'], [12, 13])

    @override_settings(FACTORY_TIME_ZONE='Asia/Karachi', AUTO_CAPTURE_HOUR=2)
    def test_nightly_capture_uses_the_factory_clock(self):
        from datetime import datetime, timezone as dt_timezone
        self.enable()
        autoplan.nightly_capture(datetime(2026, 11, 16, 20, 30, tzinfo=dt_timezone.utc))  # 01:30 in the factory
        self.assertFalse(PlanRun.objects.filter(kind='CAPTURE').exists())
        autoplan.nightly_capture(datetime(2026, 11, 16, 21, 30, tzinfo=dt_timezone.utc))  # 02:30 in the factory
        self.assertEqual(PlanRun.objects.filter(kind='CAPTURE').count(), 1)
