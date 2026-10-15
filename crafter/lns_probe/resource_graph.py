"""Sparse realized Resource-State Graph and deterministic freedom scores."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import time
import os
import psutil

from crafter.authoritative_semantics import (
    authoritative_changeover_seconds, authoritative_productive_seconds,
)
from .analysis import seconds


def _rss():
    return psutil.Process(os.getpid()).memory_info().rss/1024**2


def build_resource_state_graph(problem, index, op_details, operator_index):
    started, before = time.perf_counter(), _rss()
    nodes = {"orders": [], "operations": [], "machines": [], "workers": []}
    edges = {key: [] for key in (
        "order_contains_operation", "routing_precedence",
        "realized_machine_sequence", "current_machine_assignment",
        "current_worker_assignment")}
    machine_work = Counter()
    worker_work = Counter()
    used_machines, used_workers = set(), set()
    for order in problem.orders:
        nodes["orders"].append({
            "order_id": order.order_id, "factory_id": order.factory_id,
            "release_seconds": order.release_seconds,
            "deadline_seconds": order.deadline_seconds,
            "completion_seconds": index.order_completion[order.order_id],
            "tardiness_seconds": index.order_tardiness[order.order_id],
            "late": index.order_tardiness[order.order_id] > 0,
            "priority_weight": order.priority_weight,
            "operation_count": len(index.order_operations[order.order_id]),
        })
    for operation in problem.operations:
        key, row = operation.operation_key, index.by_key[operation.operation_key]
        feature = index.operation_features[key]
        machines = tuple(int(value) for value in row["machine_ids"])
        workers = tuple(str(value) for value in row.get("employee_ids", ()))
        used_machines.update(machines); used_workers.update(workers)
        span = seconds(row["end_minutes"])-seconds(row["start_minutes"])
        for machine in machines: machine_work[machine] += span
        for worker in workers: worker_work[worker] += int(round(
            float(row["productive_minutes"])*60))
        nodes["operations"].append({
            "operation_key": key, "order_id": operation.order_id,
            "factory_id": problem.order_by_id[operation.order_id].factory_id,
            "sequence": operation.sequence, "line_id": int(row["line_id"]),
            "machine_count": len(machines),
            "start_seconds": feature["start_seconds"],
            "productive_start_seconds": seconds(row["productive_start_minutes"]),
            "end_seconds": feature["end_seconds"],
            "productive_seconds": authoritative_productive_seconds(
                op_details[key], len(machines)),
            "setup_seconds": int(round(float(row["setup_minutes"])*60)),
            "elapsed_seconds": span, "machine_ids": list(machines),
            "worker_ids": list(workers),
            "capability": operation.required_capability,
            "eligible_line_count": feature["eligible_line_count"],
            "eligible_machine_count": feature["eligible_machine_count"],
            "order_tardiness_seconds": feature["order_tardiness_seconds"],
            "mutable": True, "fixed_or_inflight": False,
        })
        edges["order_contains_operation"].append(
            {"order_id": operation.order_id, "operation_key": key})
        for machine in machines:
            edges["current_machine_assignment"].append(
                {"operation_key": key, "machine_id": machine})
        for worker in workers:
            edges["current_worker_assignment"].append(
                {"operation_key": key, "worker_id": worker})
    for left, right in sorted(index.route_successor.items()):
        edges["routing_precedence"].append({"from": left, "to": right})
    for machine_id, sequence in sorted(index.machine_sequences.items()):
        for left, right in zip(sequence, sequence[1:]):
            gap = authoritative_changeover_seconds(left[3], right[3])
            edges["realized_machine_sequence"].append({
                "from": left[2], "to": right[2], "machine_id": machine_id,
                "previous_order": left[3], "next_order": right[3],
                "required_changeover_seconds": gap,
                "actual_gap_seconds": right[0]-left[1],
            })
    for machine_id in sorted(used_machines):
        machine = problem.machine_by_id[machine_id]
        nodes["machines"].append({
            "machine_id": machine_id, "factory_id": machine.factory_id,
            "line_id": machine.line_id,
            "capabilities": list(machine.capabilities),
            "usable": machine.is_usable,
            "incumbent_workload_seconds": machine_work[machine_id],
            "utilization": machine_work[machine_id]/problem.horizon_seconds,
            "commitment_count": sum(
                fixed.machine_id == machine_id
                for fixed in problem.resources.fixed_machine_commitments),
        })
    skills_by_worker = defaultdict(set)
    factory_by_worker = {}
    for skill, workers in operator_index.get("by_skill", {}).items():
        for worker in workers: skills_by_worker[str(worker)].add(str(skill))
    for (factory, _skill), workers in operator_index.get("by_factory", {}).items():
        for worker in workers: factory_by_worker[str(worker)] = int(factory)
    for worker in sorted(used_workers):
        nodes["workers"].append({
            "worker_id": worker, "factory_id": factory_by_worker.get(worker),
            "skills": sorted(skills_by_worker.get(worker, ())),
            "incumbent_productive_seconds": worker_work[worker],
        })
    return {
        "schema_version": "craft_resource_state_graph_v1",
        "nodes": nodes, "edges": edges,
        "statistics": {
            "nodes_by_type": {key: len(value) for key, value in nodes.items()},
            "edges_by_type": {key: len(value) for key, value in edges.items()},
            "potential_contention_edges": 0,
            "build_time_seconds": time.perf_counter()-started,
            "rss_increase_mb": max(0.0, _rss()-before),
        },
    }


def causal_waiting(problem, index, op_details):
    result = []
    for operation in problem.operations:
        key, row = operation.operation_key, index.by_key[operation.operation_key]
        start = seconds(row["start_minutes"])
        productive_start = seconds(row["productive_start_minutes"])
        end = seconds(row["end_minutes"])
        predecessor = index.route_predecessor.get(key)
        release = max(0, problem.order_by_id[operation.order_id].release_seconds)
        route_ready = (seconds(index.by_key[predecessor]["end_minutes"])
                       if predecessor else release)
        machine_ready, changeover, commitment = route_ready, 0, 0
        blockers = []
        for machine_id, pred_key in index.machine_predecessor.get(key, ()):
            pred = index.by_key[pred_key]
            pred_end = seconds(pred["end_minutes"])
            gap = authoritative_changeover_seconds(
                pred["production_order_id"], operation.order_id)
            machine_ready = max(machine_ready, pred_end+gap)
            changeover = max(changeover, gap)
            blockers.append({"machine_id": machine_id, "operation_key": pred_key,
                             "ready_seconds": pred_end+gap})
        first_segment = min((
            int(round((datetime.fromisoformat(str(pair[0]
                if not isinstance(pair, dict) else pair['start']))-
                problem.origin).total_seconds()))
            for segments in row.get("worker_segments_by_employee", {}).values()
            for pair in segments), default=productive_start)
        productive = authoritative_productive_seconds(
            op_details[key], len(row["machine_ids"]))
        worker_wait = max(0, first_segment-productive_start)
        calendar_pause = max(0, end-first_segment-productive)
        result.append({
            "operation_key": key, "order_id": operation.order_id,
            "factory_id": problem.order_by_id[operation.order_id].factory_id,
            "precedence_wait_seconds": max(0, start-route_ready),
            "machine_wait_seconds": max(0, machine_ready-route_ready),
            "worker_wait_seconds": worker_wait,
            "calendar_pause_seconds": calendar_pause,
            "changeover_delay_seconds": changeover,
            "commitment_delay_seconds": commitment,
            "release_delay_seconds": release if not predecessor else 0,
            "machine_blockers": blockers,
            "disclosure": "Deterministic realized attribution; not causal proof.",
        })
    return result


def score_freedoms(problem, index, causal_rows, neighborhood, candidate_pools):
    causal = {row["operation_key"]: row for row in causal_rows}
    keys = list(neighborhood.operation_keys)
    raw = {}
    for key in keys:
        op = problem.operation_by_key[key]
        feature, cause = index.operation_features[key], causal[key]
        alternatives = max(0, len(candidate_pools.get(key, ())) - len(
            index.by_key[key]["machine_ids"]))
        opportunity = alternatives/max(1, len(candidate_pools.get(key, ())))
        simple = (cause["machine_wait_seconds"] +
                  cause["changeover_delay_seconds"] +
                  1000*opportunity)
        route_len = len(index.order_operations[op.order_id])
        downstream = sum(
            problem.operation_by_key[item].sequence >= op.sequence
            for item in index.order_operations[op.order_id])
        impact = (feature["order_tardiness_seconds"] * downstream/max(1, route_len))
        raw[key] = {"simple_raw": simple, "impact_raw": impact,
                    "opportunity": opportunity,
                    "machine_wait": cause["machine_wait_seconds"],
                    "changeover": cause["changeover_delay_seconds"],
                    "tardiness": feature["order_tardiness_seconds"]}
    def normalize(field):
        values = [row[field] for row in raw.values()]
        low, high = min(values), max(values)
        return {key: (row[field]-low)/(high-low) if high > low else 0.0
                for key, row in raw.items()}
    simple_n, impact_n = normalize("simple_raw"), normalize("impact_raw")
    simple_scores, graph_scores = {}, {}
    for key in keys:
        simple_scores[key] = {**raw[key], "score": simple_n[key]}
        graph_scores[key] = {**raw[key],
            "local_resource_normalized": simple_n[key],
            "delivery_impact_normalized": impact_n[key],
            "score": 0.5*simple_n[key]+0.5*impact_n[key]}
    return simple_scores, graph_scores
