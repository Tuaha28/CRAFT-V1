"""Shop-floor tests on a small hand-made approved plan (factory database lookups mocked)."""
import json
import tempfile
from pathlib import Path
from unittest import mock
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from planning.datasets import discover
from planning.models import PlanRun
from planning.tests import _capture
from . import services
from .models import DefectCheck, Issue, OutputEntry
from .plan import approved_plan, load
from .roles import ROLES, ensure_groups

H = 3600
# Order 1: step 10 on line 44, 100 pieces over 8 productive hours on day 0
# (08-12 and 13-17); step 11 on line 44, 60 pieces 08-11 on day 1.
# Order 2: step 10 on line 102, 40 pieces 09-13 on day 0.
OPS = [('1_10', '1', 10, 44, 100, [[8 * H, 12 * H], [13 * H, 17 * H]], ['501', '502'], ['901', '902']),
       ('1_11', '1', 11, 44, 60, [[86400 + 8 * H, 86400 + 11 * H]], ['503'], ['903']),
       ('2_10', '2', 10, 102, 40, [[9 * H, 13 * H]], ['601'], ['951'])]
LINES = [{'line_id': 44, 'line_number': 1}, {'line_id': 102, 'line_number': 2}]
MACHINES = {901: {'asset_id': 901, 'factory_id': 4, 'line_id': 44, 'machine_model': 'Juki DDL', 'current_status': 'Active'},
            951: {'asset_id': 951, 'factory_id': 4, 'line_id': 102, 'machine_model': 'Pegasus', 'current_status': 'Active'},
            777: {'asset_id': 777, 'factory_id': 5, 'line_id': 7, 'machine_model': 'Other', 'current_status': 'Active'}}
WORKERS = {501: {'employee_id': 501, 'factory_id': 4, 'line_id': 44, 'name': 'Ayesha Khan'},
           601: {'employee_id': 601, 'factory_id': 4, 'line_id': 102, 'name': 'Bilal Ahmed'}}


def _plan_files(out):
    out.mkdir(parents=True)
    gantt = [{'key': k, 'order_id': o, 'step': s, 'name': f'Op {k}', 'machine_type': 'Lockstitch', 'line': ln,
              'start': segs[0][0] - 900, 'end': segs[-1][1], 'crew': len(w), 'pieces': p, 'machines': m, 'workers': w}
             for k, o, s, ln, p, segs, w, m in OPS]
    (out / 'digest.json').write_text(json.dumps({
        'reference_time': '2026-09-10T00:00:00', 'gantt': {'CRAFT_V3': gantt, 'EDD_CREW': []},
        'order_rows': [{'order_id': '1', 'due_day': 3.0}, {'order_id': '2', 'due_day': 1.0}]}))
    (out / 'craft_v3_schedule.json').write_text(json.dumps(
        [{'operation_key': k, 'worker_segments': segs} for k, _o, _s, _ln, _p, segs, _w, _m in OPS]))


