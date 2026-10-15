"""Retry accounting only; physical solver integration is tested separately."""
import pytest

from crafter.production.cycles import new_checkpoint, record_window, seal, verify_checkpoint
from web_v1.audits.factory1_realistic_neighborhood_v1.validate_bounded import validate_adaptive_trace


IDENTITY = dict(factory_id=1, input_fingerprint='pinned', operation_keys=list('abcdef'),
                options=dict(cycle_target=4, window_size=4, adaptive_retries=True))


def attempt(cp, accepted):
    keys = cp['pending_windows'][0]['keys']
    row = dict(keys=keys, status='ACCEPTED' if accepted else 'REJECTED',
               selected_schedule=[{'operation_key': k} for k in keys] if accepted else [],
               replay='PASS', physical='VALID', scope='SCOPE_VALID',
               materialization_mismatch_count=0, failure_reason='COMPUTATIONAL_LIMIT')
    return record_window(cp, row, independent_status='PASS' if accepted else 'NOT_REACHED')


def test_retry_then_resume_keeps_cycle_ownership_and_exact_accounting():
    cp = attempt(new_checkpoint(**IDENTITY), False)
    assert [p['keys'] for p in cp['pending_windows']] == [list('ab'), list('cd'), list('ef')]
    for _ in range(3):
        verify_checkpoint(cp, **IDENTITY)
        cp = attempt(cp, True)
    assert cp['status'] == 'COMPLETE'
    assert cp['accepted_keys'] == list('abcdef')
    assert [w['cycle_number'] for w in cp['windows']] == [1, 1, 1, 2]
    assert cp['approval_status'] == 'TENTATIVE_NOT_APPROVED'
    verify_checkpoint(cp, **IDENTITY)
    payload = dict(attempt_operation_keys=list('abcdef'), window_size=4, cycle_target=4,
                   attempt_trace=cp['windows'], selected_keys=list('abcdef'))
    assert validate_adaptive_trace(payload, list('abcdef')) == ([tuple('ab'), tuple('cd'), tuple('ef')], [])
    payload['attempt_trace'] = payload['attempt_trace'][1:]
    assert validate_adaptive_trace(payload, list('abcdef'))[1] == ['ADAPTIVE_TRACE_ORDER']


def test_terminal_failures_are_finite_and_do_not_erase_independent_work():
    cp = new_checkpoint(**IDENTITY)
    while cp['pending_windows']:
        cp = attempt(cp, cp['pending_windows'][0]['keys'] == list('ef'))
        assert cp['revision'] <= 8
    assert cp['status'] == 'PARTIAL'
    assert cp['accepted_keys'] == list('ef')
    assert set(cp['deferred']) == set('abcd')
    verify_checkpoint(cp, **IDENTITY)


@pytest.mark.parametrize('field,value', [('pending_windows', []), ('deferred', {'a': 'lost'}),
                                        ('accepted_keys', ['a'])])
def test_resealed_ledger_tampering_fails(field, value):
    cp = attempt(new_checkpoint(**IDENTITY), False)
    cp[field] = value
    with pytest.raises(ValueError, match='CYCLE_ATTEMPT_LEDGER_MISMATCH'):
        verify_checkpoint(seal(cp), **IDENTITY)


def test_rejected_attempt_cannot_reserve_resources():
    cp = new_checkpoint(**IDENTITY)
    with pytest.raises(ValueError, match='CYCLE_REJECTED_ASSIGNMENTS'):
        record_window(cp, dict(keys=list('abcd'), status='REJECTED',
                               selected_schedule=[{'operation_key': 'a'}]), independent_status='PASS')


def test_retry_preserves_source_fingerprint():
    cp = attempt(new_checkpoint(**IDENTITY), False)
    with pytest.raises(ValueError, match='CYCLE_CHECKPOINT_STALE'):
        verify_checkpoint(cp, **{**IDENTITY, 'input_fingerprint': 'changed'})


@pytest.mark.parametrize('change,error', [
    ({'attempt_operation_keys': ['unknown']}, 'ADAPTIVE_TRACE_POLICY_OR_IDENTITY'),
    ({'selected_keys': ['a']}, 'ADAPTIVE_ACCEPTED_ACCOUNTING'),
    ({'attempt_trace': [{'keys': list('abcd'), 'status': 'REJECTED',
                        'selected_schedule': [{'operation_key': 'a'}], 'failure_reason': 'limit'}]},
     'ADAPTIVE_REJECTED_COMMITMENT_OR_REASON'),
    ({'attempt_trace': [{'keys': list('abcd'), 'status': 'REJECTED'}]},
     'ADAPTIVE_REJECTED_COMMITMENT_OR_REASON'),
])
def test_independent_trace_rejects_missing_identity_and_false_reservations(change, error):
    payload = dict(attempt_operation_keys=list('abcdef'), window_size=4, cycle_target=4,
                   attempt_trace=[], selected_keys=[])
    payload.update(change)
    assert validate_adaptive_trace(payload, list('abcdef'))[1] == [error]
