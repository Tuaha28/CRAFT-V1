"""The what-if copy changes only CRAFT's input: priority, stopped lines, delayed release."""
import copy
from unittest import TestCase

from integrations.craft.plan_scenario import apply


def _source():
    return {
        'reference_time': '2026-09-10T00:00:00',
        'orders': [
            {'production_order_id': 11, 'factory_id': 6, 'delivery_deadline': '2026-09-30',
             'scheduled_start_date': '2026-09-20', 'status': 'Pending', 'target_quantity': 100, 'product_id': 1},
            {'production_order_id': 12, 'factory_id': 6, 'delivery_deadline': '2026-10-05',
             'scheduled_start_date': '2026-09-12', 'status': 'Pending', 'target_quantity': 50, 'product_id': 2},
        ],
        'resources': {
            'machines': [{'asset_id': 1, 'factory_id': 6, 'line_id': 5, 'current_status': 'Active'},
                         {'asset_id': 2, 'factory_id': 6, 'line_id': 5, 'current_status': 'Active'},
                         {'asset_id': 3, 'factory_id': 6, 'line_id': 6, 'current_status': 'Active'}],
            'workers': [{'employee_id': 40, 'factory_id': 6}],
            'work_schedules': [{'schedule_id': 1, 'employee_id': 40, 'is_on_leave': False,
                                'shift_date': '2026-09-10', 'start_time': None, 'end_time': None}],
        },
        'lots': [{'schedule_item_id': 1}], 'operations': [],
    }


def _order(source, oid):
    return next(o for o in source['orders'] if int(o['production_order_id']) == oid)


class ScenarioApplyTests(TestCase):
    def test_priority_sets_due_to_plan_start(self):
        out = apply(_source(), 6, {'priority_orders': ['11']})
        self.assertEqual(_order(out, 11)['delivery_deadline'], '2026-09-10')
        self.assertEqual(_order(out, 12)['delivery_deadline'], '2026-10-05')

    def test_line_stopped_breaks_every_machine_on_it(self):
        out = apply(_source(), 6, {'lines_stopped': [5]})
        status = {m['asset_id']: m['current_status'] for m in out['resources']['machines']}
        self.assertEqual(status, {1: 'Breakdown', 2: 'Breakdown', 3: 'Active'})

    def test_release_after_never_moves_a_start_earlier(self):
        out = apply(_source(), 6, {'release_after': {'11': '2026-09-15', '12': '2026-09-25'}})
        self.assertEqual(_order(out, 11)['scheduled_start_date'], '2026-09-20')
        self.assertEqual(_order(out, 12)['scheduled_start_date'], '2026-09-25')

    def test_unknown_ids_are_rejected(self):
        for scenario in ({'priority_orders': ['999']}, {'lines_stopped': [77]}, {'release_after': {'999': '2026-09-15'}}):
            with self.assertRaises(SystemExit):
                apply(_source(), 6, scenario)

    def test_existing_keys_unchanged(self):
        out = apply(_source(), 6, {'machines_down': [3], 'workers_absent': [40]})
        self.assertEqual(out['resources']['machines'][2]['current_status'], 'Breakdown')
        self.assertTrue(out['resources']['work_schedules'][0]['is_on_leave'])

    def test_the_input_is_not_changed(self):
        source = _source()
        before = copy.deepcopy(source)
        apply(source, 6, {'priority_orders': ['11'], 'lines_stopped': [5]})
        self.assertEqual(source, before)
