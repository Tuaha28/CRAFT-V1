"""Packed windows, halving recovery and cycle rebasing for captured TEST scheduling."""
import pytest

from web_v1.audits.factory46_operation_schedule_v1.run_factory_test import (
    _pack_route_windows, _split_failed_window)
from web_v1.backend.integrations.craft.run_capture_governed import _remaining_routes


def route(order, n):
    return [{"operation_key": f"{order}_{i}"} for i in range(1, n + 1)]


def test_packing_fills_windows_across_orders_without_breaking_route_order():
    windows = _pack_route_windows([route("a", 3), route("b", 4), route("c", 2)], 5)
    # 3 + 4 and 4 + 2 both exceed 5, so each route starts a new window.
    assert windows == [("a_1", "a_2", "a_3"), ("b_1", "b_2", "b_3", "b_4"), ("c_1", "c_2")]
    flat = [k for w in windows for k in w]
    assert sorted(flat) == sorted(f"{o}_{i}" for o, n in (("a", 3), ("b", 4), ("c", 2)) for i in range(1, n + 1))
    for order in "abc":
        positions = [flat.index(k) for k in flat if k.startswith(order)]
        assert positions == sorted(positions)


def test_packing_combines_short_routes_up_to_the_window_size():
    windows = _pack_route_windows([route(o, 2) for o in "abcdefg"], 6)
    assert [len(w) for w in windows] == [6, 6, 2]


def test_long_route_is_chunked_into_successive_windows():
    windows = _pack_route_windows([route("a", 12)], 5)
    assert windows == [tuple(f"a_{i}" for i in range(1, 6)), tuple(f"a_{i}" for i in range(6, 11)),
                       ("a_11", "a_12")]


def test_halving_recovery_shrinks_large_windows_gradually():
    keys = tuple(f"k{i}" for i in range(15))
    first = _split_failed_window(keys, halving=True)
    assert [len(w) for w in first] == [8, 7] and first[0] + first[1] == keys
    assert _split_failed_window(("x",), halving=True) == []
    # Legacy policy is unchanged.
    assert [len(w) for w in _split_failed_window(keys[:5])] == [2, 2, 1]


def eligible(*routes):
    return [("2026-01-01", order, rows, {"production_order_id": order}) for order, rows in routes]


def test_rebase_returns_route_suffixes_after_an_approved_prefix():
    remaining, prefixes = _remaining_routes(eligible(("a", route("a", 4)), ("b", route("b", 2))),
                                            {"a_1", "a_2", "b_1", "b_2"})
    assert [(order, [r["operation_key"] for r in rows]) for _, order, rows, _ in remaining] == [("a", ["a_3", "a_4"])]
    assert [r["operation_key"] for r in prefixes["a"]] == ["a_1", "a_2"]


def test_rebase_rejects_an_approval_that_is_not_a_route_prefix():
    with pytest.raises(ValueError, match="CAPTURE_APPROVED_NOT_ROUTE_PREFIX"):
        _remaining_routes(eligible(("a", route("a", 3))), {"a_2"})


def test_excluded_operation_removes_itself_and_its_route_successors():
    remaining, _ = _remaining_routes(eligible(("a", route("a", 5)), ("b", route("b", 2))),
                                     {"a_1"}, excluded={"a_3"})
    assert {order: [r["operation_key"] for r in rows] for _, order, rows, _ in remaining} == \
        {"a": ["a_2"], "b": ["b_1", "b_2"]}


def test_rebased_base_carries_eligibility_for_approved_operations():
    # Regression: cycle 2 failed GLOBAL_STATE_APPROVED_INELIGIBLE because the
    # rebased packet described only the newly selected operations.
    from web_v1.audits.factory46_operation_schedule_v1 import run_factory_test as runner
    from web_v1.backend.integrations.craft.run_capture_governed import prepare
    source, _readiness, normalized, _hash = runner._source(4)
    _release, _order, rows, _row = runner._eligible_order_routes(source, normalized)[0]
    approved = [rows[0]["operation_key"]]
    base = prepare(4, 200, approved_keys=approved)
    assert approved[0] not in base["selected_keys"]
    for key in approved + base["selected_keys"]:
        assert base["eligibility"][key]["machines"] and base["eligibility"][key]["workers"]
        assert base["calendar_windows"][key] and key in base["route_releases"]
