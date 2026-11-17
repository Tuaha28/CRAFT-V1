"""Experiment mode: the engine without a clock or database; planned against executed results per seed."""
from django.test import SimpleTestCase
from .engine import Rates
from .experiment import run
from .test_engine import fake_plan

EXACT = Rates(breakdown_per_hour=0.0, absence_per_day=0.0, perf_mean=1.0, perf_sd=0.0)


def plan_with_due(due_day):
    plan = fake_plan()
    plan.due = {'1': due_day}
    return plan


class ExperimentTests(SimpleTestCase):
    def test_no_disruption_perf_one_matches_plan(self):
        result = run(plan_with_due(1.0), days=5, seed=1, rates=EXACT)
        order = result['orders'][0]
        self.assertEqual(order['order_id'], '1')
        self.assertAlmostEqual(order['planned_finish_day'], 15 / 24, places=3)  # last step ends at hour 15
        self.assertAlmostEqual(order['simulated_finish_day'], 15 / 24, places=3)
        self.assertEqual((result['on_time_planned'], result['on_time_simulated']), (1.0, 1.0))
        self.assertAlmostEqual(result['adherence'], 1.0)
        self.assertEqual(result['unfinished'], 0)

    def test_more_breakdowns_more_lateness(self):
        plan = plan_with_due(15 / 24)
        mean = lambda rate: sum(run(plan, days=10, seed=s, rates=Rates(breakdown_per_hour=rate, absence_per_day=0.0))
                                ['mean_lateness_simulated'] for s in range(1, 21)) / 20
        self.assertGreater(mean(0.2), mean(0.0))

    def test_same_seed_same_result(self):
        self.assertEqual(run(plan_with_due(1.0), days=5, seed=4, rates=Rates()),
                         run(plan_with_due(1.0), days=5, seed=4, rates=Rates()))
