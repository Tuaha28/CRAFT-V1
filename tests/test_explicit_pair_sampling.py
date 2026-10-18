"""Explicit-pair sampling must not hide every allowed pair behind independent prefixes."""
from dataclasses import replace
from types import SimpleNamespace

from crafter.execution_contract import OperationExecutionContract
from crafter.repair.resource_configs import _spread_explicit_pairs, generate_resource_configurations
from tests.test_stage_b_source_authoritative import state
from web_v1.audits.factory1_realistic_neighborhood_v1.run_bounded import route_ready_probes


def _cross_pairs_only(row):
    spec = row['source_resource_domain']['operations']['o_1']
    contract = OperationExecutionContract.from_mapping(spec['execution_contract'])
    spec['execution_contract'] = replace(
        contract, pairing_policy='EXPLICIT_PAIRS',
        allowed_worker_machine_pairs=(('w1', 20), ('w2', 10))).as_mapping()
    return row


def test_cap_one_finds_an_allowed_pair_when_prefixes_share_none():
    # Machine prefix (10,) and worker prefix ('w1',) form no allowed pair.
    configs, audit, summary = generate_resource_configurations(
        _cross_pairs_only(state()), max_machine_sets=1, max_worker_crews=1)
    pairs = {(c.worker_ids[0], c.machine_ids[0]) for c in configs['o_1']}
    assert len(pairs) == 1 and pairs <= {('w1', 20), ('w2', 10)}
    assert summary['zero_domains'] == []
    assert audit[0]['truncated'] is True


def test_explicit_pair_fill_is_a_superset_as_the_cap_grows():
    small, _, _ = generate_resource_configurations(
        _cross_pairs_only(state()), max_machine_sets=1, max_worker_crews=1)
    large, audit, _ = generate_resource_configurations(
        _cross_pairs_only(state()), max_machine_sets=2, max_worker_crews=2)
    assert set(small['o_1']) <= set(large['o_1'])
    assert {(c.worker_ids[0], c.machine_ids[0]) for c in large['o_1']} == {('w1', 20), ('w2', 10)}
    assert audit[0]['truncated'] is False


def test_spread_order_round_robins_lines_and_is_cap_independent():
    machines = {m: SimpleNamespace(line_id=line) for m, line in
                {1: 1, 2: 1, 3: 2, 4: 2, 5: 3}.items()}
    pairs = [('a', 1), ('b', 1), ('a', 2), ('c', 3), ('d', 4), ('e', 5)]
    ordered = _spread_explicit_pairs(pairs, machines, {1, 2, 3}, set(machines), set('abcde'))
    # First pass takes one fresh pair per line, in line order.
    assert [machines[m].line_id for m, _ in ordered[:3]] == [1, 2, 3]
    assert sorted(ordered) == sorted((m, w) for w, m in pairs)
    assert ordered == _spread_explicit_pairs(list(reversed(pairs)), machines, {1, 2, 3},
                                             set(machines), set('abcde'))


def test_spread_drops_pairs_outside_pools_or_eligible_lines():
    machines = {1: SimpleNamespace(line_id=1), 2: SimpleNamespace(line_id=9)}
    ordered = _spread_explicit_pairs([('a', 1), ('a', 2), ('z', 1)], machines, {1}, {1, 2}, {'a'})
    assert ordered == [(1, 'a')]


def test_route_ready_probe_tolerates_an_operation_with_zero_configurations():
    # Factories 2 and 5 crashed here with "min() iterable argument is empty".
    problem = SimpleNamespace(operation_by_key={'o_1': SimpleNamespace(sequence=1)})
    probe_state = {'after_index': SimpleNamespace(route_predecessor={}), 'problem': problem,
                   'resource_configurations_for_probes': {'o_1': ()}}
    probes, earliest_end = route_ready_probes(probe_state, {}, {}, ('o_1',))
    assert probes == {'o_1': ()}
    assert earliest_end == {'o_1': 0}
