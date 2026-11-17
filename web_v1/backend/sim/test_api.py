"""Clock controls for managers and admins, reset that removes only simulated records, and the floor's "today"
and the customer portal's progress following the factory clock."""
from datetime import timedelta
from django.contrib.auth.models import Permission, User
from rest_framework.test import APIClient
from floor.models import DefectCheck, Issue, OutputEntry
from . import runner
from .models import FactoryClock
from .test_runner import REF, T, RunnerBase


def _client(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


class ClockApiTests(RunnerBase):
    def setUp(self):
        super().setUp()
        for code in ('request_plan', 'approve_plan'):
            self.manager.user_permissions.add(Permission.objects.get(codename=code, content_type__app_label='planning'))
        self.manager = User.objects.get(pk=self.manager.pk)
        self.url = f'/api/v1/sim/clock?dataset={self.dataset.pk}&factory=6'

    def test_manager_starts_and_sets_speed(self):
        self.approve('a', T)
        client = _client(self.manager)
        self.assertIsNone(client.get(self.url).json()['now'])
        started = client.post(self.url, {'action': 'start', 'speed': 10}, format='json').json()
        self.assertEqual((started['speed'], started['running'], started['has_plan']), (10, True, True))
        self.assertTrue(started['now'].startswith('2026-09-10T00:00'))
        self.assertEqual(client.post(self.url, {'action': 'speed', 'speed': 60}, format='json').json()['speed'], 60)
        self.assertEqual(client.post(self.url, {'action': 'pause'}, format='json').json()['running'], False)
        self.assertEqual(client.post(self.url, {'action': 'speed', 'speed': 7}, format='json').status_code, 400)

    def test_floor_role_cannot_control(self):
        viewer = User.objects.create_user('supervisor')
        viewer.user_permissions.add(Permission.objects.get(codename='view_floor', content_type__app_label='floor'))
        client = _client(User.objects.get(pk=viewer.pk))
        self.assertEqual(client.get(self.url).status_code, 200)  # everyone sees the clock
        self.assertEqual(client.post(self.url, {'action': 'start'}, format='json').status_code, 403)

    def test_reset_keeps_human_records(self):
        run = self.approve('a', T)
        self.clock(speed=60)
        runner.tick(T + timedelta(seconds=24))
        OutputEntry.objects.create(run=run, line_id=86, operation_key='1_10', slot=500, pieces=3, entered_by=self.manager)
        Issue.objects.create(dataset=self.dataset, factory_id=6, kind='OTHER', note='real', reported_by=self.manager)
        response = _client(self.manager).post(self.url, {'action': 'reset'}, format='json')
        self.assertEqual(response.json()['now'], None)
        self.assertFalse(FactoryClock.objects.exists())
        self.assertEqual(list(OutputEntry.objects.values_list('entered_by__username', flat=True)), ['manager'])
        self.assertFalse(DefectCheck.objects.filter(entered_by__username='craft_sim').exists())
        self.assertEqual(Issue.objects.count(), 1)

    def test_line_board_today_follows_the_clock(self):
        from floor.plan import load
        from floor.views import _default_day
        run = self.approve('a', T)
        clock = self.clock(speed=0)
        FactoryClock.objects.filter(pk=clock.pk).update(now=REF + timedelta(days=2, hours=3))
        self.assertEqual(_default_day(load(run), [0, 1, 2, 3]), 2)

    def test_hours_passed_follow_the_clock(self):
        from floor.plan import load
        from floor.views import _now_hour
        run = self.approve('a', T)
        clock = self.clock(speed=0)
        FactoryClock.objects.filter(pk=clock.pk).update(now=REF + timedelta(days=2, hours=19))
        plan = load(run)
        self.assertEqual((_now_hour(plan, 1), _now_hour(plan, 2), _now_hour(plan, 3)), (24, 20, 0))

    def test_portal_progress_counts_simulated_output(self):
        from portal.orders import app_pieces
        run = self.approve('a', T)
        OutputEntry.objects.create(run=run, line_id=86, operation_key='1_10', slot=0, pieces=30, entered_by=self.manager)
        OutputEntry.objects.create(run=run, line_id=86, operation_key='1_10', slot=1, pieces=10, entered_by=self.manager)
        self.assertEqual(app_pieces(self.dataset.pk, [1]), {(1, 10): 40})


class ReviewFixTests(RunnerBase):
    """Fixes from the portal + simulator review."""

    def test_control_does_not_send_the_clock_backwards(self):  # review I1
        from unittest import mock
        from . import views
        for code in ('approve_plan',):
            self.manager.user_permissions.add(Permission.objects.get(codename=code, content_type__app_label='planning'))
        manager = User.objects.get(pk=self.manager.pk)
        clock = self.clock(speed=10)
        stale = FactoryClock.objects.get(pk=clock.pk)  # what the request read
        FactoryClock.objects.filter(pk=clock.pk).update(now=REF + timedelta(hours=5), state={'carry': 7})  # a tick
        with mock.patch.object(views, '_find', return_value=stale):
            _client(manager).post(f'/api/v1/sim/clock?dataset={self.dataset.pk}&factory=6',
                                  {'action': 'speed', 'speed': 60}, format='json')
        clock.refresh_from_db()
        self.assertEqual((clock.now, clock.state, clock.speed), (REF + timedelta(hours=5), {'carry': 7}, 60))

    def test_a_reset_clock_is_skipped_by_the_tick(self):  # review I2
        run = self.approve('a', T)
        clock = self.clock(speed=60)
        FactoryClock.objects.filter(pk=clock.pk).delete()
        self.assertEqual(runner.process(clock.pk, T + timedelta(seconds=24)), 0)
        self.assertFalse(OutputEntry.objects.filter(run=run).exists())

    def test_replayed_events_do_not_duplicate_checks(self):  # review I3
        from floor.plan import load
        run = self.approve('a', T)
        clock = self.clock(speed=0)
        check = [{'type': 'CHECK', 'key': '1_10', 'line': 86, 'day': 0, 'checked': 20, 'defects': 1}]
        runner.apply(clock, run, load(run), check)
        runner.apply(clock, run, load(run), check)
        self.assertEqual(DefectCheck.objects.filter(run=run, operation_key='1_10', day=0).count(), 1)

    def test_simulator_never_overwrites_a_supervisors_hour(self):  # review M2
        from floor.plan import load
        run = self.approve('a', T)
        clock = self.clock(speed=0)
        OutputEntry.objects.create(run=run, line_id=86, operation_key='1_10', slot=0, pieces=4, entered_by=self.manager)
        runner.apply(clock, run, load(run), [{'type': 'OUTPUT', 'key': '1_10', 'line': 86, 'slot': 0, 'pieces': 9}])
        entry = OutputEntry.objects.get(run=run, operation_key='1_10', slot=0)
        self.assertEqual((entry.pieces, entry.entered_by.username), (4, 'manager'))

    def test_portal_today_follows_the_factory_clock(self):  # review I4
        from unittest import mock
        from portal import orders
        from portal.models import CustomerAccount
        account = CustomerAccount.objects.create(user=User.objects.create_user('buyer'), client_name='Acme',
                                                 dataset=self.dataset)
        row = {'production_order_id': 9, 'target_quantity': 100, 'scheduled_end_date': '2026-09-20',
               'production_status': 'Processing', 'po_status': 'In_Production', 'delivery_deadline': '2026-09-25',
               'factory_id': '6', 'style_code': 'S', 'product_id': 1}
        clock = self.clock(speed=0)
        FactoryClock.objects.filter(pk=clock.pk).update(now=REF + timedelta(days=30))  # 10 Oct in the factory
        with mock.patch.object(orders, 'fetch', return_value=([row], {9: 10}, {9: 0.4})), \
                mock.patch.object(orders, '_plan_finish', return_value={'9': '2026-09-20'}):
            found = orders.customer_order(account, 'NC-0009')
        self.assertTrue(found['late'])  # due 25 Sep, not finished on 10 Oct

    def test_company_names_match_exactly(self):  # review M1
        from portal import orders
        self.assertIn('BINARY p.client_name = %s', orders._SELECT)
