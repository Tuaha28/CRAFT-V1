"""Completing steps and orders: who may do what, the no-pieces 'Mark complete', closing short, rework, the order
journey (sewn, final inspection, packed, shipped, closed) and what plan-ahead, the simulator and the demo copy see."""
from types import SimpleNamespace
from unittest import mock
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Sum
from django.test import override_settings
from planning.models import PlanEvent
from . import completion, services
from .models import OrderStatus, OutputEntry, StepStatus
from .plan import load
from .tests import Fixture


class Base(Fixture):
    def states(self):
        return completion.step_states(self.dataset.pk, 4, load(self.run))

    def complete(self, key='1_10', user='supervisor', **kw):
        return completion.complete_step(self.users[user], dataset_id=self.dataset.pk, factory_id=4,
                                        operation_key=key, **kw)

    def recorded(self, key):
        return OutputEntry.objects.filter(operation_key=key).aggregate(n=Sum('pieces'))['n'] or 0


class StepTests(Base):
    def test_a_step_moves_from_not_started_to_in_progress(self):
        self.assertEqual(self.states()['1_10']['state'], 'NOT_STARTED')
        self.out(pieces=12)
        s = self.states()['1_10']
        self.assertEqual((s['state'], s['good'], s['needed']), ('IN_PROGRESS', 12, 100))

    def test_a_step_completes_by_itself_when_all_pieces_are_recorded(self):
        self.out(hour=8, pieces=50)
        self.out(hour=9, pieces=50)
        s = self.states()['1_10']
        self.assertEqual(s['state'], 'COMPLETE')
        self.assertTrue(StepStatus.objects.filter(operation_key='1_10', state='COMPLETE').exists())  # finish time kept

    def test_mark_complete_with_no_pieces_recorded_fills_them_in(self):
        self.complete()
        self.assertEqual(self.states()['1_10']['state'], 'COMPLETE')
        self.assertEqual(self.recorded('1_10'), 100)
        self.assertTrue(PlanEvent.objects.filter(action='STEP_COMPLETED', message__contains='without hourly counts').exists())

    def test_closing_short_needs_a_reason_and_keeps_the_real_count(self):
        with self.assertRaisesMessage(ValidationError, 'reason'):
            self.complete(pieces=90)
        self.complete(pieces=90, reason='10 pieces rejected at cutting')
        s = self.states()['1_10']
        self.assertEqual((s['state'], s['final']), ('SHORT', 90))
        self.assertEqual(self.recorded('1_10'), 90)

    def test_the_count_cannot_be_below_what_is_already_recorded(self):
        self.out(pieces=50)
        with self.assertRaises(ValidationError):
            self.complete(pieces=40, reason='typo')

    def test_a_step_waits_for_the_step_before(self):
        with self.assertRaisesMessage(ValidationError, 'step before'):
            self.complete('1_11')
        with self.assertRaises(ValidationError):
            self.out(day=1, hour=9, operation_key='1_11', pieces=5)  # step 10 has made nothing yet
        self.complete(pieces=50, reason='short cut')
        with self.assertRaises(ValidationError):
            self.complete('1_11', pieces=55, reason='more than came in')
        self.complete('1_11', reason='only 50 came from step 10')
        self.assertEqual(self.states()['1_11']['final'], 50)

    def test_each_step_says_whether_the_step_before_is_done(self):
        s = self.states()
        self.assertEqual((s['1_10']['ready'], s['1_11']['ready'], s['1_11']['previous']), (True, False, 10))
        self.complete()
        self.assertTrue(self.states()['1_11']['ready'])

    def test_a_reopened_step_stays_open_until_marked_complete_again(self):
        self.out(hour=8, pieces=50)
        self.out(hour=9, pieces=50)  # completes by itself
        completion.reopen_step(self.users['manager'], dataset_id=self.dataset.pk, factory_id=4, operation_key='1_10',
                               reason='count looks wrong')
        completion.refresh(self.dataset.pk, 4, load(self.run), self.users['manager'])
        self.assertEqual(self.states()['1_10']['state'], 'IN_PROGRESS')
        self.out(hour=9, pieces=45)  # the supervisor corrects an hour
        self.assertEqual(self.states()['1_10']['recorded'], 95)
        self.complete(pieces=95, reason='5 lost')
        self.assertEqual(self.states()['1_10']['state'], 'SHORT')

    def test_a_step_cannot_be_reopened_while_the_next_step_is_complete(self):
        self.complete()
        self.complete('1_11')
        with self.assertRaisesMessage(ValidationError, 'step 11'):
            completion.reopen_step(self.users['manager'], dataset_id=self.dataset.pk, factory_id=4, operation_key='1_10',
                                   reason='x')

    def test_the_limit_follows_a_short_step_before(self):
        self.complete(pieces=50, reason='short')
        self.assertEqual(self.states()['1_11']['limit'], 50)
        self.assertEqual(self.states()['1_10']['limit'], 100)

    def test_marking_a_complete_step_again_is_refused_not_a_crash(self):
        self.complete()
        with self.assertRaisesMessage(ValidationError, 'already complete'):
            self.complete()

    def test_only_the_lines_supervisor_or_the_manager_completes_a_step(self):
        with self.assertRaises(PermissionDenied):
            self.complete('2_10')  # line 102, not the supervisor's line
        for user in ('qc', 'maintenance', 'merchandiser', 'admin'):
            with self.assertRaises(PermissionDenied, msg=user):
                self.complete(user=user)
        self.complete('2_10', user='manager')
        self.assertEqual(self.states()['2_10']['state'], 'COMPLETE')

    def test_no_more_output_once_a_step_is_closed(self):
        self.complete(pieces=60, reason='order cut by the buyer')
        with self.assertRaises(ValidationError):
            self.out(hour=10, pieces=1)

    def test_the_manager_reopens_a_step_and_the_filled_pieces_go(self):
        self.out(pieces=30)
        self.complete()
        with self.assertRaises(PermissionDenied):
            completion.reopen_step(self.users['supervisor'], dataset_id=self.dataset.pk, factory_id=4, operation_key='1_10')
        completion.reopen_step(self.users['manager'], dataset_id=self.dataset.pk, factory_id=4, operation_key='1_10',
                               reason='marked by mistake')
        s = self.states()['1_10']
        self.assertEqual((s['state'], s['recorded']), ('IN_PROGRESS', 30))

    def test_defects_count_against_done_until_reworked(self):
        self.out(hour=8, pieces=50)
        check = services.record_check(self.users['qc'], dataset_id=self.dataset.pk, factory_id=4, line_id=44, day=0,
                                      operation_key='1_10', checked=20, defects=3, defect_type='Open seam')
        self.out(hour=9, pieces=50)
        s = self.states()['1_10']
        self.assertEqual((s['state'], s['good']), ('IN_PROGRESS', 97))
        completion.mark_reworked(self.users['qc'], check.pk)
        self.assertEqual(self.states()['1_10']['state'], 'COMPLETE')

    def test_a_broken_machine_pauses_its_step(self):
        self.out(pieces=10)
        self.issue(machine_id=901)
        self.assertEqual(self.states()['1_10']['state'], 'PAUSED')


