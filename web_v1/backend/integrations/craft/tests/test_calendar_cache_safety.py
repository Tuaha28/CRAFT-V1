from dataclasses import replace
from types import SimpleNamespace
from datetime import timedelta
from crafter.repair import calendar_regimes as c
from crafter.repair.resource_configs import ResourceConfiguration
from crafter.lns_probe.joint_modes import _fixed_crew_execution
from .test_residual_execution import fixture


def test_uncached_execution_never_builds_unused_global_signature(monkeypatch):
    p, _, index = fixture()
    state = {'problem': p, 'operator_index': index, '_worker_res': {}}
    config = ResourceConfiguration('1_2', 1, 1, 1, (1,), ('1',), 0, 600, (), 10)
    expected = c._execution(state, config, 0, cache=c.CalendarRegimeCache())
    def forbidden(_):
        raise AssertionError('Uncached replay must not fingerprint all factory reservations')
    monkeypatch.setattr(c, '_worker_calendar_signature', forbidden)
    assert c._execution(state, config, 0) == expected


def test_duration_change_invalidates_breakpoint_cache():
    p, _, index = fixture()
    state = {'problem': p, 'operator_index': index, '_worker_res': {}, '_machine_res': {},
             'after_index': SimpleNamespace(by_key={'1_2': {'start_minutes': 0}})}
    config = ResourceConfiguration('1_2', 1, 1, 1, (1,), ('1',), 0, 600, (), 10)
    cache = c.CalendarRegimeCache()
    a = c._breakpoints(state, '1_2', config, 0, cache=cache)
    shorter = replace(config, productive_seconds=300, productive_minutes=5)
    b = c._breakpoints(state, '1_2', shorter, 0, cache=cache)
    assert max(a) == 3000 and max(b) == 3300
    assert b == c._breakpoints(state, '1_2', shorter, 0)
    assert cache.statistics['breakpoint_misses'] == 2


def test_crew_cache_invalidates_on_in_place_calendar_and_reservation_change():
    p, _, index = fixture()
    state = {'problem': p, 'operator_index': index, '_worker_res': {}}
    config = ResourceConfiguration('1_2', 1, 1, 1, (1,), ('1',), 0, 600, (), 10)
    cache = c.CalendarRegimeCache()
    assert c._execution(state, config, 0, cache=cache)[0] == 600
    index['shift_of'][1] = (14, 22)
    changed = c._execution(state, config, 0, cache=cache)
    assert changed == c._execution(state, config, 0)
    assert changed[0] > 600
    index['shift_of'][1] = (6, 14)
    state['_worker_res']['1'] = [(p.origin, p.origin+timedelta(seconds=300))]
    reserved = c._execution(state, config, 0, cache=cache)
    assert reserved == c._execution(state, config, 0)
    assert reserved[0] == 900


def test_indexed_reservation_transitions_match_uncached_walk_across_shift():
    problem, _, operator_index = fixture(cross_shift=True)
    origin = problem.origin
    reservations = {'1': [(origin + timedelta(minutes=2), origin + timedelta(minutes=4)),
                          (origin + timedelta(minutes=7), origin + timedelta(minutes=9))]}
    cache = {}
    for offset in (0, 60, 120, 240, 420, 540, 600):
        ready = origin + timedelta(seconds=offset)
        expected = _fixed_crew_execution(problem, operator_index, ('1',), ready, 20,
                                         reservations)
        actual = _fixed_crew_execution(problem, operator_index, ('1',), ready, 20,
                                       reservations, availability_cache=cache)
        assert actual == expected


def test_affine_regime_cannot_drop_an_extra_worker_segment():
    assert c._affine(((100, (200, 100, 200)),
                      (101, (300, 101, 102, 250, 300)))) is None


def test_calendar_shape_transition_keeps_exact_single_second_starts(monkeypatch):
    problem, _, index = fixture(cross_shift=True)
    state = {'problem': problem, 'operator_index': index, 'keys': ('1_2',),
             '_worker_res': {}, '_machine_res': {},
             'after_index': SimpleNamespace(by_key={'1_2': {'start_minutes': 0}},
                                             route_predecessor={})}
    config = ResourceConfiguration('1_2', 1, 1, 1, (1,), ('1',), 0, 2, (), 2 / 60)
    monkeypatch.setattr(c, '_breakpoints', lambda *args, **kwargs: (0, 1))
    monkeypatch.setattr(c, '_machine_accepts_cached', lambda *args, **kwargs: True)
    monkeypatch.setattr(c, '_execution', lambda _state, _config, start, *_args:
                        (2, ((0, 2),)) if start == 0 else
                        (3, ((1, 2), (2, 3))))
    regimes, _, _, evidence = c.generate_calendar_regimes(state, {'1_2': (config,)})
    assert evidence['mismatches'] == 0
    assert {row['lo'] for row in regimes['1_2']} == {0, 1}
    assert all(row['lo'] == row['hi'] for row in regimes['1_2'])


def test_signature_eviction_bounds_cache_and_never_reuses_a_token():
    p, _, index = fixture()
    state = {'problem': p, 'operator_index': index, '_worker_res': {}}
    config = ResourceConfiguration('1_2', 1, 1, 1, (1,), ('1',), 0, 600, (), 10)
    cache = c.CalendarRegimeCache(max_signatures=2)
    seen = []
    for minutes in range(1, 6):  # five commitment states, each a new signature
        state['_worker_res']['1'] = [(p.origin, p.origin + timedelta(seconds=60 * minutes))]
        token = cache.token('worker', c._worker_calendar_signature(state))
        assert token not in seen
        seen.append(token)
        assert c._execution(state, config, 0, cache=cache, worker_signature=token) == c._execution(state, config, 0)
    assert len(cache.signature_tokens) == 2 and len(cache.execution) == 2
    assert cache.statistics['evictions'] == 3
    # A signature seen again after eviction gets a fresh token, never an old one.
    state['_worker_res']['1'] = [(p.origin, p.origin + timedelta(seconds=60))]
    again = cache.token('worker', c._worker_calendar_signature(state))
    assert again not in seen
    assert c._execution(state, config, 0, cache=cache, worker_signature=again) == c._execution(state, config, 0)
