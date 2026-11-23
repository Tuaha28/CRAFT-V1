"""The completion API: steps on My line, rework, an order's status and its actions, statuses for the Orders page."""
import json
from .models import DefectCheck
from .tests import Fixture


class CompletionApiTests(Fixture):
    def call(self, user, path, body=None):
        self.client.force_login(self.users[user])
        base = f'/api/v1/floor/{self.dataset.pk}/4/'
        if body is None:
            return self.client.get(base + path)
        return self.client.post(base + path, json.dumps(body), content_type='application/json')

    def test_the_supervisor_completes_a_step_and_qc_may_not(self):
        self.assertEqual(self.call('qc', 'steps/1_10/complete', {}).status_code, 403)
        answer = self.call('supervisor', 'steps/1_10/complete', {})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()['state'], 'COMPLETE')
        short = self.call('supervisor', 'steps/1_11/complete', {'pieces': 10})
        self.assertEqual(short.status_code, 409)
        self.assertIn('reason', short.json()['error'])
        self.assertEqual(self.call('manager', 'steps/1_10/reopen', {'reason': 'mistake'}).status_code, 200)

    def test_my_line_shows_each_steps_state_and_what_the_user_may_do(self):
        self.out(pieces=20)
        day = self.call('supervisor', 'lines/44?day=0').json()
        op = next(o for o in day['operations'] if o['key'] == '1_10')
        self.assertEqual((op['status']['state'], op['status']['good'], op['status']['needed']), ('IN_PROGRESS', 20, 100))
        self.assertTrue(day['can_complete'])
        self.assertFalse(day['can_reopen'])
        self.assertTrue(self.call('manager', 'lines/44?day=0').json()['can_reopen'])

    def test_defects_are_listed_for_rework_and_can_be_marked_reworked(self):
        self.out(pieces=20)
        check = DefectCheck.objects.create(run=self.run, line_id=44, day=0, operation_key='1_10', checked=10, defects=2,
                                           defect_type='Open seam', entered_by=self.users['qc'])
        day = self.call('supervisor', 'lines/44?day=0').json()
        self.assertEqual([c['id'] for c in day['rework']], [check.pk])
        self.client.force_login(self.users['supervisor'])
        self.assertEqual(self.client.post(f'/api/v1/floor/checks/{check.pk}/rework').status_code, 200)
        self.assertEqual(self.call('supervisor', 'lines/44?day=0').json()['rework'], [])

    def test_rework_lists_the_lines_defects_from_other_days_too(self):
        check = DefectCheck.objects.create(run=self.run, line_id=44, day=1, operation_key='1_11', checked=5, defects=1,
                                           defect_type='Open seam', entered_by=self.users['qc'])
        day = self.call('supervisor', 'lines/44?day=0').json()
        self.assertEqual([c['id'] for c in day['rework']], [check.pk])

    def test_an_order_goes_through_its_stages_by_api(self):
        self.call('supervisor', 'steps/1_10/complete', {})
        self.call('supervisor', 'steps/1_11/complete', {})
        status = self.call('qc', 'orders/1').json()
        self.assertEqual(status['stage'], 'SEWN')
        self.assertEqual([s['state'] for s in status['steps']], ['COMPLETE', 'COMPLETE'])
        self.assertTrue(status['can']['inspect'])
        self.assertFalse(status['can']['pack'])
        self.assertEqual(self.call('qc', 'orders/1/inspect', {'result': 'PASS'}).status_code, 200)
        self.assertEqual(self.call('qc', 'orders/1/pack', {'cartons': 2}).status_code, 403)
        self.assertEqual(self.call('merchandiser', 'orders/1/pack', {'cartons': 2}).status_code, 200)
        shipped = self.call('merchandiser', 'orders/1/ship', {'carrier': 'Aramex Logistics', 'tracking': 'AX-1'})
        self.assertEqual(shipped.json()['stage'], 'SHIPPED')
        self.assertEqual(self.call('merchandiser', 'orders/1/fly', {}).status_code, 404)
        statuses = self.call('merchandiser', 'orders/statuses').json()['statuses']
        self.assertEqual(statuses['1']['carrier'], 'Aramex Logistics')

    def test_order_pages_need_the_right_to_see_orders(self):
        self.assertEqual(self.call('supervisor', 'orders/statuses').status_code, 403)
        self.assertEqual(self.call('maintenance', 'orders/1').status_code, 403)
