"""Emails to the managers when an automatic CRAFT plan is ready or failed."""
import json
import smtplib
import tempfile
from pathlib import Path
from unittest import mock
from django.contrib.auth.models import Permission, User
from django.core import mail
from django.test import TestCase, override_settings
from floor.models import Profile
from .models import Dataset, PlanEvent, PlanRun
from . import notify

LOCMEM = 'django.core.mail.backends.locmem.EmailBackend'


def _manager(name, email, factory_id=None, perm=True):
    user = User.objects.create_user(name, email=email)
    if perm:
        user.user_permissions.add(Permission.objects.get(codename='request_plan'))
    Profile.objects.create(user=user, role='production_manager', factory_id=factory_id)
    return user


def _digest(out, finishes):
    out.mkdir(parents=True)
    rows = [{'order_id': oid, 'due_day': 10.0, 'status': 'Late' if f > 10 else 'On time',
             'methods': {'CRAFT_V3': {'finish_day': f}}} for oid, f in finishes.items()]
    (out / 'digest.json').write_text(json.dumps({'order_rows': rows}))


@override_settings(EMAIL_BACKEND=LOCMEM, APP_BASE_URL='http://craft.test', DEFAULT_FROM_EMAIL='craft@test')
class NotifyTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dataset = Dataset.objects.create(slug='d', label='30% load', origin='SNAPSHOT', path='x',
                                              database='garment_fyp_db_seed_v2_data2_load30_x',
                                              reference_time='2026-09-10T00:00:00', factories=[{'factory_id': 6}])
        self.requester = User.objects.create_user('craft_auto')
        parent_out, out = Path(self.tmp.name) / 'p', Path(self.tmp.name) / 'r'
        _digest(parent_out, {'1': 9.0, '2': 12.0})
        _digest(out, {'1': 11.0, '2': 12.0})
        self.parent = PlanRun.objects.create(kind='SCHEDULE', dataset=self.dataset, factory_id=6, status='SUCCEEDED',
                                             requested_by=self.requester, decision='APPROVED', out_dir=str(parent_out),
                                             summary={'craft_v3': {'late': 1, 'makespan_days': 12.0}})
        self.run = PlanRun.objects.create(kind='SCHEDULE', purpose='DISRUPTION', parent=self.parent, dataset=self.dataset,
                                          factory_id=6, status='SUCCEEDED', requested_by=self.requester,
                                          out_dir=str(out), summary={'craft_v3': {'late': 2, 'makespan_days': 12.0}},
                                          scenario={'machines_down': [12]},
                                          trigger={'source': 'AUTO', 'reason': 'Machine 12 down'})

    def tearDown(self):
        self.tmp.cleanup()

    def test_ready_email_goes_to_managers_of_that_factory(self):
        _manager('ali', 'ali@test', factory_id=6)
        _manager('sara', 'sara@test')  # all factories
        _manager('omar', 'omar@test', factory_id=2)  # another factory
        _manager('nina', 'nina@test', factory_id=6, perm=False)  # cannot approve
        _manager('zed', '', factory_id=6)  # no email address
        self.assertEqual(notify.run_finished(self.run), 'SENT')
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(sorted(message.to), ['ali@test', 'sara@test'])
        self.assertTrue(message.subject.startswith('CRAFT has a new plan for Factory 6'))
        self.assertIn('Machine 12 down', message.subject)
        self.assertIn('http://craft.test/runs/' + str(self.run.pk), message.body)
        self.assertIn('Late orders: 1 → 2', message.body)
        self.assertIn('1 order finishes later', message.body)
        self.assertIn('as if it stays that way for the whole plan', message.body)
        self.assertNotIn('approve?', message.body.lower())
        event = PlanEvent.objects.get(run=self.run, action='EMAIL_SENT')
        self.assertNotIn('ali@test', event.message)

    def test_times_in_the_email_are_on_the_factory_clock(self):
        from datetime import datetime, timezone as dt_timezone
        _manager('ali', 'ali@test', factory_id=6)
        PlanRun.objects.filter(pk=self.run.pk).update(created_at=datetime(2026, 11, 16, 21, 30, tzinfo=dt_timezone.utc),
                                                      finished_at=None)
        self.run.refresh_from_db()
        with self.settings(FACTORY_TIME_ZONE='Asia/Karachi'):
            notify.run_finished(self.run)
        self.assertIn('Requested 17 Nov 02:30', mail.outbox[0].body)

    def test_failed_run_sends_failure_email(self):
        _manager('ali', 'ali@test', factory_id=6)
        PlanRun.objects.filter(pk=self.run.pk).update(status='FAILED', error='CRAFT exited with code 1')
        self.run.refresh_from_db()
        self.assertEqual(notify.run_finished(self.run), 'SENT')
        self.assertEqual(mail.outbox[0].subject, 'CRAFT could not make the plan for Factory 6')

    def test_no_recipients_records_event(self):
        _manager('zed', '', factory_id=6)
        self.assertEqual(notify.run_finished(self.run), 'NO_RECIPIENTS')
        self.assertEqual(mail.outbox, [])
        self.assertEqual(PlanEvent.objects.get(run=self.run, action='EMAIL_NO_RECIPIENTS').message,
                         'No manager has an email address, so nobody was told')

    def test_send_error_records_event_and_does_not_raise(self):
        _manager('ali', 'ali@test', factory_id=6)
        with mock.patch('planning.notify.EmailMultiAlternatives.send', side_effect=smtplib.SMTPException('down')):
            self.assertEqual(notify.run_finished(self.run), 'FAILED')
        self.assertTrue(PlanEvent.objects.filter(run=self.run, action='EMAIL_FAILED').exists())

    def test_manual_runs_send_nothing(self):
        _manager('ali', 'ali@test', factory_id=6)
        PlanRun.objects.filter(pk=self.run.pk).update(trigger={'source': 'MANUAL'})
        self.run.refresh_from_db()
        self.assertEqual(notify.run_finished(self.run), 'SKIPPED')
        self.assertEqual(mail.outbox, [])