class Fixture(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _capture(self.tmp.name, 'capture_20261018T084010774436Z')
        self.override = override_settings(PLANNING_CAPTURE_ROOTS=[self.tmp.name],
                                          PLANNING_RUNS_DIR=str(Path(self.tmp.name) / 'runs'))
        self.override.enable()
        self.dataset = discover()[0]
        self.patches = [mock.patch('floor.lookup.lines', return_value=LINES),
                        mock.patch('floor.lookup.machine', side_effect=lambda d, m: MACHINES.get(int(m))),
                        mock.patch('floor.lookup.worker', side_effect=lambda d, w: WORKERS.get(int(w))),
                        mock.patch('floor.lookup.names', return_value=({'501': 'Ayesha Khan'}, {'901': 'Juki DDL'}))]
        for p in self.patches:
            p.start()
        groups = ensure_groups()
        self.users = {}
        for key, line in (('admin', None), ('manager', None), ('planner', None), ('supervisor', 44),
                          ('qc', None), ('maintenance', None), ('merchandiser', None)):
            user = User.objects.create_user(f'u_{key}')
            services._assign(user, key, 4 if line else None, line, groups)
            self.users[key] = User.objects.get(pk=user.pk)
        self.run = self.approved()

    def approved(self):
        run = PlanRun.objects.create(kind='SCHEDULE', dataset=self.dataset, factory_id=4, orders=2, status='SUCCEEDED',
                                     requested_by=self.users['planner'], decision='APPROVED',
                                     decided_by=self.users['manager'], decided_at=timezone.now())
        out = Path(self.tmp.name) / 'runs' / str(run.pk)
        _plan_files(out)
        run.out_dir = str(out)
        run.save()
        return run

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.override.disable()
        self.tmp.cleanup()

    def out(self, user='supervisor', **overrides):
        values = {'dataset_id': self.dataset.pk, 'factory_id': 4, 'line_id': 44, 'day': 0, 'hour': 9,
                  'operation_key': '1_10', 'pieces': 12}
        return services.save_output(self.users[user], **{**values, **overrides})

    def issue(self, user='supervisor', **overrides):
        values = {'dataset_id': self.dataset.pk, 'factory_id': 4, 'kind': 'MACHINE_DOWN', 'machine_id': 901}
        return services.report_issue(self.users[user], **{**values, **overrides})


class PlanTests(Fixture):
    def test_hourly_targets_follow_productive_segments(self):
        plan = load(self.run)
        targets = plan.hourly_targets(44, 0)
        self.assertEqual(sorted(targets['1_10']), [8, 9, 10, 11, 13, 14, 15, 16])  # lunch hour and nights carry nothing
        self.assertAlmostEqual(sum(targets['1_10'].values()), 100)
        self.assertAlmostEqual(targets['1_10'][9], 12.5)
        self.assertNotIn('1_11', targets)  # step 11 works on day 1

    def test_day_view_lists_only_that_line_and_day(self):
        plan = load(self.run)
        day0 = plan.day_ops(44, 0)
        self.assertEqual([o['key'] for o in day0], ['1_10'])
        self.assertEqual(day0[0]['windows'], [['08:00', '12:00'], ['13:00', '17:00']])
        self.assertEqual((day0[0]['planned_today'], day0[0]['planned_before']), (100, 0))
        self.assertEqual(plan.work_days(44), [0, 1])
        self.assertEqual(plan.date_of(1).isoformat(), '2026-09-11')

    def test_only_an_approved_plan_reaches_the_floor(self):
        self.run.decision = 'PENDING'
        self.run.save()
        self.assertIsNone(approved_plan(self.dataset.pk, 4))
        with self.assertRaises(ValidationError):
            self.out()


class RoleTests(Fixture):
    def test_groups_hold_exactly_the_role_permissions(self):
        self.assertEqual(Group.objects.filter(name__in=[n for n, _ in ROLES.values()]).count(), len(ROLES))
        supervisor = self.users['supervisor']
        self.assertTrue(supervisor.has_perm('floor.enter_output'))
        self.assertFalse(supervisor.has_perm('planning.request_plan'))
        self.assertFalse(self.users['merchandiser'].has_perm('floor.report_issue'))
        self.assertTrue(self.users['admin'].has_perm('floor.manage_users'))

    def test_admin_runs_the_system_but_does_not_approve_or_do_floor_work(self):
        admin = self.users['admin']
        for perm in ('floor.manage_users', 'planning.request_plan', 'floor.view_floor'):
            self.assertTrue(admin.has_perm(perm), perm)
        for perm in ('planning.approve_plan', 'floor.enter_output', 'floor.record_defect', 'floor.repair_machine',
                     'floor.report_issue', 'floor.handle_issue'):
            self.assertFalse(admin.has_perm(perm), perm)

    def test_session_reports_role_scope_and_permissions(self):
        client = APIClient()
        client.force_authenticate(self.users['supervisor'])
        body = client.get('/api/v1/planning/session').json()
        self.assertEqual((body['role'], body['scope_line'], body['scope_factory']), ('supervisor', 44, 4))
        self.assertIn('floor.enter_output', body['perms'])
        self.assertFalse(body['can_request'])


class OutputTests(Fixture):
    def test_supervisor_records_and_corrects_an_hour(self):
        self.out(pieces=12)
        self.out(pieces=11)  # same hour again corrects, it does not add
        entry = OutputEntry.objects.get()
        self.assertEqual((entry.slot, entry.pieces), (9, 11))

    def test_scope_permission_and_limits(self):
        with self.assertRaises(PermissionDenied):
            self.out(line_id=102, operation_key='2_10')  # another line
        with self.assertRaises(PermissionDenied):
            self.out(user='planner')  # planners do not enter output
        with self.assertRaises(ValidationError):
            self.out(operation_key='1_11')  # planned on day 1, not day 0
        self.out(hour=9, pieces=90)
        with self.assertRaises(ValidationError):
            self.out(hour=10, pieces=11)  # 101 of 100 pieces

    def test_board_and_line_view(self):
        self.out(hour=8, pieces=10)
        client = APIClient()
        client.force_authenticate(self.users['manager'])
        board = client.get(f'/api/v1/floor/{self.dataset.pk}/4/board?day=0').json()
        line = next(r for r in board['lines'] if r['line_id'] == 44)
        self.assertEqual((line['target_day'], line['actual_day'], line['line_number']), (100, 10, 1))
        self.assertEqual(len(board['lines']), 2)
        client.force_authenticate(self.users['supervisor'])
        board = client.get(f'/api/v1/floor/{self.dataset.pk}/4/board?day=0').json()
        self.assertEqual([r['line_id'] for r in board['lines']], [44])  # a supervisor sees their line
        self.assertEqual(client.get(f'/api/v1/floor/{self.dataset.pk}/4/lines/102?day=0').status_code, 403)
        view = client.get(f'/api/v1/floor/{self.dataset.pk}/4/lines/44?day=0').json()
        self.assertEqual(view['operations'][0]['worker_names'][0], 'Ayesha Khan')
        self.assertEqual(view['operations'][0]['recorded_total'], 10)
        self.assertEqual(client.get(f'/api/v1/floor/{self.dataset.pk}/4/lines/44?day=x').status_code, 409)


class IssueTests(Fixture):
    def test_report_take_and_resolve_a_breakdown(self):
        issue = self.issue()
        self.assertEqual((issue.line_id, issue.status), (44, 'OPEN'))
        with self.assertRaises(ValidationError):
            self.issue()  # already reported
        with self.assertRaises(PermissionDenied):
            services.move_issue(self.users['supervisor'], issue.pk, 'RESOLVED', 'fixed')
        services.move_issue(self.users['maintenance'], issue.pk, 'IN_PROGRESS')
        with self.assertRaises(ValidationError):
            services.move_issue(self.users['maintenance'], issue.pk, 'RESOLVED', '')  # needs a resolution
        services.move_issue(self.users['maintenance'], issue.pk, 'RESOLVED', 'Belt replaced')
        issue.refresh_from_db()
        self.assertEqual((issue.status, issue.handled_by.username), ('RESOLVED', 'u_maintenance'))
        with self.assertRaises(ValidationError):
            services.move_issue(self.users['manager'], issue.pk, 'IN_PROGRESS')  # resolved is final

    def test_scope_and_ownership_checks(self):
        with self.assertRaises(PermissionDenied):
            self.issue(machine_id=951)  # line 102 machine, supervisor of line 44
        with self.assertRaises(ValidationError):
            self.issue(user='manager', machine_id=777)  # another factory
        with self.assertRaises(PermissionDenied):
            self.issue(user='merchandiser')
        absent = self.issue(user='manager', kind='WORKER_ABSENT', worker_id=601, machine_id=None)
        self.assertEqual((absent.line_id, absent.machine_id), (102, None))
        with self.assertRaises(PermissionDenied):
            services.move_issue(self.users['maintenance'], absent.pk, 'IN_PROGRESS')  # not a machine issue
        with self.assertRaises(ValidationError):
            self.issue(kind='MATERIAL_SHORT', machine_id=None)  # needs the order

    def test_replan_after_issues_requests_a_disruption_run(self):
        down = self.issue()
        absent = self.issue(user='manager', kind='WORKER_ABSENT', worker_id=501, machine_id=None)
        with self.assertRaises(PermissionDenied):
            services.replan_after(self.users['supervisor'], dataset_id=self.dataset.pk, factory_id=4,
                                  issue_ids=[down.pk])
        run = services.replan_after(self.users['planner'], dataset_id=self.dataset.pk, factory_id=4,
                                    issue_ids=[down.pk, absent.pk])
        self.assertEqual((run.purpose, run.parent_id, run.status), ('DISRUPTION', self.run.pk, 'QUEUED'))
        self.assertEqual((run.scenario['machines_down'], run.scenario['workers_absent']), ([901], [501]))
        self.assertEqual(run.order_ids, ['1', '2'])
        self.assertEqual(set(Issue.objects.values_list('replan_id', flat=True)), {run.pk})


class NewDisruptionTests(Fixture):
    """Line stopped and late material: reported on the floor, re-planned around like machines and workers."""

    def test_line_stopped_needs_a_line_of_this_factory(self):
        with self.assertRaises(ValidationError):
            self.issue(user='manager', kind='LINE_STOPPED', machine_id=None)
        with self.assertRaises(ValidationError):
            self.issue(user='manager', kind='LINE_STOPPED', machine_id=None, line_id=999)
        stopped = self.issue(user='manager', kind='LINE_STOPPED', machine_id=None, line_id=44)
        self.assertEqual((stopped.kind, stopped.line_id), ('LINE_STOPPED', 44))

    def test_material_short_needs_material_and_arrival(self):
        with mock.patch('floor.lookup.orders_using_material', return_value=['1']):
            with self.assertRaises(ValidationError):
                self.issue(user='manager', kind='MATERIAL_SHORT', machine_id=None, material_id=7)
            with self.assertRaises(ValidationError):
                self.issue(user='manager', kind='MATERIAL_SHORT', machine_id=None, arrival_date='2026-09-15')
            short = self.issue(user='manager', kind='MATERIAL_SHORT', machine_id=None, material_id=7,
                               arrival_date='2026-09-15')
        self.assertEqual((short.material_id, str(short.arrival_date)), (7, '2026-09-15'))

    def test_replan_after_accepts_all_four_kinds(self):
        down = self.issue()
        absent = self.issue(user='manager', kind='WORKER_ABSENT', worker_id=501, machine_id=None)
        stopped = self.issue(user='manager', kind='LINE_STOPPED', machine_id=None, line_id=102)
        with mock.patch('floor.lookup.orders_using_material', return_value=['1']) as used:
            short = self.issue(user='manager', kind='MATERIAL_SHORT', machine_id=None, material_id=7,
                               arrival_date='2026-09-15')
            run = services.replan_after(self.users['planner'], dataset_id=self.dataset.pk, factory_id=4,
                                        issue_ids=[down.pk, absent.pk, stopped.pk, short.pk])
        self.assertEqual(used.call_args.args[1:], (7, ['1', '2']))
        self.assertEqual(run.scenario['machines_down'], [901])
        self.assertEqual(run.scenario['workers_absent'], [501])
        self.assertEqual(run.scenario['lines_stopped'], [102])
        self.assertEqual(run.scenario['release_after'], {'1': '2026-09-15'})
        self.assertEqual(run.trigger['source'], 'MANUAL')
        self.assertEqual(sorted(run.trigger['issue_ids']), sorted([down.pk, absent.pk, stopped.pk, short.pk]))

    def test_machine_report_and_resolve_flip_status_in_the_demo_copy(self):
        with mock.patch('erp.writer.set_machine_status', return_value=True) as flip:
            with self.captureOnCommitCallbacks(execute=False) as pending:
                down = self.issue(user='manager')
            flip.assert_not_called()  # nothing touches the demo copy until the report is saved
            for callback in pending:
                callback()
            with self.captureOnCommitCallbacks(execute=True):
                services.move_issue(self.users['manager'], down.pk, 'RESOLVED', 'Belt replaced')
        self.assertEqual([c.args[1:] for c in flip.call_args_list], [(901, 'Breakdown'), (901, 'Active')])

    def test_quality_issues_still_do_not_change_the_plan(self):
        quality = self.issue(user='manager', kind='QUALITY', machine_id=None, note='Open seams')
        with self.assertRaises(ValidationError):
            services.replan_after(self.users['planner'], dataset_id=self.dataset.pk, factory_id=4,
                                  issue_ids=[quality.pk])

    def test_material_used_by_no_planned_order_warns(self):
        client = APIClient()
        client.force_authenticate(self.users['manager'])
        with mock.patch('floor.lookup.orders_using_material', return_value=[]):
            response = client.post(f'/api/v1/floor/{self.dataset.pk}/4/issues',
                                   {'kind': 'MATERIAL_SHORT', 'material_id': 7, 'arrival_date': '2026-09-15'},
                                   format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['warning'], 'No order in the current plan uses this material')


class QualityTests(Fixture):
    def test_check_records_dhu_and_needs_a_type(self):
        services.record_check(self.users['qc'], dataset_id=self.dataset.pk, factory_id=4, line_id=44, day=0,
                              operation_key='1_10', checked=50, defects=3, defect_type='Open seam')
        with self.assertRaises(ValidationError):
            services.record_check(self.users['qc'], dataset_id=self.dataset.pk, factory_id=4, line_id=44, day=0,
                                  operation_key='1_10', checked=50, defects=2)
        with self.assertRaises(PermissionDenied):
            services.record_check(self.users['supervisor'], dataset_id=self.dataset.pk, factory_id=4, line_id=44,
                                  day=0, operation_key='1_10', checked=5, defects=0)
        client = APIClient()
        client.force_authenticate(self.users['manager'])
        body = client.get(f'/api/v1/floor/{self.dataset.pk}/4/checks').json()
        self.assertEqual(body['by_line'], [{'line_id': 44, 'checked': 50, 'defects': 3, 'dhu': 6.0}])
        self.assertEqual(DefectCheck.objects.count(), 1)


class UserTests(Fixture):
    def test_admin_sets_a_manager_email_for_plan_emails(self):
        client = APIClient()
        client.force_authenticate(self.users['admin'])
        made = client.post('/api/v1/floor/users', {'username': 'pm2', 'role': 'manager', 'factory_id': 4,
                                                   'email': 'pm2@factory.test'}, format='json')
        self.assertEqual(made.status_code, 201)
        self.assertEqual(made.json()['email'], 'pm2@factory.test')
        uid = made.json()['id']
        changed = client.post(f'/api/v1/floor/users/{uid}', {'role': 'manager', 'factory_id': 4,
                                                             'active': True, 'email': 'new@factory.test'}, format='json')
        self.assertEqual(changed.json()['email'], 'new@factory.test')
        kept = client.post(f'/api/v1/floor/users/{uid}', {'role': 'manager', 'factory_id': 4, 'active': True},
                           format='json')
        self.assertEqual(kept.json()['email'], 'new@factory.test')
        bad = client.post('/api/v1/floor/users', {'username': 'pm3', 'role': 'manager', 'email': 'nope'},
                          format='json')
        self.assertEqual(bad.status_code, 400)

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.users['admin'])

    def test_admin_creates_supervisor_who_can_sign_in(self):
        base = {'username': 'line44_sup', 'role': 'supervisor', 'factory_id': 4, 'dataset_id': self.dataset.pk}
        self.assertEqual(self.client.post('/api/v1/floor/users', base, format='json').status_code, 409)  # no line
        self.assertEqual(self.client.post('/api/v1/floor/users', {**base, 'line_id': 55}, format='json').status_code,
                         409)  # not a line of factory 4
        response = self.client.post('/api/v1/floor/users', {**base, 'line_id': 44}, format='json')
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(APIClient().login(username='line44_sup', password=body['password']))
        user = User.objects.get(username='line44_sup')
        self.assertTrue(user.has_perm('floor.enter_output'))
        self.assertEqual(self.client.post('/api/v1/floor/users', {**base, 'line_id': 44}, format='json').status_code,
                         409)  # name taken

    def test_only_admin_and_no_self_lockout(self):
        other = APIClient()
        other.force_authenticate(self.users['manager'])
        self.assertEqual(other.get('/api/v1/floor/users').status_code, 403)
        me = self.users['admin'].pk
        self.assertEqual(self.client.post(f'/api/v1/floor/users/{me}', {'role': 'planner'}, format='json').status_code,
                         409)
        target = self.users['qc'].pk
        changed = self.client.post(f'/api/v1/floor/users/{target}', {'role': 'maintenance', 'active': False},
                                   format='json').json()
        self.assertEqual((changed['role'], changed['active']), ('maintenance', False))
        user = User.objects.get(pk=target)
        self.assertTrue(user.has_perm('floor.repair_machine') is False)  # inactive users have no permissions
        self.assertEqual(list(user.groups.values_list('name', flat=True)), ['Maintenance technician'])
        reset = self.client.post(f'/api/v1/floor/users/{target}/password', {}, format='json').json()
        self.assertTrue(User.objects.get(pk=target).check_password(reset['password']))
