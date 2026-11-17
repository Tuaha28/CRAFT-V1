"""Customer portal: a customer sees only their own orders, the agent reads them through a service-key contract,
requests are idempotent, and a customer session cannot reach any staff API."""
import base64
import hashlib
import hmac
import json
import time
from unittest import mock
from django.contrib.auth.models import Permission, User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from planning.models import Dataset
from . import agent_client, orders
from .models import CustomerAccount, CustomerRequest

KEY = 'service-key-for-tests-0123456789abcdef'
SECRET = 'jwt-secret-for-tests-0123456789abcdef0123'

# Two orders of Acme and one of Other Co, as the read-only query returns them.
ROWS = [
    {'production_order_id': 477, 'target_quantity': 200, 'scheduled_end_date': '2026-09-20',
     'production_status': 'Processing', 'po_number': 'PO-1', 'po_status': 'In_Production', 'order_date': '2026-08-01',
     'delivery_deadline': '2026-09-25', 'style_code': '1000208', 'product': 'NeedleCraft Elite Pant', 'category': 'Pant',
     'product_id': 1, 'factory_id': '6', 'carrier_name': None, 'tracking_number': None, 'actual_shipment_date': None,
     'estimated_arrival_date': None, 'delivery_status': None, 'client_name': 'Acme'},
    {'production_order_id': 12, 'target_quantity': 100, 'scheduled_end_date': '2026-09-01',
     'production_status': 'Completed', 'po_number': 'PO-2', 'po_status': 'Shipped', 'order_date': '2026-07-01',
     'delivery_deadline': '2026-09-05', 'style_code': '1000300', 'product': 'NeedleCraft Elite Tee', 'category': 'Tee',
     'product_id': 2, 'factory_id': '6', 'carrier_name': 'DHL', 'tracking_number': 'TRK1',
     'actual_shipment_date': '2026-09-02', 'estimated_arrival_date': '2026-09-12', 'delivery_status': 'In_Transit',
     'client_name': 'Acme'},
]


def fake_fetch(account, production_order_id=None):
    mine = [dict(r) for r in ROWS if r['client_name'] == account.client_name]
    if production_order_id is not None:
        mine = [r for r in mine if r['production_order_id'] == production_order_id]
    progress = {477: (50, 4), 12: (100, 4)}  # pieces through every step, steps
    return mine, {r['production_order_id']: progress[r['production_order_id']][0] for r in mine}


@override_settings(CRAFT_AGENT_SERVICE_KEY=KEY, CRAFT_AGENT_JWT_SECRET=SECRET, CRAFT_AGENT_URL='http://agent.test')
class PortalBase(TestCase):
    def setUp(self):
        self.dataset = Dataset.objects.create(slug='d', label='30% load', origin='SNAPSHOT', path='x', database='db',
                                              reference_time='2026-09-10T00:00:00', factories=[{'factory_id': 6}])
        self.acme = self.customer('acme_buyer', 'Acme')
        self.other = self.customer('other_buyer', 'Other Co')
        self.patch = mock.patch.object(orders, 'fetch', side_effect=fake_fetch)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def customer(self, username, client):
        user = User.objects.create_user(username, password='pw-for-tests-only')
        return CustomerAccount.objects.create(user=user, client_name=client, dataset=self.dataset)

    def agent(self, account, method, path, data=None, key=KEY, idem=None):
        headers = {'HTTP_AUTHORIZATION': f'Bearer {key}', 'HTTP_X_CUSTOMER_ID': account.customer_id}
        if idem:
            headers['HTTP_IDEMPOTENCY_KEY'] = idem
        if method == 'GET':
            return self.client.get(path, **headers)
        return self.client.post(path, json.dumps(data), content_type='application/json', **headers)

    def portal(self, account):
        client = APIClient()
        client.force_login(account.user)  # a real session, so the request middleware sees the customer
        return client


