import pytest

from web_v1.audits.factory46_operation_schedule_v1.run_factory_test import (
    _application_engine,
    _select_routes,
    _split_failed_window,
    _verify_resume_accounting,
)


def _routes(count, route_length=6):
    return [
        (f"2026-01-{index + 1:02d}", str(index),
         [{"operation_key": f"{index}_{step}", "routing_sequence": step}
          for step in range(route_length)],
         {"production_order_id": index})
        for index in range(count)
    ]


def test_ten_operation_probe_is_breadth_first_and_route_prefix_closed():
    selected = _select_routes(_routes(8), "10")
    assert sum(len(route) for route, _order in selected) == 10
    assert len(selected) == 8
    assert [len(route) for route, _order in selected] == [2, 2, 1, 1, 1, 1, 1, 1]
    for route, _order in selected:
        assert [row["routing_sequence"] for row in route] == list(range(len(route)))


def test_twenty_five_operation_probe_never_exceeds_source_route_lengths():
    selected = _select_routes(_routes(6), "25")
    assert sum(len(route) for route, _order in selected) == 25
    assert [len(route) for route, _order in selected] == [5, 4, 4, 4, 4, 4]
    assert all(len(route) <= 25 for route, _order in selected)


def test_all_probe_keeps_complete_source_routes():
    routes = _routes(3, route_length=4)
    selected = _select_routes(routes, "all")
    assert [len(route) for route, _order in selected] == [4, 4, 4]


def test_runner_forbids_application_database_connections():
    with pytest.raises(RuntimeError, match="DATABASE_CONNECTION_FORBIDDEN_CAPTURE_ONLY_RUNNER"):
        _application_engine.connect()


@pytest.mark.parametrize("size", range(1, 26))
def test_failed_window_reduction_preserves_order_identity_and_terminates(size):
    keys = tuple(str(index) for index in range(size))
    smaller = _split_failed_window(keys)
    if size == 1:
        assert smaller == []
    else:
        assert tuple(key for chunk in smaller for key in chunk) == keys
        assert all(0 < len(chunk) < size for chunk in smaller)
        assert max(map(len, smaller)) <= 2


def _resume_payload():
    return {'selected_schedule': [{'operation_key': 'a'}],
            'pending_windows': [['b']], 'deferred_operations': {'c': {'reason': 'blocked'}},
            'windows': [{'accepted': True, 'operation_keys': ['a']}]}


def test_restore_accounts_for_pending_deferred_and_accepted():
    _verify_resume_accounting(_resume_payload(), ('a', 'b', 'c'))


@pytest.mark.parametrize('mutation', ['duplicate', 'missing', 'accepted_again', 'unrecorded'])
def test_restore_rejects_invalid_ledger(mutation):
    payload = _resume_payload()
    if mutation == 'duplicate':
        payload['selected_schedule'].append({'operation_key': 'a'})
    elif mutation == 'missing':
        payload['pending_windows'] = []
    elif mutation == 'accepted_again':
        payload['pending_windows'] = [['a', 'b']]
    else:
        payload['windows'] = []
    with pytest.raises(ValueError, match='CHECKPOINT_.*ACCOUNTING'):
        _verify_resume_accounting(payload, ('a', 'b', 'c'))


def test_saved_source_schedule_is_replayed_and_duration_tampering_rejected():
    import json
    from copy import deepcopy
    from web_v1.audits.factory46_operation_schedule_v1.run_factory_test import (
        HERE, _source, _eligible_order_routes, _prepare, _validate_assembled_trial,
    )
    source, readiness, snapshot, source_hash = _source(6)
    routes = _select_routes(_eligible_order_routes(source, snapshot), '10')
    state = _prepare(6, source, readiness, snapshot, source_hash, routes)
    evidence = json.loads((HERE / 'factory6_10_execution_contract_v1_20261013T204044877490Z.json').read_text())
    rows = evidence['selected_schedule']
    assert _validate_assembled_trial(state, rows, state['boundary_facts'])['accepted']
    invalid = deepcopy(rows)
    invalid[0]['end_seconds'] = invalid[0]['start_seconds'] + 1
    assert not _validate_assembled_trial(state, invalid, state['boundary_facts'])['accepted']
    missing = [row for row in rows if row['operation_key'] != '274_343']
    rejected = _validate_assembled_trial(state, missing, state['boundary_facts'])
    assert not rejected['accepted']
    assert rejected['scope_status'] == 'SCOPE_INVALID'
    assert rejected['missing_source_predecessors'] == {'274_344': '274_343'}
