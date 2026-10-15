from datetime import datetime

from crafter.authoritative_semantics import walk_fixed_crew


def test_authoritative_fixed_crew_calendar_walk_returns_start_end_segments():
    ready = datetime(2026, 1, 1, 6, 0, 0)
    start, end, segments = walk_fixed_crew(ready, 10.0, lambda _when: True)
    assert start == ready
    assert end > start
    assert isinstance(segments, list)
    assert segments and segments[0][0] == start and segments[-1][1] == end


def test_e17_adapter_contract_is_three_value_unpacking():
    # This is intentionally a source-level contract check: the E1.7 adapter
    # must consume all three authoritative values, never classify tuple-shape
    # errors as calendar infeasibility.
    source = open("crafter/lns_probe/joint_modes.py", encoding="utf-8").read()
    assert "_start, end, segments = walk_fixed_crew" in source
    assert "except NoAvailableCalendar" in source
    assert "except Exception as exc" not in source


def test_e17_replay_contract_uses_authoritative_productive_and_reservations():
    source = open("crafter/lns_probe/run_e1_7.py", encoding="utf-8").read()
    assert "mode.productive_minutes" in source
    assert "outside_worker_reservations" in source
    assert "post_cp_envelope_changes.json" in source
    assert "replay_end-mode.end_seconds" in source
    assert "except NoAvailableCalendar" in source


def test_e17_objective_matches_e14_delivery_surrogate():
    model = open("crafter/lns_probe/joint_mode_model.py", encoding="utf-8").read()
    assert "(problem.horizon_seconds + 1) * sum(tardiness) + max_end" in model
    assert "sum(ends.values())" not in model
