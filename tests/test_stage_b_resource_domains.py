"""Small exhaustive oracles for Stage-B legal alternative coverage."""
from itertools import combinations
from types import SimpleNamespace
from datetime import datetime

import pytest

from crafter.repair import resource_configs as resource
from crafter.lns_probe.joint_modes import _fixed_crew_execution
from utils.work_calendar import NoAvailableCalendar


def _state(*, crew=2, workers=('w1', 'w2'), machine_count=40):
    machines = {mid: SimpleNamespace(factory_id=1, line_id=1)
                for mid in range(1, machine_count + 1)}
    problem = SimpleNamespace(
        operation_by_key={'op': SimpleNamespace(order_id='order', required_capability='sewing',
                                                 eligible_line_ids=(1,))},
        order_by_id={'order': SimpleNamespace(factory_id=1)},
        line_by_id={1: SimpleNamespace(factory_id=1)}, machine_by_id=machines)
    operator = {'by_line': {(1, 'sewing'): tuple(workers)},
                'by_factory': {(1, 'sewing'): tuple(workers)},
                'crew_of': {worker: (index, index) for index, worker in enumerate(workers)}}
    state = {'problem': problem,
             'after_index': SimpleNamespace(by_key={'op': {
                 'machine_ids': (), 'employee_ids': (), 'line_id': 1}}),
             'operator_index': operator, 'keys': ('op',), 'event': None,
             'op_details': {'op': {}}}
    return state


def _patch_sources(monkeypatch, *, crew, machine_count):
    monkeypatch.setattr(resource, '_candidate_domains',
                        lambda *_args, **_kwargs: {'op': ({'line_id': 1, 'k': crew},)})
    monkeypatch.setattr(resource, '_available_machine_pool',
                        lambda *_args: tuple(range(1, machine_count + 1)))
    monkeypatch.setattr(resource, 'authoritative_duration_contract',
                        lambda *_args: SimpleNamespace(productive_seconds=600,
                                                        productive_minutes=10.0))
    monkeypatch.setattr(resource, 'authoritative_setup_seconds', lambda *_args: 0)


def test_machine_beyond_old_pool_cap_and_mixed_calendar_crew_are_exposed(monkeypatch):
    _patch_sources(monkeypatch, crew=2, machine_count=40)
    state = _state()
    configs, audit, _ = resource.generate_resource_configurations(
        state, max_machine_sets=2, max_worker_crews=1, machine_pool_limit=30)
    assert [row.machine_ids for row in configs['op']] == [(1, 2), (39, 40)]
    assert all(row.worker_ids == ('w1', 'w2') for row in configs['op'])
    domain = audit[0]['domain_rows'][0]
    assert domain['total_legal_machine_sets'] == 780
    assert domain['total_legal_worker_crews'] == 1
    assert domain['machine_pool_complete'] is True
    assert domain['worker_pool_complete'] is True
    assert domain['more_machine_sets_available'] is True
    assert domain['more_worker_crews_available'] is False


def test_expansion_preserves_prefix_and_reaches_small_exhaustive_oracle(monkeypatch):
    _patch_sources(monkeypatch, crew=2, machine_count=4)
    state = _state(workers=('w1', 'w2', 'w3'), machine_count=4)
    compact, _, _ = resource.generate_resource_configurations(
        state, max_machine_sets=1, max_worker_crews=1)
    expanded, audit, _ = resource.generate_resource_configurations(
        state, max_machine_sets=6, max_worker_crews=3)
    assert set(compact['op']) <= set(expanded['op'])
    machine_oracle = {tuple(pair) for pair in combinations(range(1, 5), 2)}
    worker_oracle = {tuple(pair) for pair in combinations(('w1', 'w2', 'w3'), 2)}
    assert {row.machine_ids for row in expanded['op']} == machine_oracle
    assert {row.worker_ids for row in expanded['op']} == worker_oracle
    assert len(expanded['op']) == 18
    assert audit[0]['truncated'] is False


def test_skill_and_factory_filters_do_not_admit_unlisted_resources(monkeypatch):
    _patch_sources(monkeypatch, crew=1, machine_count=3)
    state = _state(crew=1, workers=('qualified',), machine_count=3)
    state['problem'].machine_by_id[3] = SimpleNamespace(factory_id=2, line_id=1)
    state['operator_index']['by_line'][(1, 'sewing')] += ('unskilled',)
    configs, audit, _ = resource.generate_resource_configurations(
        state, max_machine_sets=3, max_worker_crews=3)
    assert {row.machine_ids for row in configs['op']} == {(1,), (2,)}
    assert {row.worker_ids for row in configs['op']} == {('qualified',)}
    assert audit[0]['truncated'] is False


def test_mixed_calendar_crew_executes_only_during_shared_shift():
    ready = datetime(2026, 1, 1, 8)
    index = {'shift_of': {'w1': (6, 14), 'w2': (10, 18)}}
    end, segments = _fixed_crew_execution(
        SimpleNamespace(), index, ('w1', 'w2'), ready, 60, {})
    assert end == datetime(2026, 1, 1, 11)
    assert segments == ((datetime(2026, 1, 1, 10), end),)


def test_disjoint_mixed_calendar_crew_is_not_falsely_executable():
    ready = datetime(2026, 1, 1, 8)
    index = {'shift_of': {'w1': (6, 14), 'w2': (14, 22)}}
    with pytest.raises(NoAvailableCalendar):
        _fixed_crew_execution(SimpleNamespace(), index, ('w1', 'w2'), ready, 60, {})
