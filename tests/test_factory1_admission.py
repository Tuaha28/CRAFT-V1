"""The captured global ledger is not itself a schedulable resource domain."""
from collections import Counter

from web_v1.backend.integrations.craft.factory1_test_state import admission_blocker, pinned_base


def test_factory1_global_ledger_explains_unadmitted_work():
    base = pinned_base()
    assert len(base['operation_ledger']) == 2879
    assert len(base['selected_keys']) == 25
    assert set(base['operation_ledger']) == set(base['execution_states']) == set(base['schedule_disposition'])
    assert Counter(base['execution_states'].values()) == {
        'FULLY_COMPLETED': 171, 'PARTIALLY_EXECUTED': 82, 'UNSTARTED': 2626}
    assert all(row['completed'] + row['in_flight'] + row['remaining'] == row['required']
               for row in base['operation_ledger'].values())
    selected = set(base['selected_keys'])
    completed = next(key for key in base['completed_keys'] if key not in selected)
    partial = next(key for key in base['production_admission_blocked_keys']
                   if key not in selected)
    unstarted = next(key for key, status in base['execution_states'].items()
                     if status == 'UNSTARTED' and key not in selected)
    assert admission_blocker(base, completed) == 'COMPLETED_WORK_MUST_NOT_BE_RESCHEDULED'
    assert admission_blocker(base, partial) == 'REAL_PARTIAL_CONTINUATION_EVIDENCE_MISSING'
    assert admission_blocker(base, unstarted) == 'SOURCE_EXECUTION_DOMAIN_NOT_INDEPENDENTLY_VALIDATED'
    assert admission_blocker(base, 'not-an-operation') == 'UNKNOWN_OPERATION_IDENTITY'
    selected_unstarted = next(key for key in base['selected_keys']
                              if base['execution_states'][key] == 'UNSTARTED')
    assert admission_blocker(base, selected_unstarted) is None
