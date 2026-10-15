"""Independent assertions for the TEST-only two-order priority competition."""
import pytest

from web_v1.backend.integrations.craft.priority_fixture import KEYS, solve


def _validated(result):
    acceptance = result["acceptance"]
    scoped = acceptance["validator"]["scope_acceptance"]
    assert acceptance["status"] == "ACCEPTED"
    assert acceptance["objective"]["status"] == "OBJECTIVE_OPTIMIZATION_COMPLETE"
    assert acceptance["replay"]["status"] == "PASS"
    assert scoped["physical_status"] == "VALID"
    assert scoped["scope_status"] == "SCOPE_VALID"
    assert acceptance["materialization"]["mismatch_count"] == 0
    assert result["quantity"]["status"] == "PASS"
    rows = acceptance["selected_schedule"]
    assert {row["operation_key"] for row in rows} == set(KEYS)
    assert all(row["start_seconds"] >= 300 for row in rows)
    ordered = sorted(rows, key=lambda item: item["start_seconds"])
    assert all(left["end_seconds"] <= right["start_seconds"] for left, right in zip(ordered, ordered[1:]))
    by_key = {row["operation_key"]: row for row in rows}
    assert by_key["A_residual"]["end_seconds"] <= by_key["A_finish"]["start_seconds"]


def test_urgent_override_reaches_objective_and_changes_competing_order():
    baseline = solve()
    urgent = solve("URGENT", entered_by="test-manager", override_id="test-priority-1")
    _validated(baseline)
    _validated(urgent)
    before = baseline["fixture"]["order_completion_seconds"]
    after = urgent["fixture"]["order_completion_seconds"]
    assert before == {"A": 1020, "B": 3120}
    assert after == {"A": 3120, "B": 900}
    context = urgent["acceptance"]["objective"]["objective"]["context"]
    assert context["active_manager_overrides"]["B"]["entered_by"] == "test-manager"
    assert context["orders"][1]["tier"] == "URGENT"
    assert baseline["fixture"]["regime_count"] == urgent["fixture"]["regime_count"]
    assert baseline["acceptance"]["objective"]["after_metrics"]["regional_makespan_seconds"] == 3120
    assert urgent["acceptance"]["objective"]["after_metrics"]["regional_makespan_seconds"] == 3120


def test_priority_fixture_rejects_unknown_tier():
    with pytest.raises(ValueError, match="UNSUPPORTED_PRIORITY_TIER"):
        solve("EMERGENCY")
