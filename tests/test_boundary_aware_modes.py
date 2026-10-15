from crafter.repair.boundary_aware_modes import (
    BoundaryAvailabilityIndex,
    _crew_candidates,
    _covered,
    _matching,
    _merge,
    _subtract,
)
from crafter.repair.resource_configs import generate_matching_worker_crews
from datetime import datetime, timedelta
from types import SimpleNamespace


def test_half_open_subtraction_keeps_left_boundary_free():
    assert _subtract(((0, 10),), ((10, 20),)) == ((0, 10),)
    assert _subtract(((0, 20),), ((5, 10),)) == ((0, 5), (10, 20))


def test_interval_merging_is_deterministic_and_half_open_safe():
    assert _merge(((10, 20), (0, 10), (20, 30))) == ((0, 30),)
    assert _merge(((0, 5), (7, 10))) == ((0, 5), (7, 10))


def test_covered_requires_every_required_pause_resume_segment():
    # `_covered` is intentionally a contiguous-interval predicate; callers
    # apply it to each labor-active segment when pause/resume creates gaps.
    assert _covered(((0, 5),), ((0, 5),))
    assert _covered(((7, 10),), ((7, 10),))
    assert not _covered(((0, 5), (8, 10)), ((0, 10),))


def test_matching_is_unique_deterministic_and_prevents_duplicate_workers():
    slots = ("skill-a", "skill-b")
    candidates = (("w2", "w1"), ("w2", "w3"))
    first = _matching(slots, candidates, max_solutions=8)
    second = _matching(slots, candidates, max_solutions=8)
    assert first == second
    assert first
    assert all(len(crew) == len(set(crew)) == 2 for crew in first)
    assert all(tuple(sorted(crew)) == crew for crew in first)


def test_matching_reports_insufficient_workers_without_fabrication():
    assert _matching(("skill", "skill"), (("w1",), ("w1",)), max_solutions=4) == ()


def test_matching_forbidden_crew_is_not_repeated():
    forbidden = (("w1", "w2"),)
    result = _matching(("skill", "skill"), (("w1", "w2", "w3"),) * 2,
                       forbidden=forbidden, max_solutions=8)
    assert result
    assert forbidden[0] not in result
    assert len(result) == len(set(result))


def test_large_worker_pool_matching_stays_compact_without_combination_materialization():
    pool = tuple(f"w{i:04d}" for i in range(1000))
    crews = generate_matching_worker_crews(
        ("skill",) * 5, (pool,) * 5, max_solutions=12)
    assert len(crews) == 12
    assert all(len(crew) == 5 and len(set(crew)) == 5 for crew in crews)
    assert len({tuple(sorted(crew)) for crew in crews}) == 12


def test_fixed_crew_may_pause_but_must_cover_every_labor_segment(monkeypatch):
    import crafter.repair.boundary_aware_modes as modes

    origin = datetime(2026, 1, 1)
    segments = ((10, 20), (30, 40))
    monkeypatch.setattr(modes, "authoritative_setup_seconds", lambda detail: 0)
    monkeypatch.setattr(
        modes, "authoritative_duration_contract",
        lambda detail, k: SimpleNamespace(productive_minutes=20.0))
    monkeypatch.setattr(modes, "_eligible_workers", lambda *args: ("w1", "w2"))
    monkeypatch.setattr(
        modes, "productive_segments",
        lambda ready, minutes, predicate: (
            ready,
            origin + timedelta(seconds=40),
            tuple((origin + timedelta(seconds=a), origin + timedelta(seconds=b))
                  for a, b in segments),
        ))
    monkeypatch.setattr(
        modes, "_fixed_crew_execution",
        lambda problem, index, crew, ready, minutes, reservations: (
            origin + timedelta(seconds=40),
            tuple((origin + timedelta(seconds=a), origin + timedelta(seconds=b))
                  for a, b in segments),
        ))
    state = {
        "problem": SimpleNamespace(
            origin=origin,
            operation_by_key={"op": SimpleNamespace(required_capability="skill")}),
        "op_details": {"op": {}},
        "operator_index": {},
        "planning_context": {"factory_id": 1},
        "_worker_res": {},
    }
    availability = BoundaryAvailabilityIndex(
        0, 100, {}, {}, {},
        {"w1": ((10, 20), (30, 40)), "w2": ((10, 20), (30, 40))})
    crews = _crew_candidates(
        state, availability, "op", 1, 2, (), 0, 1)
    assert crews == (("w1", "w2"),)


def test_fixed_crew_rejected_when_any_labor_segment_is_reserved(monkeypatch):
    import crafter.repair.boundary_aware_modes as modes

    origin = datetime(2026, 1, 1)
    segments = ((10, 20), (30, 40))
    monkeypatch.setattr(modes, "authoritative_setup_seconds", lambda detail: 0)
    monkeypatch.setattr(
        modes, "authoritative_duration_contract",
        lambda detail, k: SimpleNamespace(productive_minutes=20.0))
    monkeypatch.setattr(modes, "_eligible_workers", lambda *args: ("eligible", "reserved"))
    monkeypatch.setattr(
        modes, "productive_segments",
        lambda ready, minutes, predicate: (
            ready, origin + timedelta(seconds=40),
            tuple((origin + timedelta(seconds=a), origin + timedelta(seconds=b))
                  for a, b in segments),
        ))
    monkeypatch.setattr(
        modes, "_fixed_crew_execution",
        lambda problem, index, crew, ready, minutes, reservations: (
            origin + timedelta(seconds=40),
            tuple((origin + timedelta(seconds=a), origin + timedelta(seconds=b))
                  for a, b in segments),
        ))
    state = {
        "problem": SimpleNamespace(
            origin=origin,
            operation_by_key={"op": SimpleNamespace(required_capability="skill")}),
        "op_details": {"op": {}}, "operator_index": {},
        "planning_context": {"factory_id": 1}, "_worker_res": {},
    }
    availability = BoundaryAvailabilityIndex(
        0, 100, {}, {}, {},
        {"eligible": ((10, 20), (30, 40)), "reserved": ((10, 20),)})
    assert _crew_candidates(state, availability, "op", 1, 2, (), 0, 1) == ()
