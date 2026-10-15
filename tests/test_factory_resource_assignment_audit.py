from datetime import datetime
from unittest import TestCase

from web_v1.audits.factory_resource_assignments_v1.audit import overlaps, simultaneous, segments, on_model_calendar, lot_precedence_errors


def at(hour, minute=0):
    return datetime(2026, 9, 9, hour, minute)


class AssignmentAuditTests(TestCase):
    def test_touching_intervals_are_not_conflicts(self):
        self.assertEqual(overlaps([(at(6), at(7), 'a'), (at(7), at(8), 'b')])['overlap_pairs'], 0)

    def test_nested_overlaps_count_every_pair(self):
        result = overlaps([(at(6), at(10), 'a'), (at(7), at(9), 'b'), (at(8), at(10), 'c')])
        self.assertEqual(result['overlap_pairs'], 3)

    def test_distinct_resources_not_segment_count(self):
        result = simultaneous([(at(6),at(8),1,10,'a'), (at(7),at(9),1,11,'b')])
        self.assertEqual(result, {'workers':1, 'machines':2, 'lots':2})

    def test_future_segments_are_never_history(self):
        row = {'actual_work_segments': [[at(6).isoformat(),at(8).isoformat()], [at(9).isoformat(),at(10).isoformat()]]}
        self.assertEqual(segments(row, at(7)), [(at(6),at(7))])

    def test_invalid_segment_rejected(self):
        with self.assertRaisesRegex(ValueError, 'INVALID_SEGMENT'):
            segments({'actual_work_segments': [[at(8).isoformat(),at(6).isoformat()]]})

    def test_static_calendar_checks_internal_off_shift_gap(self):
        schedules = {1:[{'start_time':{'seconds':21600}, 'end_time':{'seconds':50400}}]}
        self.assertTrue(on_model_calendar(at(6),at(14),1,{}, {},schedules))
        self.assertFalse(on_model_calendar(at(6),datetime(2026,9,10,10),1,{}, {},schedules))

    def test_multiple_batch_memberships_not_silently_selected(self):
        self.assertIsNone(on_model_calendar(at(6),at(7),1,{1:{10,11}}, {},{1:[{}]}))

    def test_lot_transfer_does_not_require_entire_operation_finished(self):
        pred = [{'lot_index':0,'lot_quantity':20,'actual_end_time':at(7).isoformat()},
                {'lot_index':1,'lot_quantity':20,'actual_end_time':at(10).isoformat()}]
        succ = [{'lot_index':0,'lot_quantity':20,'actual_start_time':at(8).isoformat(),'schedule_item_id':5}]
        self.assertEqual(lot_precedence_errors(pred, succ), [])
        succ[0]['actual_start_time'] = at(6).isoformat()
        self.assertEqual(lot_precedence_errors(pred, succ), ['5'])

    def test_lot_transfer_rejects_quantity_mismatch(self):
        pred = [{'lot_index':0,'lot_quantity':20,'actual_end_time':at(7).isoformat()}]
        succ = [{'lot_index':0,'lot_quantity':19,'actual_start_time':at(8).isoformat(),'schedule_item_id':6}]
        self.assertEqual(lot_precedence_errors(pred, succ), ['6'])

    def test_lot_transfer_rejects_missing_predecessor_lot(self):
        pred = [{'lot_index':0,'lot_quantity':20,'actual_end_time':at(7).isoformat()}]
        succ = [{'lot_index':1,'lot_quantity':20,'actual_start_time':at(8).isoformat(),'schedule_item_id':7}]
        self.assertEqual(lot_precedence_errors(pred, succ), ['7'])

    def test_future_successor_is_not_claimed_as_historical(self):
        pred = [{'lot_index':0,'lot_quantity':20,'actual_end_time':at(10).isoformat()}]
        succ = [{'lot_index':0,'lot_quantity':20,'actual_start_time':at(8).isoformat(),'schedule_item_id':8}]
        self.assertEqual(lot_precedence_errors(pred, succ, reference=at(7)), [])
