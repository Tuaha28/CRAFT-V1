"""Immutable domain contracts for the isolated Crafter experiment."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional, Tuple


class CrafterStatus(str, Enum):
    NOT_SOLVED = "NOT_SOLVED"
    MASTER_FEASIBLE = "MASTER_FEASIBLE"
    MACHINE_FEASIBLE = "MACHINE_FEASIBLE"
    MASTER_FEASIBLE_MACHINE_INFEASIBLE = "MASTER_FEASIBLE_MACHINE_INFEASIBLE"
    MASTER_INFEASIBLE = "MASTER_INFEASIBLE"
    MASTER_UNKNOWN = "MASTER_UNKNOWN"
    MASTER_FEASIBLE_WORKER_INFEASIBLE = "MASTER_FEASIBLE_WORKER_INFEASIBLE"
    WORKER_FEASIBLE = "WORKER_FEASIBLE"
    VALIDATED_FEASIBLE = "VALIDATED_FEASIBLE"
    VALIDATION_FAILED = "VALIDATION_FAILED"


@dataclass(frozen=True)
class LineSpec:
    line_id: int
    factory_id: int
    name: str = ""


@dataclass(frozen=True)
class MachineSpec:
    machine_id: int
    factory_id: int
    line_id: int
    capabilities: Tuple[str, ...]
    is_usable: bool = True

    def supports(self, capability: str) -> bool:
        wanted = capability.strip().lower()
        return wanted in {x.strip().lower() for x in self.capabilities}


@dataclass(frozen=True)
class OrderSpec:
    order_id: str
    factory_id: int
    release_seconds: int
    deadline_seconds: Optional[int]
    priority_weight: int = 1


@dataclass(frozen=True)
class OperationSpec:
    operation_key: str
    order_id: str
    operation_id: str
    sequence: int
    required_capability: str
    eligible_line_ids: Tuple[int, ...]
    serial_productive_seconds: int
    setup_seconds: int = 900
    min_machine_count: int = 1
    max_machine_count: int = 1


@dataclass(frozen=True)
class LabourCapacitySpec:
    factory_id: int
    skill: str
    capacity: int


@dataclass(frozen=True)
class FixedMachineCommitment:
    machine_id: int
    start_seconds: int
    end_seconds: int
    production_order_id: str


@dataclass(frozen=True)
class FixedWorkerCommitment:
    employee_id: str
    factory_id: int
    skill: str
    start_seconds: int
    end_seconds: int
    operation_key: str = "fixed"


@dataclass(frozen=True)
class ResourceState:
    fixed_machine_commitments: Tuple[FixedMachineCommitment, ...] = ()
    fixed_worker_commitments: Tuple[FixedWorkerCommitment, ...] = ()


@dataclass(frozen=True)
class SchedulingProblem:
    problem_id: str
    origin: datetime
    horizon_seconds: int
    lines: Tuple[LineSpec, ...]
    machines: Tuple[MachineSpec, ...]
    orders: Tuple[OrderSpec, ...]
    operations: Tuple[OperationSpec, ...]
    labour_capacities: Tuple[LabourCapacitySpec, ...]
    resources: ResourceState = field(default_factory=ResourceState)
    changeover_seconds: int = 1500

    def __post_init__(self):
        if self.horizon_seconds <= 0:
            raise ValueError("horizon_seconds must be positive")
        for op in self.operations:
            if op.serial_productive_seconds <= 0 or op.setup_seconds < 0:
                raise ValueError(f"invalid duration for {op.operation_key}")
            if not 1 <= op.min_machine_count <= op.max_machine_count:
                raise ValueError(f"invalid machine-count range for {op.operation_key}")
        keys = [op.operation_key for op in self.operations]
        if len(keys) != len(set(keys)):
            raise ValueError("operation keys must be unique")

    @property
    def line_by_id(self):
        return {x.line_id: x for x in self.lines}

    @property
    def machine_by_id(self):
        return {x.machine_id: x for x in self.machines}

    @property
    def order_by_id(self):
        return {x.order_id: x for x in self.orders}

    @property
    def operation_by_key(self):
        return {x.operation_key: x for x in self.operations}


@dataclass(frozen=True)
class OperationAssignment:
    operation_key: str
    order_id: str
    operation_id: str
    line_id: int
    machine_ids: Tuple[int, ...]
    machine_count: int
    start_seconds: int
    productive_start_seconds: int
    end_seconds: int
    setup_seconds: int
    productive_seconds: int


@dataclass(frozen=True)
class SolverStatistics:
    ortools_version: str
    cp_status: str
    wall_time_seconds: float
    deterministic_time: Optional[float]
    objective_value: Optional[float]
    best_bound: Optional[float]
    relative_gap: Optional[float]
    variables: int
    constraints: int
    optional_intervals: int
    conflicts: int
    branches: int
    peak_rss_mb: Optional[float]


@dataclass(frozen=True)
class CrafterSolution:
    status: CrafterStatus
    assignments: Tuple[OperationAssignment, ...]
    statistics: SolverStatistics
    objective_components: Tuple[Tuple[str, int], ...] = ()
    diagnostics: Tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerFailure:
    operation_key: str
    line_id: int
    machine_count: int
    start_seconds: int
    end_seconds: int
    required_skill: str
    required_workers: int
    available_workers: int
    reason: str
    machine_ids: Tuple[int, ...] = ()
    actual_worker_completion_seconds: Optional[int] = None
    extension_required_seconds: Optional[int] = None


@dataclass(frozen=True)
class MachineFailure:
    operation_key: str
    line_id: int
    machine_count: int
    start_seconds: int
    end_seconds: int
    required_capability: str
    candidate_machine_ids: Tuple[int, ...]
    available_machine_ids: Tuple[int, ...]
    reason: str


@dataclass(frozen=True)
class MachineMaterializationResult:
    status: CrafterStatus
    solution: CrafterSolution
    failure: Optional[MachineFailure] = None
    diagnostics: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PhenotypeOperation:
    assignment: OperationAssignment
    employee_ids: Tuple[str, ...]
    worker_segments: Tuple[Tuple[str, Tuple[Tuple[datetime, datetime], ...]], ...]
    sourcing_tier: str


@dataclass(frozen=True)
class CrafterPhenotype:
    status: CrafterStatus
    operations: Tuple[PhenotypeOperation, ...]
    worker_failure: Optional[WorkerFailure] = None
    phenotype_hash: Optional[str] = None
    comparison_metrics: Tuple[Tuple[str, Any], ...] = ()
    validation_report: Optional[Mapping[str, Any]] = None
