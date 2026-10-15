from types import SimpleNamespace
from ortools.sat.python import cp_model

from crafter.repair.cp_repair import build_repair_model
from crafter.repair.engine import _routing_aware_earliest
from crafter.repair.resource_configs import ResourceConfiguration


def _state_and_regimes():
    keys = ('a_1', 'b_2')
    index = SimpleNamespace(
        route_successor={'a_1': 'b_2', 'b_2': 'c_3'},
        route_predecessor={'a_1': 'd_0', 'b_2': 'a_1'},
    )
    def assignment(key, start, end):
        return SimpleNamespace(operation_key=key, start_seconds=start, end_seconds=end)
    baseline = SimpleNamespace(operations=(
        SimpleNamespace(assignment=assignment('c_3', 500, 600)),
        SimpleNamespace(assignment=assignment('d_0', 0, 25)),
    ))
    state = {
        'keys': keys,
        'problem': SimpleNamespace(horizon_seconds=1000),
        'after_index': index,
        'baseline': baseline,
        'event': SimpleNamespace(metadata={}),
        # Non-empty dictionaries prevent fallback construction; no resources
        # are involved in this routing-only fixture.
        '_machine_res': {999: []},
        '_worker_res': {'none': []},
    }
    regimes = {}
    for key in keys:
        cfg = ResourceConfiguration(key, 1, 1, 1, (), (), 0, 10, ('TEST',))
        regimes[key] = ({
            'key': key, 'config': cfg, 'base': cfg,
            'lo': 0, 'base_lo': 0, 'hi': 900,
            'formula': ((1, 10),), 'segments': 0,
        },)
    return state, regimes


def _edges(artifact, category=None):
    rows = artifact['routing_constraint_records']
    if category is not None:
        rows = [row for row in rows if row['category'] == category]
    return {tuple(row['edge']) for row in rows}


def test_no_internal_routing_never_falls_through_to_outside():
    state, regimes = _state_and_regimes()
    artifact = build_repair_model(state, regimes, include_internal_routing=False)
    assert _edges(artifact, 'INTERNAL_ROUTING') == set()
    assert ('a_1', 'b_2') not in _edges(artifact, 'OUTSIDE_ROUTING')
    assert _edges(artifact, 'OUTSIDE_ROUTING') == {('b_2', 'c_3'), ('d_0', 'a_1')}


def test_excluding_exact_internal_or_outside_edge_removes_only_that_edge():
    state, regimes = _state_and_regimes()
    full = build_repair_model(state, regimes)
    assert _edges(full) == {('a_1', 'b_2'), ('b_2', 'c_3'), ('d_0', 'a_1')}

    no_internal = build_repair_model(state, regimes, exclude_routing_edges={('a_1', 'b_2')})
    assert ('a_1', 'b_2') not in _edges(no_internal)
    assert _edges(no_internal) == {('b_2', 'c_3'), ('d_0', 'a_1')}

    no_out_successor = build_repair_model(state, regimes, exclude_routing_edges={('b_2', 'c_3')})
    assert ('b_2', 'c_3') not in _edges(no_out_successor)
    assert _edges(no_out_successor) == {('a_1', 'b_2'), ('d_0', 'a_1')}

    no_out_predecessor = build_repair_model(state, regimes, exclude_routing_edges={('d_0', 'a_1')})
    assert ('d_0', 'a_1') not in _edges(no_out_predecessor)
    assert _edges(no_out_predecessor) == {('a_1', 'b_2'), ('b_2', 'c_3')}


def test_route_aware_earliest_propagates_internal_completion():
    baseline = SimpleNamespace(operations=(SimpleNamespace(assignment=SimpleNamespace(
        operation_key='c_3', start_seconds=70, end_seconds=80)),))
    state = {
        'keys': ('a_1', 'b_2'), 'baseline': baseline,
        'after_index': SimpleNamespace(
            route_predecessor={'a_1': None, 'b_2': 'a_1'},
            route_successor={'a_1': 'b_2', 'b_2': 'c_3'}),
    }
    regimes = {
        'a_1': ({'lo':40, 'hi':100, 'formula':((1,20),)},),
        # Standalone minimum end is 20, but this start is unreachable after A.
        'b_2': ({'lo':0, 'hi':100, 'formula':((1,20),)},),
    }
    bounds = _routing_aware_earliest(state, regimes)
    assert bounds['earliest_end']['a_1'] == 60
    assert bounds['earliest_start']['b_2'] == 60
    assert bounds['earliest_end']['b_2'] == 80


