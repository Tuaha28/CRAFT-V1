"""Operation accounting and restart guards for tentative bounded cycles."""
from copy import deepcopy

import pytest

from crafter.production.cycles import new_checkpoint, next_window, policy, record_window, verify_checkpoint


KEYS = ('A1', 'A2', 'B1', 'B2')
PREDECESSORS = {'A1': None, 'A2': 'A1', 'B1': None, 'B2': 'B1'}
OPTIONS = {'cycle_target': 2, 'window_size': 1}


def test_cycle_target_and_window_limit_are_separate():
    assert policy({'cycle_target': 200, 'window_size': 25}) == {
        'cycle_target': 200, 'window_size': 25}
    with pytest.raises(ValueError, match='CYCLE_WINDOW_LIMIT'):
        policy({'cycle_target': 200, 'window_size': 26})


def _accepted(key):
    return {'keys': [key], 'status': 'ACCEPTED', 'replay': 'PASS',
            'physical': 'VALID', 'scope': 'SCOPE_VALID',
            'materialization_mismatch_count': 0,
            'selected_schedule': [{'operation_key': key}]}


def test_two_cycles_resume_and_conserve_every_identity():
    cp = new_checkpoint(factory_id=1, input_fingerprint='source-v1',
                        operation_keys=KEYS, options=OPTIONS)
    cycles = []
    for _ in KEYS:
        verify_checkpoint(cp, factory_id=1, input_fingerprint='source-v1',
                          operation_keys=KEYS, options=OPTIONS)
        cp, selected = next_window(cp, predecessors=PREDECESSORS)
        assert len(selected) == 1
        cp = record_window(cp, _accepted(selected[0]), independent_status='PASS')
        cycles.append(cp['windows'][-1]['cycle_number'])
    assert cycles == [1, 1, 2, 2]
    assert cp['accepted_keys'] == sorted(KEYS)
    assert cp['status'] == 'COMPLETE'
    assert cp['approval_status'] == 'TENTATIVE_NOT_APPROVED'
    assert len({row['keys'][0] for row in cp['windows']}) == len(KEYS)


def test_stale_mutated_duplicate_and_unvalidated_checkpoint_fail_closed():
    cp = new_checkpoint(factory_id=1, input_fingerprint='source-v1',
                        operation_keys=KEYS, options=OPTIONS)
    cp = record_window(cp, _accepted('A1'), independent_status='PASS')
    with pytest.raises(ValueError, match='CYCLE_CHECKPOINT_STALE'):
        verify_checkpoint(cp, factory_id=1, input_fingerprint='source-v2',
                          operation_keys=KEYS, options=OPTIONS)
    modified = deepcopy(cp)
    modified['accepted_keys'] = []
    with pytest.raises(ValueError, match='CYCLE_CHECKPOINT_HASH'):
        verify_checkpoint(modified, factory_id=1, input_fingerprint='source-v1',
                          operation_keys=KEYS, options=OPTIONS)
    with pytest.raises(ValueError, match='CYCLE_DUPLICATE_OR_INVALID_WINDOW'):
        record_window(cp, _accepted('A1'), independent_status='PASS')
    with pytest.raises(ValueError, match='CYCLE_INDEPENDENT_VALIDATION_REQUIRED'):
        record_window(cp, _accepted('A2'), independent_status='FAIL')


def test_failed_route_is_deferred_and_unrelated_work_remains_selectable():
    cp = new_checkpoint(factory_id=1, input_fingerprint='source-v1',
                        operation_keys=KEYS, options=OPTIONS)
    cp = record_window(cp, {'keys': ['A1'], 'status': 'REJECTED',
                            'failure_reason': 'RESOURCE_DOMAIN_EXPANSION_LIMIT'},
                       independent_status='NOT_REACHED')
    cp, selected = next_window(cp, predecessors=PREDECESSORS)
    assert selected == ('B1',)
    assert cp['deferred']['A1'] == 'RESOURCE_DOMAIN_EXPANSION_LIMIT'
    cp = record_window(cp, _accepted('B1'), independent_status='PASS')
    cp, selected = next_window(cp, predecessors=PREDECESSORS)
    assert selected == ('B2',)
    cp = record_window(cp, _accepted('B2'), independent_status='PASS')
    cp, selected = next_window(cp, predecessors=PREDECESSORS)
    assert selected == ()
    assert cp['status'] == 'PARTIAL'
    assert cp['deferred']['A2'] == 'PREDECESSOR_UNRESOLVED:A1'
    assert cp['accepted_keys'] == ['B1', 'B2']


def test_large_cycle_accounting_never_exceeds_bounded_window():
    # Pure coordinator contract only: this does not claim CP-SAT scheduled
    # 202 source operations or that their resource domains are admissible.
    keys = tuple(f'operation_{i:03d}' for i in range(202))
    options = {'cycle_target': 200, 'window_size': 25}
    cp = new_checkpoint(factory_id=1, input_fingerprint='contract-test',
                        operation_keys=keys, options=options)
    for _ in range(9):
        cp, selected = next_window(cp, predecessors={})
        assert 1 <= len(selected) <= 25
        cp = record_window(cp, {'keys': list(selected), 'status': 'ACCEPTED',
                                'replay': 'PASS', 'physical': 'VALID',
                                'scope': 'SCOPE_VALID', 'materialization_mismatch_count': 0,
                                'selected_schedule': [
                                    {'operation_key': key} for key in selected]},
                           independent_status='PASS')
    assert cp['status'] == 'COMPLETE'
    assert cp['accepted_keys'] == sorted(keys)
    assert [row['cycle_number'] for row in cp['windows']] == [1] * 8 + [2]
    assert [len(row['keys']) for row in cp['windows']] == [25] * 8 + [2]