class WorkerEmailTests(TestCase):
    """The worker emails after an automatic run finishes, and never for a manual one."""

    def _queued(self, source):
        from .tests import _capture
        from .datasets import discover
        self.tmp = tempfile.TemporaryDirectory()
        _capture(self.tmp.name, 'capture_20261018T084010774436Z')
        with override_settings(PLANNING_CAPTURE_ROOTS=[self.tmp.name]):
            dataset = discover()[0]
        user = User.objects.create_user('craft_auto')
        return PlanRun.objects.create(kind='SCHEDULE', dataset=dataset, factory_id=4, orders=1, mode='QUICK',
                                      requested_by=user, trigger={'source': source, 'reason': 'Machine 5 down'})

    def _work(self, run):
        import sys
        from django.core.management import call_command
        from . import jobs
        from .tests import FAKE_RUNNER
        fake = Path(self.tmp.name) / 'fake_runner.py'
        fake.write_text(FAKE_RUNNER)
        with override_settings(PLANNING_SCHEDULER_PYTHON=sys.executable, EMAIL_BACKEND=LOCMEM,
                               PLANNING_RUNS_DIR=str(Path(self.tmp.name) / 'runs')), \
                mock.patch.object(jobs, 'schedule_command', lambda r, out: [sys.executable, str(fake), '4', str(out)]), \
                mock.patch.object(jobs, 'digest', return_value={'methods': [], 'craft_v3': {}}), \
                mock.patch('planning.management.commands.planning_worker.POLL_SECONDS', 0.05):
            call_command('planning_worker', '--once')
        self.tmp.cleanup()

    def test_auto_run_is_emailed_after_it_finishes(self):
        _manager('ali', 'ali@test', factory_id=4)
        run = self._queued('AUTO')
        self._work(run)
        self.assertEqual(PlanRun.objects.get(pk=run.pk).status, 'SUCCEEDED')
        self.assertEqual([m.to for m in mail.outbox], [['ali@test']])

    def test_manual_run_is_not_emailed(self):
        _manager('ali', 'ali@test', factory_id=4)
        self._work(self._queued('MANUAL'))
        self.assertEqual(mail.outbox, [])
