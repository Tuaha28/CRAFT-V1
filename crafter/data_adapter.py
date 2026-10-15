"""Read-only adapters for Crafter.

Only an in-memory, non-final synthetic fixture is implemented for the smoke
gate.  No application database connection is opened by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .contracts import (
    FixedMachineCommitment, FixedWorkerCommitment, LabourCapacitySpec,
    LineSpec, MachineSpec, OperationSpec, OrderSpec, ResourceState,
    SchedulingProblem,
)


@dataclass(frozen=True)
class SmokeFixture:
    problem: SchedulingProblem
    operator_index: dict


def build_smoke_fixture() -> SmokeFixture:
    """Five orders / fifteen operations with real scheduler semantics."""
    origin = datetime(2026, 9, 21, 7, 0, 0)
    lines = (
        LineSpec(10, 1, "L10"),
        LineSpec(20, 1, "L20"),
    )
    # Machines 102 and 202 are deliberately multi-capability.  Per-machine
    # NoOverlap must prevent A/B pool double booking.
    machines = (
        MachineSpec(101, 1, 10, ("a",)),
        MachineSpec(102, 1, 10, ("a", "b")),
        MachineSpec(103, 1, 10, ("b",)),
        MachineSpec(104, 1, 10, ("c",)),
        MachineSpec(105, 1, 10, ("c",)),
        MachineSpec(201, 1, 20, ("a",)),
        MachineSpec(202, 1, 20, ("a", "b")),
        MachineSpec(203, 1, 20, ("b",)),
        MachineSpec(204, 1, 20, ("c",)),
        MachineSpec(205, 1, 20, ("c",)),
    )
    orders = tuple(
        OrderSpec(
            order_id=f"O{index}", factory_id=1,
            release_seconds=(index-1)*600,
            deadline_seconds=8_000 + index*1_800,
            priority_weight=3 if index == 1 else 1,
        )
        for index in range(1, 6)
    )
    operations = []
    route = (
        ("A", "a", 1800, 2),
        ("B", "b", 1200, 2),
        ("C", "c", 900, 2),
    )
    for order in orders:
        for sequence, (operation_id, skill, serial_seconds, max_machines) in enumerate(route, 1):
            operations.append(OperationSpec(
                operation_key=f"{order.order_id}_{operation_id}",
                order_id=order.order_id,
                operation_id=operation_id,
                sequence=sequence,
                required_capability=skill,
                eligible_line_ids=(10, 20),
                serial_productive_seconds=serial_seconds,
                setup_seconds=900,
                min_machine_count=1,
                max_machine_count=max_machines,
            ))
    resources = ResourceState(
        fixed_machine_commitments=(
            FixedMachineCommitment(101, 0, 3600, "EXISTING"),
        ),
        fixed_worker_commitments=(
            FixedWorkerCommitment("1", 1, "a", 0, 1200, "fixed:worker-1"),
        ),
    )
    problem = SchedulingProblem(
        problem_id="crafter-smoke-five-orders-v1",
        origin=origin,
        horizon_seconds=40_000,
        lines=lines,
        machines=machines,
        orders=orders,
        operations=tuple(operations),
        labour_capacities=(
            LabourCapacitySpec(1, "a", 6),
            LabourCapacitySpec(1, "b", 6),
            LabourCapacitySpec(1, "c", 6),
        ),
        resources=resources,
        changeover_seconds=1500,
    )

    by_line, by_factory, by_skill, efficiency = {}, {}, {}, {}
    employee = 1
    # Three workers per (line, skill), enough for a two-machine operation
    # even when worker 1's exact reservation is active.
    for skill in ("a", "b", "c"):
        for line_id in (10, 20):
            pool = list(range(employee, employee+3))
            employee += 3
            by_line[(line_id, skill)] = pool
            by_factory.setdefault((1, skill), []).extend(pool)
            by_skill.setdefault(skill, []).extend(pool)
            for worker in pool:
                efficiency[(worker, skill)] = 1.0
    operator_index = {
        "by_line": by_line,
        "by_factory": by_factory,
        "by_skill": by_skill,
        "line_factory": {10: 1, 20: 1},
        "shift_of": {},
        "crew_of": {},
        "efficiency_of": efficiency,
    }
    return SmokeFixture(problem=problem, operator_index=operator_index)


def build_scaling_fixture(operation_count: int) -> SchedulingProblem:
    """Deterministic, non-final model-construction fixture.

    It deliberately reuses the smoke resource topology so growth measurements
    reflect contention rather than an expanding machine fleet.  It is never
    sourced from or written to the application data store.
    """
    if operation_count < 1:
        raise ValueError("operation_count must be positive")
    smoke = build_smoke_fixture()
    template = smoke.problem
    route = (
        ("A", "a", 1800),
        ("B", "b", 1200),
        ("C", "c", 900),
    )
    order_count = (operation_count + len(route) - 1) // len(route)
    horizon = max(40_000, order_count * 12_000)
    orders = tuple(
        OrderSpec(
            order_id=f"S{index:04d}", factory_id=1,
            release_seconds=((index - 1) % 8) * 300,
            deadline_seconds=min(horizon - 1, 18_000 + index * 1_800),
            priority_weight=3 if index % 10 == 1 else 1,
        )
        for index in range(1, order_count + 1)
    )
    operations = []
    for order in orders:
        for sequence, (operation_id, skill, seconds) in enumerate(route, 1):
            if len(operations) >= operation_count:
                break
            operations.append(OperationSpec(
                operation_key=f"{order.order_id}_{operation_id}",
                order_id=order.order_id,
                operation_id=operation_id,
                sequence=sequence,
                required_capability=skill,
                eligible_line_ids=(10, 20),
                serial_productive_seconds=seconds,
                setup_seconds=900,
                min_machine_count=1,
                max_machine_count=2,
            ))
    return SchedulingProblem(
        problem_id=f"crafter-scaling-{operation_count}-operations",
        origin=template.origin,
        horizon_seconds=horizon,
        lines=template.lines,
        machines=template.machines,
        orders=orders,
        operations=tuple(operations),
        labour_capacities=template.labour_capacities,
        resources=template.resources,
        changeover_seconds=template.changeover_seconds,
    )