class OrderTests(Base):
    def act(self, name, user, order='1', **kw):
        return getattr(completion, name)(self.users[user], dataset_id=self.dataset.pk, factory_id=4, order_id=order, **kw)

    def stage(self, order='1'):
        return completion.order_stage(self.dataset.pk, 4, order)

    def sew(self):
        self.complete()
        self.complete('1_11')

    def test_an_order_is_sewn_when_every_step_is_complete(self):
        self.complete()
        self.assertEqual(self.stage(), 'IN_PRODUCTION')
        self.complete('1_11')
        self.assertEqual(self.stage(), 'SEWN')
        self.assertEqual(self.stage('2'), 'NOT_STARTED')

    def test_the_order_journey_goes_in_order(self):
        self.sew()
        with self.assertRaises(ValidationError):
            self.act('pack_order', 'merchandiser', cartons=4)  # not inspected yet
        self.act('inspect_order', 'qc', result='PASS')
        with self.assertRaises(ValidationError):
            self.act('ship_order', 'merchandiser', carrier='Aramex Logistics')  # not packed yet
        self.act('pack_order', 'merchandiser', cartons=4)
        self.act('ship_order', 'merchandiser', carrier='Aramex Logistics', tracking='AX-1')
        self.assertEqual(self.stage(), 'SHIPPED')
        self.act('close_order', 'manager')
        row = OrderStatus.objects.get(order_id='1')
        self.assertEqual((row.stage, row.cartons, row.carrier, row.tracking), ('CLOSED', 4, 'Aramex Logistics', 'AX-1'))

    def test_a_failed_inspection_needs_a_note_and_can_be_redone(self):
        self.sew()
        with self.assertRaises(ValidationError):
            self.act('inspect_order', 'qc', result='FAIL')
        self.act('inspect_order', 'qc', result='FAIL', note='Hems uneven on 12 pieces')
        self.assertEqual(self.stage(), 'QC_FAILED')
        self.act('inspect_order', 'qc', result='PASS', note='Reworked')
        self.assertEqual(self.stage(), 'QC_PASSED')

    def test_each_order_stage_has_its_own_role(self):
        self.sew()
        for user in ('supervisor', 'merchandiser', 'maintenance'):
            with self.assertRaises(PermissionDenied, msg=user):
                self.act('inspect_order', user, result='PASS')
        self.act('inspect_order', 'qc', result='PASS')
        for user in ('qc', 'supervisor', 'planner'):
            with self.assertRaises(PermissionDenied, msg=user):
                self.act('pack_order', user, cartons=2)
        self.act('pack_order', 'manager', cartons=2)
        for user in ('merchandiser', 'qc', 'supervisor', 'admin'):
            with self.assertRaises(PermissionDenied, msg=user):
                self.act('close_order', user, reason='x')
        self.act('close_order', 'planner', reason='buyer took the packed goods at the gate')

    def test_closing_an_unfinished_order_needs_a_reason(self):
        with self.assertRaises(ValidationError):
            self.act('close_order', 'manager', order='2')
        self.act('close_order', 'manager', order='2', reason='Buyer cancelled the rest')
        self.assertEqual(self.stage('2'), 'CLOSED')
        with self.assertRaises(ValidationError):
            self.act('close_order', 'manager', order='99', reason='not in any plan')

    def test_reopening_an_order(self):
        self.sew()
        self.act('inspect_order', 'qc', result='PASS')
        self.act('pack_order', 'merchandiser', cartons=1)
        with self.assertRaises(PermissionDenied):
            self.act('reopen_order', 'merchandiser', reason='wrong cartons')
        self.act('reopen_order', 'manager', reason='wrong cartons')
        self.assertEqual(self.stage(), 'SEWN')
        self.act('close_order', 'manager', order='2', reason='cancelled')
        self.act('reopen_order', 'manager', order='2', reason='not cancelled after all')
        self.assertEqual(self.stage('2'), 'NOT_STARTED')

    def test_a_step_of_a_packed_order_cannot_be_reopened(self):
        self.sew()
        self.act('inspect_order', 'qc', result='PASS')
        self.act('pack_order', 'merchandiser', cartons=1)
        with self.assertRaises(ValidationError):
            completion.reopen_step(self.users['manager'], dataset_id=self.dataset.pk, factory_id=4, operation_key='1_11',
                                   reason='x')

    def test_reopening_a_step_of_a_sewn_order_puts_it_back_in_production(self):
        self.sew()
        completion.reopen_step(self.users['manager'], dataset_id=self.dataset.pk, factory_id=4, operation_key='1_11',
                               reason='miscounted')
        self.assertEqual(self.stage(), 'IN_PRODUCTION')

    def test_plan_ahead_and_the_simulator_treat_closed_work_as_done(self):
        from planning.autoplan import _progress
        from sim.runner import _done
        self.complete(pieces=50, reason='short')
        self.act('close_order', 'manager', order='2', reason='cancelled')
        self.assertEqual(_done(SimpleNamespace(dataset_id=self.dataset.pk, factory_id=4), load(self.run))['2_10'], 40)
        share, unfinished = _progress(self.run)
        self.assertEqual(unfinished, ['1'])  # only step 11 of order 1 is open
        self.complete('1_11', reason='follows step 10')
        self.assertEqual(_progress(self.run), (1.0, []))

    @override_settings(CRAFT_WRITEBACK=True)
    def test_progress_is_written_to_the_demo_copy(self):
        with mock.patch('erp.writer._write') as write, self.captureOnCommitCallbacks(execute=True):
            self.sew()
            self.act('inspect_order', 'qc', result='PASS')
            self.act('pack_order', 'merchandiser', cartons=3)
            self.act('ship_order', 'merchandiser', carrier='Aramex Logistics', tracking='AX-9')
        sql = ' | '.join(s for c in write.call_args_list for s, _p in c.args[3])
        params = [p for c in write.call_args_list for _s, p in c.args[3]]
        self.assertIn("execution_status = 'Done'", sql)
        self.assertIn("SET status = 'Completed'", sql)
        self.assertIn("delivery_status = 'Packing'", sql)
        self.assertIn("delivery_status = 'In_Transit'", sql)
        self.assertIn("po_status = 'Shipped'", sql)
        self.assertTrue(any('AX-9' in p for p in params))

    @override_settings(CRAFT_WRITEBACK=True)
    def test_reopening_a_step_is_written_to_the_demo_copy(self):
        with mock.patch('erp.writer._write') as write, self.captureOnCommitCallbacks(execute=True):
            self.sew()
            completion.reopen_step(self.users['manager'], dataset_id=self.dataset.pk, factory_id=4,
                                   operation_key='1_11', reason='miscounted')
        sql = ' | '.join(s for c in write.call_args_list[-1:] for s, _p in c.args[3])
        self.assertIn("execution_status = 'Scheduled'", sql)
        self.assertIn("SET status = 'Processing'", sql)
