"""Auto-planning makes new plans: a first plan for a factory without one, and the next plan when the approved
plan is nearly done. One heavy run at a time, smallest factory first, never while a plan waits for a manager."""
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from floor import plan as floor_plan
from floor.models import OutputEntry
from . import autoplan, jobs
from .models import AutoPlanSetting, Dataset, PlanRun

NOW = timezone.now().replace(microsecond=0)
BOOK = {'eligible': [{'order_id': str(i)} for i in (11, 12, 13, 14, 15, 16, 17, 18, 19, 20)]}


@override_settings(AUTO_PLAN_ORDERS=5, AUTO_PLAN_NEXT_AT=0.8)
class PlanAheadTests(TestCase):
    def setUp(self):
        floor_plan._CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset = Dataset.objects.create(slug='d', label='30% load', origin='SNAPSHOT', path='x', database='db',
                                              reference_time='2026-09-10T00:00:00',
                                              factories=[{'factory_id': 1, 'orders': 2120}, {'factory_id': 6, 'orders': 74}])
        self.manager = User.objects.create_user('manager')
        book = mock.patch.object(jobs, 'orderbook', return_value=BOOK)
        book.start()
        self.addCleanup(book.stop)

    def enable(self, factory_id):
        AutoPlanSetting.objects.create(dataset=self.dataset, factory_id=factory_id, enabled=True, set_by=self.manager)

    def approved(self, factory_id=6, orders=('11', '12')):
        out = Path(self.tmp.name) / f'plan{factory_id}'
        out.mkdir()
        gantt = [{'key': f'{o}_1', 'order_id': o, 'step': 1, 'line': 86, 'start': 0, 'end': 36000, 'machines': ['5'],
                  'workers': ['7'], 'pieces': 100, 'name': 's', 'machine_type': 'SNLS', 'crew': 1} for o in orders]
        (out / 'digest.json').write_text(json.dumps({'reference_time': '2026-09-10T00:00:00', 'gantt': {'CRAFT_V3': gantt},
                                                     'order_rows': [{'order_id': o} for o in orders]}))
        (out / 'craft_v3_schedule.json').write_text(json.dumps([{'operation_key': f'{o}_1', 'worker_segments': [[0, 36000]]}
                                                                for o in orders]))
        return PlanRun.objects.create(kind='SCHEDULE', dataset=self.dataset, factory_id=factory_id, status='SUCCEEDED',
                                      requested_by=self.manager, decision='APPROVED', decided_at=NOW, out_dir=str(out))

    def done(self, run, key, pieces):
        OutputEntry.objects.create(run=run, line_id=86, operation_key=key, slot=0, pieces=pieces, entered_by=self.manager)

    def test_first_plan_for_a_factory_without_one(self):
        self.enable(6)
        runs = autoplan.plan_ahead(NOW)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual((run.factory_id, run.orders, run.order_offset, run.mode), (6, 5, 0, 'QUICK'))
        self.assertEqual(run.requested_by.username, 'craft_auto')
        self.assertEqual((run.trigger['source'], run.trigger['kinds']), ('AUTO', ['FIRST_PLAN']))

    def test_nothing_while_a_plan_waits_or_runs(self):
        self.enable(6)
        PlanRun.objects.create(kind='SCHEDULE', dataset=self.dataset, factory_id=6, status='SUCCEEDED',
                               decision='PENDING', requested_by=self.manager)
        self.assertEqual(autoplan.plan_ahead(NOW), [])

    def test_one_at_a_time_smallest_factory_first(self):
        self.enable(1)
        self.enable(6)
        runs = autoplan.plan_ahead(NOW)
        self.assertEqual([r.factory_id for r in runs], [6])  # factory 6 (74 orders) before factory 1 (2,120)
        self.assertEqual(autoplan.plan_ahead(NOW + timedelta(minutes=1)), [])  # busy now

    def test_next_plan_when_the_approved_one_is_nearly_done(self):
        self.enable(6)
        run = self.approved()
        self.done(run, '11_1', 100)
        self.done(run, '12_1', 70)  # 170 of 200 pieces = 85%
        runs = autoplan.plan_ahead(NOW)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].order_ids, ['12', '13', '14', '15', '16', '17'])  # unfinished + the next 5
        self.assertEqual(runs[0].trigger['kinds'], ['NEXT_PLAN'])

    def test_no_next_plan_before_80_percent(self):
        self.enable(6)
        run = self.approved()
        self.done(run, '11_1', 100)  # 50%
        self.assertEqual(autoplan.plan_ahead(NOW), [])

    def test_switched_off_factory_is_left_alone(self):
        AutoPlanSetting.objects.create(dataset=self.dataset, factory_id=6, enabled=False, set_by=self.manager)
        self.assertEqual(autoplan.plan_ahead(NOW), [])

    def test_a_rejected_first_plan_is_not_retried_for_a_day(self):
        self.enable(6)
        PlanRun.objects.create(kind='SCHEDULE', dataset=self.dataset, factory_id=6, status='SUCCEEDED',
                               decision='REJECTED', decided_at=NOW, requested_by=self.manager,
                               trigger={'source': 'AUTO', 'kinds': ['FIRST_PLAN']})
        self.assertEqual(autoplan.plan_ahead(NOW + timedelta(hours=2)), [])
        self.assertEqual(len(autoplan.plan_ahead(NOW + timedelta(hours=25))), 1)

    def test_all_factories_switch(self):
        from rest_framework.test import APIClient
        from django.contrib.auth.models import Permission
        self.manager.user_permissions.add(Permission.objects.get(codename='request_plan', content_type__app_label='planning'))
        client = APIClient()
        client.force_authenticate(User.objects.get(pk=self.manager.pk))
        body = client.post(f'/api/v1/planning/autoplan/all?dataset={self.dataset.pk}', {'enabled': True}, format='json').json()
        self.assertEqual(body['factories'], [1, 6])
        self.assertEqual(AutoPlanSetting.objects.filter(dataset=self.dataset, enabled=True).count(), 2)
