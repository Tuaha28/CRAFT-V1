"""Isolated experimental CP-SAT scheduler.

Nothing in the production GA imports this package.  Crafter may depend on
stable, read-only scheduling primitives, but the dependency never points in
the other direction.
"""

from .contracts import (
    CrafterPhenotype,
    CrafterSolution,
    CrafterStatus,
    FixedMachineCommitment,
    FixedWorkerCommitment,
    LabourCapacitySpec,
    LineSpec,
    MachineSpec,
    OperationAssignment,
    OperationSpec,
    OrderSpec,
    ResourceState,
    SchedulingProblem,
)

__all__ = [
    "CrafterPhenotype", "CrafterSolution", "CrafterStatus",
    "FixedMachineCommitment", "FixedWorkerCommitment",
    "LabourCapacitySpec", "LineSpec", "MachineSpec",
    "OperationAssignment", "OperationSpec", "OrderSpec",
    "ResourceState", "SchedulingProblem",
]
