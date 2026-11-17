"""The factory clock moves at its speed, writes the engine's events as craft_sim through the normal records,
follows the latest approved plan and never floods the database after a long gap."""
import json
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest import mock
from django.contrib.auth.models import User
from django.test import TestCase
from floor import lookup, plan as floor_plan
from floor.models import DefectCheck, Issue, OutputEntry
from planning.models import Dataset, PlanRun
from . import runner
from .engine import Rates
from .models import FactoryClock

REF = datetime(2026, 9, 10, tzinfo=dt_timezone.utc)
T = datetime(2026, 11, 17, 12, 0, tzinfo=dt_timezone.utc)  # a real moment
QUIET = Rates(breakdown_per_hour=0.0, absence_per_day=0.0)
OPS = [('1_10', 1, ['5'], ['7'], [[0, 36000]], 100), ('1_11', 2, ['6'], ['8'], [[36000, 54000]], 50)]


class RunnerBase(TestCase):
    def setUp(self):
        floor_plan._CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset = Dataset.objects.create(slug='d', label='30% load', origin='SNAPSHOT', path='x', database='db',
                                              reference_time='2026-09-10T00:00:00', factories=[{'factory_id': 6}])
        self.manager = User.objects.create_user('manager')
        quiet = mock.patch.object(runner, 'RATES', QUIET)
        quiet.start()
        self.addCleanup(quiet.stop)

    def approve(self, name, decided):
        out = Path(self.tmp.name) / name
        out.mkdir()
        gantt = [{'key': k, 'order_id': '1', 'step': s, 'line': 86, 'start': seg[0][0], 'end': seg[-1][1], 'machines': m,
                  'workers': w, 'pieces': p, 'name': f'step {s}', 'machine_type': 'SNLS', 'crew': 1}
                 for k, s, m, w, seg, p in OPS]
        (out / 'digest.json').write_text(json.dumps({'reference_time': '2026-09-10T00:00:00',
                                                     'gantt': {'CRAFT_V3': gantt}, 'order_rows': []}))
        (out / 'craft_v3_schedule.json').write_text(json.dumps(
            [{'operation_key': k, 'worker_segments': seg} for k, _s, _m, _w, seg, _p in OPS]))
        return PlanRun.objects.create(kind='SCHEDULE', dataset=self.dataset, factory_id=6, status='SUCCEEDED',
                                      requested_by=self.manager, decision='APPROVED', decided_at=decided, out_dir=str(out))

    def clock(self, speed=60, last_tick=T):
        return FactoryClock.objects.create(dataset=self.dataset, factory_id=6, now=REF, speed=speed, last_tick=last_tick)


class RunnerTests(RunnerBase):
    def test_tick_advances_by_speed(self):
        self.approve('a', T)
        clock = self.clock(speed=1)
        runner.tick(T + timedelta(minutes=3))  # 1x: one factory hour per real minute
        clock.refresh_from_db()
        self.assertEqual(clock.now, REF + timedelta(hours=3))

    def test_paused_clock_does_not_move(self):
        clock = self.clock(speed=0)
        runner.tick(T + timedelta(minutes=10))
        clock.refresh_from_db()
        self.assertEqual(clock.now, REF)

    def test_tick_caps_a_long_gap(self):
        self.approve('a', T)
        clock = self.clock(speed=60)
        runner.tick(T + timedelta(hours=1))  # 3,600 factory hours would be due
        clock.refresh_from_db()
        self.assertEqual(clock.now, REF + timedelta(hours=24))

    def test_tick_writes_output_and_checks_as_craft_sim(self):
        run = self.approve('a', T)
        self.clock(speed=60)
        runner.tick(T + timedelta(seconds=24))
        entries = OutputEntry.objects.filter(run=run)
        self.assertTrue(entries.exists())
        self.assertEqual(set(entries.values_list('entered_by__username', flat=True)), {'craft_sim'})
        self.assertTrue(DefectCheck.objects.filter(run=run, day=0, entered_by__username='craft_sim').exists())

    def test_no_plan_no_records(self):
        clock = self.clock(speed=60)
        runner.tick(T + timedelta(seconds=5))
        clock.refresh_from_db()
        self.assertEqual(clock.now, REF + timedelta(hours=5))
        self.assertFalse(OutputEntry.objects.exists())

    def test_follows_new_plan_without_double_output(self):
        first = self.approve('a', T)
        clock = self.clock(speed=60)
        runner.tick(T + timedelta(seconds=5))
        second = self.approve('b', T + timedelta(minutes=1))  # a re-plan approved mid-way
        clock.refresh_from_db()
        runner.tick(clock.last_tick + timedelta(seconds=24))
        self.assertTrue(OutputEntry.objects.filter(run=first).exists())
        self.assertTrue(OutputEntry.objects.filter(run=second).exists())
        for key, *_rest, pieces in OPS:
            done = sum(OutputEntry.objects.filter(operation_key=key).values_list('pieces', flat=True))
            self.assertLessEqual(done, pieces, key)

    def test_breakdown_opens_and_repair_closes_an_issue(self):
        self.approve('a', T)
        self.clock(speed=60)
        found = {'asset_id': 5, 'factory_id': 6, 'line_id': 86, 'machine_model': 'Juki', 'current_status': 'Active'}
        with mock.patch.object(runner, 'RATES', Rates(breakdown_per_hour=1.0, absence_per_day=0.0, repair_hours=(2, 2))), \
                mock.patch.object(lookup, 'machine', side_effect=lambda d, m: {**found, 'asset_id': m}):
            runner.tick(T + timedelta(seconds=24))
        issues = Issue.objects.filter(kind='MACHINE_DOWN', reported_by__username='craft_sim')
        self.assertTrue(issues.filter(status='RESOLVED').exists())
        self.assertTrue(issues.filter(machine_id=5).exists())
