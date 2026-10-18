"""LANE_SPLIT_V1 input transformation."""
import pytest

from crafter.production.lanes import base_order_id, lane_policy, split_lanes


def op(order, op_id, seq, qty):
    return {"operation_key": f"{order}_{op_id}", "order_id": order, "operation_id": op_id,
            "routing_sequence": seq, "execution_state": "UNSTARTED", "lots": [{"lot_id": 1}],
            "quantity": {"target": qty, "total": qty, "completed": 0, "in_flight": 0,
                         "not_started": qty, "remaining": qty}}


def fixture():
    snapshot = {"operations": [op("7", "1", 1, 90), op("7", "2", 2, 85), op("8", "3", 1, 30)]}
    source = {"orders": [{"production_order_id": 7, "target_quantity": 90},
                         {"production_order_id": 8, "target_quantity": 30}],
              "operations": [{"production_order_id": 7, "operation_id": 1, "standard_allowed_minutes": 1},
                             {"production_order_id": 7, "operation_id": 2, "standard_allowed_minutes": 2},
                             {"production_order_id": 8, "operation_id": 3, "standard_allowed_minutes": 3}]}
    return source, snapshot


def test_lanes_split_quantity_exactly_and_keep_route_per_lane():
    source, snapshot = fixture()
    new_source, new_snapshot, lane_map = split_lanes(source, snapshot, ["7", "8"], lane_policy(20, 4))
    lanes = [o for o in new_snapshot["operations"] if o["order_id"].startswith("7L")]
    # min quantity 85 // 20 = 4 lanes; every lane keeps both operations.
    assert sorted({o["order_id"] for o in lanes}) == ["7L1", "7L2", "7L3", "7L4"]
    for op_id, qty in (("1", 90), ("2", 85)):
        shares = [o["quantity"]["not_started"] for o in lanes if o["operation_id"] == op_id]
        assert sum(shares) == qty and max(shares) - min(shares) <= 1
    assert all(o["lots"] == [] and o["lane_of"].startswith("7_") for o in lanes)
    assert sum(o["target_quantity"] for o in new_source["orders"]
               if str(o["production_order_id"]).startswith("7L")) == 90
    # Order 8 (30 // 20 = 1 lane) is unchanged; inputs are not mutated.
    assert [o["operation_key"] for o in new_snapshot["operations"] if o["order_id"] == "8"] == ["8_3"]
    assert len(snapshot["operations"]) == 3 and snapshot["operations"][0]["lots"] == [{"lot_id": 1}]
    assert lane_map["7L2"] == {"order_id": "7", "lanes": 4} and "8" not in lane_map
    assert {(str(r["production_order_id"]), str(r["operation_id"])) for r in new_source["operations"]} >= {
        ("7L1", "1"), ("7L4", "2"), ("8", "3")}


def test_ineligible_orders_are_untouched_and_base_ids_resolve():
    source, snapshot = fixture()
    _s, new_snapshot, _m = split_lanes(source, snapshot, ["8"], lane_policy(20, 4))
    assert [o["operation_key"] for o in new_snapshot["operations"]] == ["7_1", "7_2", "8_3"]
    assert base_order_id("7L3") == "7" and base_order_id("7") == "7"


def test_policy_bounds():
    with pytest.raises(ValueError):
        lane_policy(0, 4)
    with pytest.raises(ValueError):
        lane_policy(20, 0)
