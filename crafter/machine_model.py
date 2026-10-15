"""Physical-machine mode construction for Crafter's bounded CP-SAT model."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .contracts import MachineSpec, OperationSpec, SchedulingProblem


@dataclass(frozen=True)
class OperationMode:
    operation_key: str
    line_id: int
    machine_count: int
    setup_seconds: int
    productive_seconds: int
    duration_seconds: int
    candidate_machine_ids: Tuple[int, ...]


def legal_modes(problem: SchedulingProblem, operation: OperationSpec) -> Tuple[OperationMode, ...]:
    """Enumerate understandable `(line, k)` modes over physical assets."""
    order = problem.order_by_id[operation.order_id]
    line_by_id = problem.line_by_id
    modes = []
    for line_id in operation.eligible_line_ids:
        line = line_by_id.get(line_id)
        if line is None or line.factory_id != order.factory_id:
            continue
        machines = tuple(sorted(
            m.machine_id for m in problem.machines
            if m.is_usable and m.factory_id == order.factory_id
            and m.line_id == line_id and m.supports(operation.required_capability)
        ))
        upper = min(operation.max_machine_count, len(machines))
        for count in range(operation.min_machine_count, upper + 1):
            productive = (
                operation.serial_productive_seconds + count - 1
            ) // count
            modes.append(OperationMode(
                operation_key=operation.operation_key,
                line_id=line_id,
                machine_count=count,
                setup_seconds=operation.setup_seconds,
                productive_seconds=productive,
                duration_seconds=operation.setup_seconds + productive,
                candidate_machine_ids=machines,
            ))
    return tuple(modes)