def test_event_delayed_predecessor_bound_matches_cp_and_closure():
    state, regimes = _state_and_regimes()
    state['event'] = SimpleNamespace(metadata={
        'predecessor_operation_key': 'a_1',
        'delayed_end_seconds': 70,
    })
    bounds = _routing_aware_earliest(state, regimes)
    assert bounds['earliest_start']['a_1'] == 60
    assert bounds['earliest_end']['a_1'] == 70
    assert bounds['earliest_start']['b_2'] == 70

    artifact = build_repair_model(state, regimes, include_outside_routing=False)
    assert artifact['event_constraint_records'] == [{
        'category': 'EVENT_DELAYED_PREDECESSOR_END',
        'operation_key': 'a_1',
        'lower_bound_seconds': 70,
    }]
    artifact['model'].Minimize(artifact['ends']['a_1'])
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    assert solver.Solve(artifact['model']) == cp_model.OPTIMAL
    assert solver.Value(artifact['ends']['a_1']) == 70
    assert solver.Value(artifact['starts']['b_2']) >= 70


def test_affine_optional_intervals_validate_and_preserve_exact_execution():
    """The compressed interval encoding keeps the same affine endpoints."""
    state, _ = _state_and_regimes()
    state['keys'] = ('a_1',)
    state['after_index'] = SimpleNamespace(
        route_successor={'a_1': None}, route_predecessor={'a_1': None})
    state['baseline'] = SimpleNamespace(operations=())
    cfg = ResourceConfiguration('a_1', 1, 1, 1, (7,), ('w1',), 2, 8, ('TEST',))
    regimes = {'a_1': ({
        'key': 'a_1', 'config': cfg, 'base': cfg,
        'lo': 10, 'base_lo': 10, 'hi': 20,
        # end=start+14; productive worker intervals are [start+2,start+6]
        # and [start+10,start+14].
        'formula': ((1, 14), (1, 2), (1, 6), (1, 10), (1, 14)),
        'segments': 2,
    },)}
    artifact = build_repair_model(state, regimes)
    assert artifact['model'].Validate() == ''

    for start in (10, 11, 15, 19, 20):
        probe = build_repair_model(state, regimes)
        probe['model'].Add(probe['starts']['a_1'] == start)
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        assert solver.Solve(probe['model']) in (cp_model.FEASIBLE, cp_model.OPTIMAL)
        assert solver.Value(probe['ends']['a_1']) == start + 14


def test_outside_routing_boundaries_project_float_instants_upward():
    state, regimes = _state_and_regimes()
    # Persisted decoder values can sit infinitesimally below an integer.  The
    # canonical CP boundary must agree with datetime validation and may never
    # floor such a predecessor completion.
    predecessor = state['baseline'].operations[1].assignment
    object.__setattr__(predecessor, 'end_seconds', 25.999999999)
    artifact = build_repair_model(state, regimes)
    records = {tuple(row['edge']): row for row in artifact['routing_constraint_records']}
    assert records[('d_0', 'a_1')]['fixed_boundary_seconds'] == 26
    artifact['model'].Add(artifact['starts']['a_1'] == 25)
    solver = cp_model.CpSolver()
    assert solver.Solve(artifact['model']) == cp_model.INFEASIBLE


def test_committed_cross_window_predecessor_uses_carried_exact_end():
    """A rolling boundary is authoritative over its pre-window row."""
    state, regimes = _state_and_regimes()
    state['committed_routing_completion'] = {
        'd_0': {'authoritative_end_seconds': 33},
    }
    artifact = build_repair_model(state, regimes)
    records = {tuple(row['edge']): row for row in artifact['routing_constraint_records']}
    assert records[('d_0', 'a_1')]['fixed_boundary_seconds'] == 33
    artifact['model'].Add(artifact['starts']['a_1'] == 32)
    solver = cp_model.CpSolver()
    assert solver.Solve(artifact['model']) == cp_model.INFEASIBLE


def test_initial_planning_does_not_freeze_future_outside_benchmark_rows():
    state, regimes = _state_and_regimes()
    state['planning_context'] = {'mode': 'INITIAL_PLAN', 'factory_id': 1}
    state['classified'] = {
        'c_3': {'state': 'FUTURE_OUTSIDE_WINDOW'},
        'd_0': {'state': 'FUTURE_OUTSIDE_WINDOW'},
    }
    artifact = build_repair_model(state, regimes)
    assert _edges(artifact, 'OUTSIDE_ROUTING') == set()
