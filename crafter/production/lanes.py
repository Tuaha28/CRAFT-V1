"""LANE_SPLIT_V1: parallel bundle lanes for captured TEST scheduling.

The captured lot history shows how the factories actually execute an
operation: bundles of about 20 pieces, each sewn by exactly one worker on one
machine, with several bundles of the same operation in progress at once and
the next operation starting before the previous one finishes.

This module reproduces that as an input transformation only. An admissible
order is split into ``L`` lane orders (``<order>L<i>``); every lane carries an
even share of each operation's quantity and is an ordinary one-worker,
one-machine route, so the canonical CP model, calendars, replay and validators
are unchanged. Lane ``i`` of operation ``n + 1`` follows lane ``i`` of
operation ``n``, which lets consecutive operations overlap across lanes. Crew
size stays 1 per lane: parallelism comes from more lanes, never from an
inferred crew.

``L = clamp(min_operation_quantity // bundle_pieces, 1, max_lanes)`` keeps every
lane at least one bundle. Orders not admitted to scheduling are left unchanged.
"""
from __future__ import annotations

import copy
import re

LANE_POLICY_VERSION = "LANE_SPLIT_V1"
_LANE_ID = re.compile(r"^(?P<order>[^_L]+)L(?P<lane>\d+)$")


def lane_policy(bundle_pieces=20, max_lanes=4):
    if type(bundle_pieces) is not int or bundle_pieces < 1:
        raise ValueError("LANE_BUNDLE_PIECES_INVALID")
    if type(max_lanes) is not int or not 1 <= max_lanes <= 16:
        raise ValueError("LANE_MAX_LANES_INVALID")
    return {"version": LANE_POLICY_VERSION, "bundle_pieces": bundle_pieces, "max_lanes": max_lanes}


def base_order_id(order_id):
    """Original order of a lane order id (identity for unsplit orders)."""
    match = _LANE_ID.match(str(order_id))
    return match.group("order") if match else str(order_id)


def _shares(quantity, lanes):
    return [quantity // lanes + (index < quantity % lanes) for index in range(lanes)]


def split_lanes(source, snapshot, eligible_order_ids, policy):
    """Return (source, snapshot, lane_map) with eligible orders split into lanes.

    Inputs are not mutated. ``lane_map`` maps each lane order id to its
    original order id and lane count, for reporting order-level KPIs.
    """
    if policy.get("version") != LANE_POLICY_VERSION:
        raise ValueError("LANE_POLICY_UNSUPPORTED")
    bundle, max_lanes = policy["bundle_pieces"], policy["max_lanes"]
    eligible = {str(order_id) for order_id in eligible_order_ids}
    for order_id in eligible:
        if "L" in order_id or "_" in order_id:
            raise ValueError(f"LANE_ORDER_ID_NOT_SPLITTABLE:{order_id}")
    by_order = {}
    for operation in snapshot["operations"]:
        if str(operation["order_id"]) in eligible:
            by_order.setdefault(str(operation["order_id"]), []).append(operation)
    lanes = {order_id: max(1, min(max_lanes, min(int(op["quantity"]["not_started"]) for op in ops) // bundle))
             for order_id, ops in by_order.items()}
    split = {order_id for order_id, count in lanes.items() if count > 1}

    source_ops = {(str(row["production_order_id"]), str(row["operation_id"])): row
                  for row in source["operations"]}
    new_snapshot = {k: v for k, v in snapshot.items() if k != "operations"}
    new_snapshot["operations"] = []
    new_source = {k: v for k, v in source.items() if k not in ("operations", "orders")}
    new_source["operations"] = [row for row in source["operations"]
                                if str(row["production_order_id"]) not in split]
    new_source["orders"] = [row for row in source["orders"]
                            if str(row["production_order_id"]) not in split]
    lane_map = {}
    for operation in snapshot["operations"]:
        order_id = str(operation["order_id"])
        if order_id not in split:
            new_snapshot["operations"].append(operation)
            continue
        quantity = int(operation["quantity"]["not_started"])
        for index, share in enumerate(_shares(quantity, lanes[order_id]), 1):
            lane_order = f"{order_id}L{index}"
            lane = copy.deepcopy(operation)
            lane.update(operation_key=f"{lane_order}_{operation['operation_id']}", order_id=lane_order,
                        lots=[], lane_of=operation["operation_key"])
            lane["quantity"].update(target=share, total=share, not_started=share, remaining=share)
            new_snapshot["operations"].append(lane)
            raw = dict(source_ops[(order_id, str(operation["operation_id"]))])
            raw["production_order_id"] = lane_order
            new_source["operations"].append(raw)
            lane_map[lane_order] = {"order_id": order_id, "lanes": lanes[order_id]}
    for order in source["orders"]:
        order_id = str(order["production_order_id"])
        if order_id in split:
            for index, share in enumerate(_shares(int(order["target_quantity"]), lanes[order_id]), 1):
                new_source["orders"].append({**order, "production_order_id": f"{order_id}L{index}",
                                             "target_quantity": share})
    return new_source, new_snapshot, lane_map
