"""The engine replays a plan hour by hour: output with a performance factor, backlog carried forward,
breakdowns and absences, a daily quality check. Pure: no database."""
import random
from django.test import SimpleTestCase
from floor.plan import Plan
from .engine import HOUR, Rates, State, advance

QUIET = Rates(breakdown_per_hour=0.0, absence_per_day=0.0)


def fake_plan():
    plan = Plan.__new__(Plan)
    ops = {'1_10': {'key': '1_10', 'order_id': '1', 'step': 1, 'line': 86, 'pieces': 100, 'machines': ['5'],
                    'workers': ['7'], 'segments': [(0, 10 * HOUR)]},
           '1_11': {'key': '1_11', 'order_id': '1', 'step': 2, 'line': 86, 'pieces': 50, 'machines': ['6'],
                    'workers': ['8'], 'segments': [(10 * HOUR, 15 * HOUR)]}}
    for op in ops.values():
        op['productive'] = sum(b - a for a, b in op['segments'])
    plan.ops = ops
    return plan


def total(events, key, kind='OUTPUT'):
    return sum(e['pieces'] for e in events if e['type'] == kind and e['key'] == key)


class EngineTests(SimpleTestCase):
    def test_output_follows_plan_with_perf(self):
        state = State(perf={'1_10': 1.0, '1_11': 1.0})
        events = advance(fake_plan(), state, 0, 24 * HOUR, random.Random(1), QUIET)
        self.assertEqual((total(events, '1_10'), total(events, '1_11')), (100, 50))
        self.assertEqual(state.done, {'1_10': 100, '1_11': 50})
        first = next(e for e in events if e['type'] == 'OUTPUT')
        self.assertEqual((first['key'], first['line'], first['slot'], first['pieces']), ('1_10', 86, 0, 10))

    def test_same_seed_same_events(self):
        run = lambda: advance(fake_plan(), State(), 0, 48 * HOUR, random.Random(7), Rates())
        self.assertEqual(run(), run())

    def test_backlog_carries_forward(self):
        state = State(perf={'1_10': 0.5, '1_11': 1.0})
        advance(fake_plan(), state, 0, 10 * HOUR, random.Random(1), QUIET)
        self.assertEqual(state.done['1_10'], 50)  # half speed during its planned hours
        advance(fake_plan(), state, 10 * HOUR, 48 * HOUR, random.Random(1), QUIET)
        self.assertEqual(state.done['1_10'], 100)  # finished later, in the line's working hours

    def test_machine_down_stops_its_operation(self):
        state = State(perf={'1_10': 1.0, '1_11': 1.0}, down={5: 5 * HOUR})
        events = advance(fake_plan(), state, 0, 10 * HOUR, random.Random(1), QUIET)
        self.assertFalse([e for e in events if e['type'] == 'OUTPUT' and e['key'] == '1_10' and e['slot'] < 5])
        self.assertIn({'type': 'REPAIRED', 'machine': 5, 'line': 86}, events)
        self.assertEqual(state.done['1_10'], 50)

    def test_no_second_breakdown_for_a_down_machine(self):
        state = State(perf={'1_10': 1.0, '1_11': 1.0})
        events = advance(fake_plan(), state, 0, 2 * HOUR, random.Random(3), Rates(breakdown_per_hour=1.0, absence_per_day=0.0))
        self.assertEqual(len([e for e in events if e['type'] == 'BREAKDOWN' and e['machine'] == 5]), 1)

    def test_never_more_than_operation_pieces(self):
        state = State(perf={'1_10': 1.5, '1_11': 1.5})
        events = advance(fake_plan(), state, 0, 72 * HOUR, random.Random(1), QUIET)
        self.assertEqual((total(events, '1_10'), total(events, '1_11')), (100, 50))

    def test_daily_check_has_defects_within_rate(self):
        events = advance(fake_plan(), State(perf={'1_10': 1.0, '1_11': 1.0}), 0, 24 * HOUR, random.Random(1), QUIET)
        checks = [e for e in events if e['type'] == 'CHECK']
        self.assertEqual(sorted(c['key'] for c in checks), ['1_10', '1_11'])
        for c in checks:
            self.assertEqual((c['day'], c['line']), (0, 86))
            self.assertTrue(0 < c['checked'] <= 20 and 0 <= c['defects'] <= c['checked'])

    def test_absent_worker_slows_the_operation(self):
        state = State(perf={'1_10': 1.0, '1_11': 1.0}, absent={7: 0})
        advance(fake_plan(), state, 0, 10 * HOUR, random.Random(1), QUIET)
        self.assertEqual(state.done.get('1_10', 0), 0)  # its only worker is away today
        self.assertTrue(state.to_json() and State.from_json(state.to_json()).absent == {7: 0})
