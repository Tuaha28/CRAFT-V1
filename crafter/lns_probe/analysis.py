"""Read-only incumbent indexes and deterministic delay attribution."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
import math

from crafter.authoritative_semantics import (
    authoritative_changeover_seconds,
    authoritative_productive_seconds,
    project_instant_seconds,
)


def _dt(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def seconds(value_minutes):
    return project_instant_seconds(float(value_minutes)*60.0).projected_seconds


@dataclass(frozen=True)
class IncumbentIndex:
    rows: tuple
    by_key: dict
    route_predecessor: dict
    route_successor: dict
    machine_predecessor: dict
    machine_successor: dict
    machine_sequences: dict
    order_operations: dict
    order_completion: dict
    order_tardiness: dict
    operation_scores: dict
    operation_features: dict
    adjacency: dict


def build_incumbent_index(problem, rows, op_details):
    by_key = {str(row["operation_key"]): row for row in rows}
    order_operations = defaultdict(list)
    for operation in problem.operations:
        order_operations[operation.order_id].append(operation.operation_key)
    route_predecessor, route_successor = {}, {}
    for order_id, keys in order_operations.items():
        keys.sort(key=lambda key: (
            problem.operation_by_key[key].sequence, key))
        for left, right in zip(keys, keys[1:]):
            route_successor[left] = right
            route_predecessor[right] = left

    machine_sequences = defaultdict(list)
    for row in rows:
        start, end = seconds(row["start_minutes"]), seconds(row["end_minutes"])
        for machine_id in row["machine_ids"]:
            machine_sequences[int(machine_id)].append((
                start, end, str(row["operation_key"]),
                str(row["production_order_id"])))
    machine_predecessor, machine_successor = defaultdict(list), defaultdict(list)
    for machine_id, sequence in machine_sequences.items():
        sequence.sort(key=lambda item: (item[0], item[1], item[2]))
        for left, right in zip(sequence, sequence[1:]):
            machine_successor[left[2]].append((machine_id, right[2]))
            machine_predecessor[right[2]].append((machine_id, left[2]))

    order_completion, order_tardiness = {}, {}
    for order_id, keys in order_operations.items():
        final = max(keys, key=lambda key: problem.operation_by_key[key].sequence)
        completion = seconds(by_key[final]["end_minutes"])
        order_completion[order_id] = completion
        due = problem.order_by_id[order_id].deadline_seconds
        order_tardiness[order_id] = max(0, completion-max(0, due or completion))

    usable_by_line_capability = Counter()
    for machine in problem.machines:
        if not machine.is_usable:
            continue
        for capability in set(item.strip().lower()
                              for item in machine.capabilities):
            usable_by_line_capability[(machine.line_id, capability)] += 1
    operation_scores, operation_features = {}, {}
    for operation in problem.operations:
        key = operation.operation_key
        row = by_key[key]
        order = problem.order_by_id[operation.order_id]
        start, productive_start, end = (
            seconds(row["start_minutes"]),
            seconds(row["productive_start_minutes"]),
            seconds(row["end_minutes"]),
        )
        k = len(set(int(value) for value in row["machine_ids"]))
        required = authoritative_productive_seconds(op_details[key], k)
        calendar_delay = max(0, end-productive_start-required)
        predecessor = route_predecessor.get(key)
        routing_wait = max(
            0, start-seconds(by_key[predecessor]["end_minutes"])) if predecessor else max(
                0, start-max(0, order.release_seconds))
        machine_block = 0
        blockers = []
        for machine_id, pred_key in machine_predecessor.get(key, ()):
            pred = by_key[pred_key]
            pred_end = seconds(pred["end_minutes"])
            gap = authoritative_changeover_seconds(
                pred["production_order_id"], row["production_order_id"])
            blocking = max(0, pred_end+gap-start)
            machine_block = max(machine_block, blocking)
            blockers.append({"machine_id": machine_id,
                             "predecessor": pred_key, "blocking_seconds": blocking})
        capability = operation.required_capability.strip().lower()
        eligible_counts = [
            usable_by_line_capability[(line_id, capability)]
            for line_id in operation.eligible_line_ids
            if usable_by_line_capability[(line_id, capability)] > 0
        ]
        scarcity = 1.0/max(1, sum(eligible_counts))
        downstream = sum(
            op_details[candidate]["serial_processing_mins"]
            for candidate in order_operations[operation.order_id]
            if problem.operation_by_key[candidate].sequence >= operation.sequence)
        tardiness = order_tardiness[operation.order_id]
        due_pressure = tardiness + max(0, end-(order.deadline_seconds or end))
        # Heuristic causal ranking only; it is not a critical-path proof.
        score = (
            float(tardiness)
            + 2.0*calendar_delay
            + routing_wait
            + machine_block
            + 100_000.0*scarcity
            + 10.0*downstream
        )
        operation_scores[key] = score
        operation_features[key] = {
            "factory_id": order.factory_id,
            "order_id": operation.order_id,
            "line_id": int(row["line_id"]),
            "capability": capability,
            "start_seconds": start,
            "end_seconds": end,
            "machine_count": k,
            "calendar_delay_seconds": calendar_delay,
            "routing_or_release_wait_seconds": routing_wait,
            "machine_blocking_seconds": machine_block,
            "machine_blockers": blockers,
            "eligible_machine_count": sum(eligible_counts),
            "eligible_line_count": len(operation.eligible_line_ids),
            "scarcity": scarcity,
            "downstream_work_minutes": downstream,
            "order_tardiness_seconds": tardiness,
            "deadline_pressure_seconds": due_pressure,
            "heuristic_causal_score": score,
        }

    adjacency = defaultdict(set)
    for left, right in route_successor.items():
        adjacency[left].add(right)
        adjacency[right].add(left)
    for key, neighbors in machine_predecessor.items():
        for _machine, neighbor in neighbors:
            adjacency[key].add(neighbor)
            adjacency[neighbor].add(key)
    for key, neighbors in machine_successor.items():
        for _machine, neighbor in neighbors:
            adjacency[key].add(neighbor)
            adjacency[neighbor].add(key)
    # Add only immediate temporal neighbors within an incumbent line/capability
    # sequence. This is linear and never creates potential-contention cliques.
    by_resource = defaultdict(list)
    for key, feature in operation_features.items():
        by_resource[(feature["factory_id"], feature["line_id"],
                     feature["capability"])].append(key)
    for keys in by_resource.values():
        keys.sort(key=lambda key: (
            operation_features[key]["start_seconds"], key))
        for left, right in zip(keys, keys[1:]):
            adjacency[left].add(right)
            adjacency[right].add(left)

    return IncumbentIndex(
        rows=tuple(rows), by_key=by_key,
        route_predecessor=route_predecessor,
        route_successor=route_successor,
        machine_predecessor=dict(machine_predecessor),
        machine_successor=dict(machine_successor),
        machine_sequences=dict(machine_sequences),
        order_operations={key: tuple(value)
                          for key, value in order_operations.items()},
        order_completion=order_completion,
        order_tardiness=order_tardiness,
        operation_scores=operation_scores,
        operation_features=operation_features,
        adjacency={key: tuple(sorted(value)) for key, value in adjacency.items()},
    )


def causal_attribution(problem, index):
    report = []
    for order_id, tardiness in sorted(
            index.order_tardiness.items(), key=lambda item: (-item[1], item[0])):
        if tardiness <= 0:
            continue
        keys = index.order_operations[order_id]
        final = max(keys, key=lambda key: problem.operation_by_key[key].sequence)
        ranked = sorted(keys, key=lambda key: (
            -index.operation_scores[key], key))
        causes = Counter()
        for key in keys:
            feature = index.operation_features[key]
            causes["worker/calendar pause"] += feature["calendar_delay_seconds"]
            causes["routing/release waiting"] += feature[
                "routing_or_release_wait_seconds"]
            causes["physical-machine blocking"] += feature[
                "machine_blocking_seconds"]
            causes["resource scarcity proxy"] += int(
                100_000*feature["scarcity"])
        report.append({
            "order_id": order_id,
            "factory_id": problem.order_by_id[order_id].factory_id,
            "tardiness_seconds": tardiness,
            "final_operation": final,
            "top_attributed_delay_causes": [
                {"cause": cause, "attributed_score": value}
                for cause, value in causes.most_common()
            ],
            "top_candidate_operations": [
                {"operation_key": key,
                 "score": index.operation_scores[key],
                 "features": index.operation_features[key]}
                for key in ranked[:min(10, len(ranked))]
            ],
            "disclosure": (
                "Deterministic repair signal only; not an exact critical-path proof."),
        })
    return {
        "late_orders": len(report),
        "method": (
            "Routing waits, realized machine predecessors, authoritative calendar "
            "delay, resource scarcity, downstream work and due pressure. Only "
            "realized immediate relationships are indexed; no contention cliques."),
        "orders": report,
    }
