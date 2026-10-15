"""Neutral, policy-independent extraction of one factory's frozen workload.

The extractor consumes the authoritative state tuple returned by
``crafter.disruptions.benchmark._load_state``.  It never reads CRAFT rolling
windows, neighborhoods, selected keys, resource-domain caps, or accepted
schedules.  Planned timestamps in the historical phenotype are deliberately
not used as execution truth.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Mapping


def _sha_keys(keys) -> str:
    return hashlib.sha256("\n".join(sorted(str(x) for x in keys)).encode("utf-8")).hexdigest()


def _employee_lookup(mapping: Mapping, employee_id: str):
    if employee_id in mapping:
        return mapping[employee_id]
    if str(employee_id).isdigit() and int(employee_id) in mapping:
        return mapping[int(employee_id)]
    return None


def _employees(values) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in (values or ())}))


@dataclass(frozen=True)
class NeutralFactoryWorkload:
    """Serializable neutral payload plus non-serialized authoritative objects."""

    payload: dict
    problem: Any
    operator_index: Mapping

    def as_dict(self) -> dict:
        return self.payload


def extract_neutral_factory_workload(
    loaded,
    *,
    factory_id: int,
    snapshot_as_of_seconds: int,
    authoritative_execution_facts: Mapping[str, Mapping] | None = None,
) -> NeutralFactoryWorkload:
    """Extract all and only operations owned by ``factory_id``.

    ``authoritative_execution_facts`` is the only input allowed to mark a
    benchmark operation completed, in progress, or committed.  Historical
    planned starts/ends in ``rows`` are not execution facts.
    """

    problem, operator_index, rows, _durations, _details, _route_index, _baseline, _checked, source = loaded
    factory_id = int(factory_id)
    snapshot = int(snapshot_as_of_seconds)
    order_by_id = problem.order_by_id
    factory_orders = tuple(sorted(
        (order for order in problem.orders if int(order.factory_id) == factory_id),
        key=lambda order: str(order.order_id),
    ))
    factory_order_ids = {str(order.order_id) for order in factory_orders}
    operations = tuple(sorted(
        (op for op in problem.operations if str(op.order_id) in factory_order_ids),
        key=lambda op: str(op.operation_key),
    ))
    operation_keys = tuple(str(op.operation_key) for op in operations)
    duplicate_keys = sorted(key for key in set(operation_keys) if operation_keys.count(key) > 1)
    facts = {str(key): dict(value) for key, value in (authoritative_execution_facts or {}).items()}

    route_by_order: dict[str, list] = {}
    for op in operations:
        route_by_order.setdefault(str(op.order_id), []).append(op)
    precedence = []
    predecessor_by_key: dict[str, list[str]] = {str(op.operation_key): [] for op in operations}
    successor_by_key: dict[str, list[str]] = {str(op.operation_key): [] for op in operations}
    for order_id, route in sorted(route_by_order.items()):
        route.sort(key=lambda op: (int(op.sequence), str(op.operation_key)))
        if len({int(op.sequence) for op in route}) != len(route):
            raise ValueError(f"duplicate routing sequence for order {order_id}")
        for predecessor, successor in zip(route, route[1:]):
            left, right = str(predecessor.operation_key), str(successor.operation_key)
            precedence.append({"predecessor": left, "successor": right, "order_id": order_id})
            predecessor_by_key[right].append(left)
            successor_by_key[left].append(right)

    machine_rows = tuple(sorted(
        (machine for machine in problem.machines if int(machine.factory_id) == factory_id),
        key=lambda machine: int(machine.machine_id),
    ))
    machine_by_line: dict[int, list] = {}
    for machine in machine_rows:
        machine_by_line.setdefault(int(machine.line_id), []).append(machine)

    worker_skills: dict[str, set[str]] = {}
    worker_factory: dict[str, int] = {}
    for raw_key, values in operator_index.get("by_factory", {}).items():
        if not isinstance(raw_key, tuple) or len(raw_key) != 2:
            continue
        owner, skill = int(raw_key[0]), str(raw_key[1])
        if owner != factory_id:
            continue
        for employee in values:
            worker = str(employee)
            worker_factory[worker] = owner
            worker_skills.setdefault(worker, set()).add(skill)
    worker_ids = tuple(sorted(worker_factory))

    workers = []
    missing_calendar_workers = []
    for worker in worker_ids:
        crew = _employee_lookup(operator_index.get("crew_of", {}), worker)
        shift = _employee_lookup(operator_index.get("shift_of", {}), worker)
        if crew is not None:
            calendar = {
                "kind": "ROTATING_CREW",
                "rotation_offset": int(crew[0]),
                "rest_day_offset": int(crew[1]),
                "source": "utils.shift_rotation.batch_available_at",
            }
        elif shift is not None:
            calendar = {
                "kind": "FIXED_SHIFT_FLOATER",
                "start_hour": float(shift[0]),
                "end_hour": float(shift[1]),
                "source": "Work_Schedules via operator_index.shift_of",
            }
        else:
            calendar = {"kind": "UNRESOLVED", "source": None}
            missing_calendar_workers.append(worker)
        workers.append({
            "worker_id": worker,
            "factory_id": factory_id,
            "skills": sorted(worker_skills.get(worker, ())),
            "calendar": calendar,
            "assignable_at_snapshot": True,
        })

    categories = {
        "COMPLETED": [], "IN_PROGRESS": [], "COMMITTED_OR_FROZEN_AUTHORITATIVE": [],
        "RELEASED_FUTURE_WORK": [], "FUTURE_RELEASE": [], "EXPLICITLY_EXCLUDED": [],
        "UNRESOLVED": [],
    }
    execution = {}
    operation_payload = []
    for op in operations:
        key = str(op.operation_key)
        order = order_by_id[op.order_id]
        fact = facts.get(key)
        if fact:
            state = str(fact.get("state", "UNRESOLVED")).upper()
            state = state if state in categories else "UNRESOLVED"
            source_name = "authoritative_execution_facts"
        elif int(order.release_seconds) > snapshot:
            state, source_name = "FUTURE_RELEASE", "order.release_seconds"
        else:
            state, source_name = "RELEASED_FUTURE_WORK", "release_and_route_state"
        categories[state].append(key)
        execution[key] = {
            "state": state,
            "source": source_name,
            "belongs_to_benchmark_universe": state != "EXPLICITLY_EXCLUDED",
            "eligible_to_execute_at_snapshot": (
                state == "RELEASED_FUTURE_WORK"
                and not predecessor_by_key[key]
            ),
            "note": "planned historical timestamps were not used as execution truth",
        }

        eligible_machines = []
        for line_id in op.eligible_line_ids:
            for machine in machine_by_line.get(int(line_id), ()): 
                if machine.is_usable and machine.supports(op.required_capability):
                    eligible_machines.append(int(machine.machine_id))
        line_workers = set()
        for line_id in op.eligible_line_ids:
            line_workers.update(str(value) for value in operator_index.get("by_line", {}).get(
                (int(line_id), str(op.required_capability)), ()))
        factory_workers = set(str(value) for value in operator_index.get("by_factory", {}).get(
            (factory_id, str(op.required_capability)), ()))
        eligible_workers = sorted((line_workers | factory_workers) & set(worker_ids))
        operation_payload.append({
            **asdict(op),
            "predecessor_operation_keys": sorted(predecessor_by_key[key]),
            "successor_operation_keys": sorted(successor_by_key[key]),
            "eligible_machine_ids": sorted(set(eligible_machines)),
            "eligible_worker_ids": eligible_workers,
            "execution_state": execution[key],
        })

    for values in categories.values():
        values.sort()
    included = sorted(
        key for state, keys in categories.items()
        if state != "EXPLICITLY_EXCLUDED" for key in keys
    )
    machine_ids = {int(machine.machine_id) for machine in machine_rows}
    machine_commitments = [asdict(row) for row in problem.resources.fixed_machine_commitments
                           if int(row.machine_id) in machine_ids]
    worker_commitments = [asdict(row) for row in problem.resources.fixed_worker_commitments
                          if int(row.factory_id) == factory_id and str(row.employee_id) in set(worker_ids)]

    payload = {
        "contract_version": "NEUTRAL_FACTORY_WORKLOAD_V1",
        "factory_id": factory_id,
        "snapshot_as_of_seconds": snapshot,
        "evaluation_origin": problem.origin.isoformat(),
        "time_unit": "seconds relative to evaluation_origin",
        "source_type": "FROZEN_BENCHMARK_RAW",
        "source_metadata": {
            "problem_id": problem.problem_id,
            "source_summary": str(source),
            "database_access": "READ_ONLY",
            "database_writes": 0,
        },
        "orders": [asdict(order) for order in factory_orders],
        "operations": operation_payload,
        "precedence": precedence,
        "lines": [asdict(line) for line in sorted(problem.lines, key=lambda line: int(line.line_id))
                  if int(line.factory_id) == factory_id],
        "machines": [asdict(machine) for machine in machine_rows],
        "workers": workers,
        "resource_commitments": {
            "machine": machine_commitments,
            "worker": worker_commitments,
        },
        "execution_state": execution,
        "accounting": {
            "RAW_FACTORY_OPERATIONS": len(operation_keys),
            **{name: len(values) for name, values in categories.items()},
            "BENCHMARK_SCHEDULING_UNIVERSE": len(included),
            "raw_operation_keys": sorted(operation_keys),
            "benchmark_operation_keys": included,
            "duplicates": duplicate_keys,
            "identity_holds": len(operation_keys) == sum(len(values) for values in categories.values()),
        },
        "operation_key_sha256": _sha_keys(operation_keys),
        "calendar_contract": {
            "worker": "rotating 06:00-14:00 / 14:00-22:00 / 22:00-06:00 crews with staggered rest days; fixed Work_Schedules shifts for floaters",
            "machine": "usable machines are continuously available in the frozen model; fixed commitments remove benchmark-available capacity",
            "missing_worker_calendar_count": len(missing_calendar_workers),
            "missing_worker_calendar_ids": missing_calendar_workers,
        },
        "policy_independence": {
            "uses_craft_neighborhood": False,
            "uses_craft_selected_keys": False,
            "uses_craft_resource_domain": False,
            "uses_craft_accepted_schedule": False,
            "uses_planned_timestamp_as_execution_truth": False,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    payload["workload_fingerprint_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return NeutralFactoryWorkload(payload=payload, problem=problem, operator_index=operator_index)
