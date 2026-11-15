"""ERP read models against the real factory database (skipped when MySQL or the
read-only account is not available on this machine)."""
from datetime import date
from unittest import skipUnless
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient
from planning.models import Dataset
from . import queries
from .db import ErpUnavailable, read_only

DB = 'garment_fyp_db_seed_v2_data2_load30_20261018_083214'
REF = date(2026, 9, 10)


def _available():
    try:
        with read_only(DB) as cursor:
            cursor.execute('SELECT 1')
        return True
    except ErpUnavailable:
        return False


AVAILABLE = _available()


class SafetyTests(SimpleTestCase):
    def test_only_validated_source_databases(self):
        for name in ('mysql', 'garment_fyp_db; DROP TABLE x', 'craft_web_db', ''):
            with self.assertRaises(ErpUnavailable):
                with read_only(name):
                    pass

    @skipUnless(AVAILABLE, 'factory database not available')
    def test_session_refuses_writes(self):
        with read_only(DB) as cursor:
            with self.assertRaisesRegex(Exception, 'READ ONLY'):
                cursor.execute('CREATE TEMPORARY TABLE erp_probe (a INT)')


@skipUnless(AVAILABLE, 'factory database not available')
class ReadModelTests(SimpleTestCase):
    def test_factory4_matches_its_capture(self):
        with read_only(DB) as cursor:
            summary = queries.summary(cursor, 4, REF)
            people = queries.workforce(cursor, 4, REF, size=1)
            machines = queries.machines(cursor, 4, size=1)
        self.assertEqual(summary['orders_total'], 84)  # factory 4 orders in the 30% capture
        self.assertEqual(people['total'], 102)
        self.assertEqual(machines['total'], 46)
        self.assertGreater(summary['pieces_inspected'], 0)

    def test_order_filters_and_detail(self):
        with read_only(DB) as cursor:
            overdue = queries.orders(cursor, 4, REF, status='OVERDUE', size=200)
            assert overdue['items']
            self.assertTrue(all(o['delivery_deadline'] < REF.isoformat() for o in overdue['items']))
            detail = queries.order_detail(cursor, 4, overdue['items'][0]['production_order_id'])
            self.assertTrue(detail['routing'])
            self.assertIsNone(queries.order_detail(cursor, 4, -1))

    def test_crews_get_a_shift_and_rest_day(self):
        with read_only(DB) as cursor:
            crews = queries.workforce(cursor, 4, REF, size=1)['crews']
        self.assertTrue(crews)
        self.assertTrue(all(c['shift_today'] and c['rest_day'] for c in crews))

    def test_crew_members_follow_their_crew_rotation(self):
        with read_only(DB) as cursor:
            data = queries.workforce(cursor, 4, REF, size=200)
        crews = {c['batch_name']: c for c in data['crews']}
        members = [p for p in data['items'] if p['crew']]
        self.assertTrue(members)
        for person in members:
            crew = crews[person['crew']]
            self.assertEqual(f"{person['shift_name']} {person['start_time']}–{person['end_time']}", crew['shift_today'])
            self.assertEqual(person['resting_today'], crew['resting_today'])


@skipUnless(AVAILABLE, 'factory database not available')
class ApiTests(TestCase):
    def test_endpoints_need_login_and_answer(self):
        dataset = Dataset.objects.create(slug='t', label='30% load', origin='SNAPSHOT', path='x', database=DB,
                                         load_pct=30, reference_time='2026-09-10T00:00:00',
                                         factories=[{'factory_id': 4}])
        client = APIClient()
        self.assertEqual(client.get(f'/api/v1/erp/{dataset.pk}/4/summary').status_code, 403)
        client.force_authenticate(User.objects.create_user('viewer'))
        for module in ('summary', 'orders', 'styles', 'materials', 'workforce', 'machines', 'quality'):
            response = client.get(f'/api/v1/erp/{dataset.pk}/4/{module}')
            self.assertEqual(response.status_code, 200, module)
        self.assertEqual(client.get(f'/api/v1/erp/{dataset.pk}/9/summary').status_code, 404)
