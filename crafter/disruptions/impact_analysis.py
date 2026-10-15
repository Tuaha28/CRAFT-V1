"""Deterministic graph-based disruption impact propagation."""
from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Iterable

from crafter.lns_probe.resource_graph import build_resource_state_graph


def direct_impact(index, rows, event) -> set[str]:
    by_key = index.by_key
    affected = set(str(x) for x in event.operation_keys)
    machine_ids, worker_ids = set(event.machine_ids), set(event.worker_ids)
    for key, row in by_key.items():
        start = int(round(float(row["start_minutes"]) * 60.0))
        end = int(round(float(row["end_minutes"]) * 60.0))
        future_overlap = end > event.event_time_seconds
        if machine_ids and future_overlap and machine_ids.intersection(int(x) for x in row.get("machine_ids", ())):
            affected.add(key)
        if worker_ids and future_overlap and worker_ids.intersection(str(x) for x in row.get("employee_ids", ())):
            affected.add(key)
        if event.order_ids and str(row.get("production_order_id")) in set(event.order_ids):
            affected.add(key)
    delayed = event.metadata.get("predecessor_operation_key")
    if delayed:
        affected.add(str(delayed))
        successor = index.route_successor.get(str(delayed))
        while successor:
            affected.add(successor)
            successor = index.route_successor.get(successor)
    return affected


def propagate_impact(index, direct: Iterable[str], *, max_depth: int = 8) -> dict:
    direct = set(direct)
    distance = {key: 0 for key in direct}
    queue = deque(sorted(direct))
    adjacency = index.adjacency
    while queue:
        key = queue.popleft()
        depth = distance[key]
        if depth >= max_depth:
            continue
        for nxt in sorted(adjacency.get(key, ())):
            if nxt not in distance:
                distance[nxt] = depth + 1
                queue.append(nxt)
    return {
        "direct_operations": sorted(direct),
        "indirect_operations": sorted(k for k, d in distance.items() if d > 0),
        "impacted_operations": sorted(distance),
        "impact_depth_by_operation": {k: int(v) for k, v in sorted(distance.items())},
        "max_depth": max(distance.values(), default=0),
        "impacted_orders": sorted({str(index.by_key[k]["production_order_id"]) for k in distance}),
        "impacted_machines": sorted({int(mid) for k in distance for mid in index.by_key[k].get("machine_ids", ())}),
        "impacted_workers": sorted({str(w) for k in distance for w in index.by_key[k].get("employee_ids", ())}),
    }


def graph_delta(graph_before: dict, graph_after: dict, event) -> dict:
    before_ops = {x["operation_key"]: x for x in graph_before["nodes"]["operations"]}
    after_ops = {x["operation_key"]: x for x in graph_after["nodes"]["operations"]}
    changed = []
    for key in sorted(set(before_ops) | set(after_ops)):
        if before_ops.get(key) != after_ops.get(key):
            changed.append(key)
    return {
        "event_id": event.event_id,
        "changed_operation_nodes": changed,
        "changed_node_count": len(changed),
        "changed_edge_count": 0,
        "update_mode": "rebuild_shadow_graph_for_correctness",
    }


def build_after_graph(problem, index, op_details, operator_index, event):
    return build_resource_state_graph(problem, index, op_details, operator_index)