class OrderShapeTests(PortalBase):
    def test_order_reference_round_trips(self):
        self.assertEqual(orders.order_ref(477), 'NC-0477')
        self.assertEqual(orders.parse_ref('NC-0477'), 477)
        self.assertIsNone(orders.parse_ref('NC-00477'))  # not the form we issue: never matches another order
        self.assertIsNone(orders.parse_ref('477'))

    def test_customer_orders_have_stage_progress_and_dates(self):
        found = {o['order_id']: o for o in orders.customer_orders(self.acme)}
        self.assertEqual(set(found), {'NC-0477', 'NC-0012'})
        sewing = found['NC-0477']
        self.assertEqual((sewing['stage'], sewing['progress_percent']), ('In production', 25))
        self.assertEqual(sewing['estimated_ready_date'], '2026-09-20')  # no approved plan: the scheduled end
        self.assertEqual(sewing['customer_id'], self.acme.customer_id)
        shipped = found['NC-0012']
        self.assertEqual((shipped['stage'], shipped['progress_percent'], shipped['carrier'],
                          shipped['tracking_reference'], shipped['estimated_arrival_date']),
                         ('Shipped', 100, 'DHL', 'TRK1', '2026-09-12'))

    def test_arrival_before_the_ready_date_is_not_shown(self):
        bad = [dict(ROWS[0], estimated_arrival_date='2026-09-02')]  # the record says it arrives before it is made
        with mock.patch.object(orders, 'fetch', return_value=(bad, {477: 50})):
            self.assertIsNone(orders.customer_order(self.acme, 'NC-0477')['estimated_arrival_date'])
        good = [dict(ROWS[0], estimated_arrival_date='2026-09-30')]
        with mock.patch.object(orders, 'fetch', return_value=(good, {477: 50})):
            self.assertEqual(orders.customer_order(self.acme, 'NC-0477')['estimated_arrival_date'], '2026-09-30')

    def test_a_schedule_date_already_past_is_not_promised(self):
        # the factory data's "today" is 2026-09-10: a scheduled end before it, with work not finished, is stale
        stale = [dict(ROWS[0], scheduled_end_date='2026-08-20', delivery_deadline='2026-08-25')]
        with mock.patch.object(orders, 'fetch', return_value=(stale, {477: 0})):
            found = orders.customer_order(self.acme, 'NC-0477')
        self.assertIsNone(found['estimated_ready_date'])
        self.assertTrue(found['late'])  # due before today and not shipped
        self.assertIn('confirm', found['customer_note'])

    def test_started_order_is_in_production_with_step_progress(self):
        # 200 pieces, 4 steps: step 1 has 100 done, the rest nothing; nothing has passed every step yet
        waiting = [dict(ROWS[0], production_status='Pending', po_status='Confirmed')]
        with mock.patch.object(orders, 'fetch', return_value=(waiting, {477: 0}, {477: 0.125})):
            found = orders.customer_order(self.acme, 'NC-0477')
        self.assertEqual((found['stage'], found['progress_percent']), ('In production', 12))

    def test_another_company_sees_none_of_them(self):
        self.assertEqual(orders.customer_orders(self.other), [])
        self.assertIsNone(orders.customer_order(self.other, 'NC-0477'))

    def test_ready_date_comes_from_the_approved_plan_when_the_order_is_in_it(self):
        with mock.patch.object(orders, '_plan_finish', return_value={'477': '2026-09-18'}):
            found = orders.customer_order(self.acme, 'NC-0477')
        self.assertEqual((found['estimated_ready_date'], found['ready_from']), ('2026-09-18', 'plan'))


