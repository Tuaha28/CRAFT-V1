"""Profile the authoritative frozen workload selected for the 172-op run."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")

from crafter.disruptions.benchmark import _load_state
from crafter.lns_probe.line_k_model import _available_machine_pool


def _flatten_ids(value):
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(_flatten_ids(item))
        return out
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(_flatten_ids(item))
        return out
    return [] if value is None else [str(value)]


def main():
    loaded = _load_state()
    problem, operator_index, _rows, _durations, _details, _route_index, _baseline, _checked, source = loaded
    factory_id = 2
    order_factory = {str(order.order_id): int(order.factory_id) for order in problem.orders}
    ops = [op for op in problem.operations if order_factory[str(op.order_id)] == factory_id]
    op_keys = sorted(str(op.operation_key) for op in ops)
    order_ids = sorted({str(op.order_id) for op in ops})
    line_ids = sorted({int(line) for op in ops for line in op.eligible_line_ids
                       if int(problem.line_by_id[int(line)].factory_id) == factory_id})
    machine_ids = sorted(int(machine.machine_id) for machine in problem.machines
                         if int(machine.factory_id) == factory_id)
    by_factory = operator_index.get("by_factory", {})
    factory_worker_values = []
    for key, value in by_factory.items():
        if isinstance(key, tuple) and key and int(key[0]) == factory_id:
            factory_worker_values.append(value)
        elif str(key) in {str(factory_id), f"({factory_id},)"}:
            factory_worker_values.append(value)
    worker_ids = sorted(set(_flatten_ids(factory_worker_values)))
    if not worker_ids:
        # Keep the source truthful if the adapter uses only by-line/by-skill views.
        worker_ids = sorted(set(_flatten_ids({line: operator_index.get("by_line", {}).get(line, ())
                                               for line in line_ids})))
    line_counts = Counter(int(line) for op in ops for line in op.eligible_line_ids
                          if int(problem.line_by_id[int(line)].factory_id) == factory_id)
    machine_pool_sizes = []
    for op in ops:
        for line in op.eligible_line_ids:
            line_spec = problem.line_by_id.get(int(line))
            if line_spec is not None and int(line_spec.factory_id) == factory_id:
                machine_pool_sizes.append(len(tuple(_available_machine_pool(problem, op, int(line)))))
    payload = {
        "source_type": "FROZEN_BENCHMARK_RAW",
        "source_provenance": str(source),
        "factory_id": factory_id,
        "snapshot_as_of_seconds": 0,
        "execution_truth_contract": "historical decoder/planned timestamps are not execution evidence",
        "total_factory_operations": len(op_keys),
        "completed_count": 0,
        "in_progress_count": 0,
        "schedulable_future_count": len(op_keys),
        "explicit_exclusions": [],
        "operations_requiring_scheduling": op_keys,
        "order_count": len(order_ids),
        "order_ids": order_ids,
        "routing_count": len(order_ids),
        "line_count": len(line_ids),
        "line_ids": line_ids,
        "machine_pool_count": len(machine_ids),
        "machine_ids": machine_ids,
        "worker_pool_count": len(worker_ids),
        "worker_ids": worker_ids,
        "operation_eligibility": {
            "eligible_lines_per_operation": {
                str(op.operation_key): sorted(int(line) for line in op.eligible_line_ids
                                              if int(problem.line_by_id[int(line)].factory_id) == factory_id)
                for op in ops
            },
            "eligible_line_frequency": {str(k): int(v) for k, v in sorted(line_counts.items())},
            "machine_pool_sizes_by_eligible_line": {
                "min": min(machine_pool_sizes, default=0),
                "median": sorted(machine_pool_sizes)[len(machine_pool_sizes)//2] if machine_pool_sizes else 0,
                "max": max(machine_pool_sizes, default=0),
                "samples": len(machine_pool_sizes),
            },
        },
        "accounting_identity": {
            "universe": len(op_keys), "completed": 0, "in_progress": 0,
            "excluded": 0, "schedulable": len(op_keys),
        },
    }
    out = Path("outputs/crafter/craft_172_scale/workload_accounting.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"factory_id": factory_id, "operations": len(op_keys), "orders": len(order_ids),
                      "lines": len(line_ids), "machines": len(machine_ids), "workers": len(worker_ids)}))


if __name__ == "__main__":
    main()