class AgentContractTests(PortalBase):
    def test_needs_the_service_key(self):
        self.assertEqual(self.agent(self.acme, 'GET', '/customer/orders', key='wrong').status_code, 401)
        response = self.client.get('/customer/orders', HTTP_X_CUSTOMER_ID=self.acme.customer_id)
        self.assertEqual(response.status_code, 401)

    def test_unknown_or_inactive_customer_is_refused(self):
        self.acme.user.is_active = False
        self.acme.user.save()
        self.assertEqual(self.agent(self.acme, 'GET', '/customer/orders').status_code, 403)
        response = self.client.get('/customer/orders', HTTP_AUTHORIZATION=f'Bearer {KEY}', HTTP_X_CUSTOMER_ID='CUST-999')
        self.assertEqual(response.status_code, 403)

    def test_orders_and_order_are_scoped_to_the_customer(self):
        body = self.agent(self.acme, 'GET', '/customer/orders').json()
        self.assertEqual(sorted(o['order_id'] for o in body), ['NC-0012', 'NC-0477'])
        self.assertTrue(all(o['customer_id'] == self.acme.customer_id for o in body))
        self.assertEqual(self.agent(self.acme, 'GET', '/customer/orders/NC-0477').json()['order_id'], 'NC-0477')
        # someone else's order and a missing one look the same
        self.assertEqual(self.agent(self.other, 'GET', '/customer/orders/NC-0477').status_code, 404)
        self.assertEqual(self.agent(self.acme, 'GET', '/customer/orders/NC-9999').status_code, 404)

    def test_no_internal_fields_leave(self):
        body = self.agent(self.acme, 'GET', '/customer/orders/NC-0477').json()
        for internal in ('factory_id', 'production_status', 'client_name', 'product_id', 'po_status'):
            self.assertNotIn(internal, body)

    def test_products_are_the_reference_catalogue(self):
        body = self.agent(self.acme, 'GET', '/customer/products').json()
        self.assertEqual([p['style_code'] for p in body], ['NC-TEE-180', 'NC-POLO-220', 'NC-ACT-150', 'NC-HOOD-300'])

    def test_request_is_idempotent_and_conflicts_are_refused(self):
        data = {'kind': 'order_change', 'order_id': 'NC-0477', 'description': 'Please add 20 pieces in navy.'}
        first = self.agent(self.acme, 'POST', '/customer/requests', data, idem='a1b2')
        self.assertEqual(first.status_code, 201)
        receipt = first.json()
        self.assertEqual((receipt['status'], receipt['approval_status']), ('submitted', 'pending_review'))
        self.assertRegex(receipt['request_id'], r'^REQ-\d+$')
        again = self.agent(self.acme, 'POST', '/customer/requests', data, idem='a1b2')
        self.assertEqual(again.json()['request_id'], receipt['request_id'])
        self.assertEqual(CustomerRequest.objects.count(), 1)
        changed = self.agent(self.acme, 'POST', '/customer/requests', {**data, 'description': 'Something else now.'},
                             idem='a1b2')
        self.assertEqual(changed.status_code, 409)

    def test_request_needs_a_key_and_an_own_order(self):
        data = {'kind': 'order_change', 'order_id': 'NC-0477', 'description': 'Please add 20 pieces in navy.'}
        self.assertEqual(self.agent(self.acme, 'POST', '/customer/requests', data).status_code, 400)
        self.assertEqual(self.agent(self.other, 'POST', '/customer/requests', data, idem='x').status_code, 404)
        short = {'kind': 'support', 'description': 'help'}
        self.assertEqual(self.agent(self.acme, 'POST', '/customer/requests', short, idem='y').status_code, 400)


class GuardTests(PortalBase):
    def test_customer_cannot_reach_staff_apis(self):
        client = self.portal(self.acme)
        for path in ('/api/v1/planning/runs', '/api/v1/erp/1/6/orders', '/api/v1/floor/1/6/issues',
                     '/api/v1/planning/events', '/api/v1/assistant/ask'):
            self.assertEqual(client.get(path).status_code, 403, path)

    def test_customer_session_says_customer(self):
        body = self.portal(self.acme).get('/api/v1/planning/session').json()
        self.assertEqual((body['role'], body['customer']['client_name']), ('customer', 'Acme'))
        self.assertEqual(body['perms'], [])

    def test_portal_pages_show_only_own_orders(self):
        client = self.portal(self.acme)
        self.assertEqual(sorted(o['order_id'] for o in client.get('/api/v1/portal/orders').json()),
                         ['NC-0012', 'NC-0477'])
        self.assertEqual(self.portal(self.other).get('/api/v1/portal/orders/NC-0477').status_code, 404)

    def test_portal_says_when_the_factory_is_simulated(self):
        from datetime import datetime, timezone as dt_timezone
        from sim.models import FactoryClock
        client = self.portal(self.acme)
        self.assertFalse(client.get('/api/v1/portal/me').json()['simulated'])
        FactoryClock.objects.create(dataset=self.dataset, factory_id=6, now=datetime(2026, 9, 10, tzinfo=dt_timezone.utc), speed=10)
        self.assertTrue(client.get('/api/v1/portal/me').json()['simulated'])

    def test_staff_cannot_use_customer_pages(self):
        staff = User.objects.create_user('planner')
        client = APIClient()
        client.force_authenticate(staff)
        self.assertEqual(client.get('/api/v1/portal/orders').status_code, 403)


class TokenTests(PortalBase):
    def test_token_is_a_valid_short_lived_hs256_jwt(self):
        token = agent_client.mint(self.acme, now=1_800_000_000)
        head, body, sig = token.split('.')
        pad = lambda s: s + '=' * (-len(s) % 4)
        expected = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), f'{head}.{body}'.encode(),
                                                     hashlib.sha256).digest()).rstrip(b'=').decode()
        self.assertEqual(sig, expected)
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(head))), {'alg': 'HS256', 'typ': 'JWT'})
        claims = json.loads(base64.urlsafe_b64decode(pad(body)))
        self.assertEqual((claims['sub'], claims['iss'], claims['aud'], claims['scope']),
                         (self.acme.customer_id, 'needlecraft-portal', 'needlecraft-agent', 'customer:read customer:write'))
        self.assertEqual(claims['exp'] - claims['iat'], 600)

    def test_short_secret_is_refused(self):
        with override_settings(CRAFT_AGENT_JWT_SECRET='short'):
            with self.assertRaises(agent_client.AgentUnavailable):
                agent_client.mint(self.acme)

    def test_chat_without_agent_says_unavailable(self):
        with mock.patch.object(agent_client, 'call', side_effect=agent_client.AgentUnavailable('down')):
            response = self.portal(self.acme).post('/api/v1/portal/chat', {'message': 'Where is my order?'},
                                                   format='json')
        self.assertEqual(response.status_code, 503)

    def test_chat_forwards_the_question_as_this_customer(self):
        reply = {'answer': 'NC-0477 is in production.', 'session_id': 's1', 'sources': [], 'pending_actions': [],
                 'mode': 'llm', 'tool_trace': [{'name': 'get_order'}]}
        with mock.patch.object(agent_client, 'call', return_value=reply) as call:
            body = self.portal(self.acme).post('/api/v1/portal/chat', {'message': 'Where is NC-0477?'},
                                               format='json').json()
        self.assertEqual(body['answer'], 'NC-0477 is in production.')
        self.assertNotIn('tool_trace', body)
        self.assertEqual(call.call_args.args[:3], (self.acme, 'POST', '/chat'))


class StaffTests(PortalBase):
    def staff(self, *perms):
        user = User.objects.create_user('staff_' + '_'.join(p.split('.')[1] for p in perms))
        for perm in perms:
            app, code = perm.split('.')
            user.user_permissions.add(Permission.objects.get(codename=code, content_type__app_label=app))
        client = APIClient()
        client.force_authenticate(User.objects.get(pk=user.pk))
        return client

    def test_staff_decide_a_request_and_the_customer_sees_it(self):
        CustomerRequest.objects.create(account=self.acme, kind='support', description='Need the packing list',
                                       idempotency_key='k')
        client = self.staff('portal.handle_request')
        listed = client.get('/api/v1/portal/staff/requests').json()
        self.assertEqual((listed[0]['client_name'], listed[0]['status']), ('Acme', 'PENDING'))
        decided = client.post(f'/api/v1/portal/staff/requests/{listed[0]["id"]}',
                              {'decision': 'ACCEPTED', 'note': 'Sent by email today'}, format='json')
        self.assertEqual(decided.status_code, 200)
        mine = self.portal(self.acme).get('/api/v1/portal/requests').json()
        self.assertEqual((mine[0]['status'], mine[0]['staff_note']), ('ACCEPTED', 'Sent by email today'))
        self.assertEqual(self.portal(self.other).get('/api/v1/portal/requests').json(), [])

    def test_requests_need_the_permission(self):
        self.assertEqual(self.staff('floor.view_floor').get('/api/v1/portal/staff/requests').status_code, 403)

    def test_admin_creates_a_customer_account_for_an_existing_client(self):
        client = self.staff('floor.manage_users')
        with mock.patch.object(orders, 'client_exists', return_value=True):
            created = client.post('/api/v1/portal/staff/accounts', {'username': 'acme2', 'client_name': 'Acme',
                                                                    'dataset_id': self.dataset.pk}, format='json')
        self.assertEqual(created.status_code, 201)
        self.assertTrue(created.json()['password'])
        self.assertEqual(CustomerAccount.objects.get(user__username='acme2').client_name, 'Acme')
        with mock.patch.object(orders, 'client_exists', return_value=False):
            missing = client.post('/api/v1/portal/staff/accounts', {'username': 'ghost', 'client_name': 'Nobody',
                                                                    'dataset_id': self.dataset.pk}, format='json')
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(self.staff('portal.handle_request').get('/api/v1/portal/staff/accounts').status_code, 403)
