"""Raw factory-state contracts for planning; no future phenotype is embedded."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class FactorySnapshotV1:
    snapshot_id: str
    source_type: str
    factory_id: int
    planning_time_seconds: int
    problem: object
    operator_index: Mapping
    operation_status: Mapping[str, str]
    routing_provenance: str
    calendar_provenance: str
    reservation_provenance: str
    execution_provenance: str
    manager_overrides: tuple = ()
    incumbent_reference: object | None = None
    # Explicit execution-truth epoch.  ``planning_time_seconds`` is retained
    # for compatibility with existing adapters; rolling windows must not use
    # their window origin to reinterpret this snapshot.
    snapshot_as_of_seconds: int | None = None

    def __post_init__(self):
        if self.snapshot_as_of_seconds is None:
            object.__setattr__(self, 'snapshot_as_of_seconds', int(self.planning_time_seconds))
        if int(self.snapshot_as_of_seconds) < 0:
            raise ValueError('snapshot-as-of time cannot be negative')
        forbidden = {'FUTURE_ASSIGNED', 'PLANNED_FROM_DECODER'}
        bad = {k: v for k, v in self.operation_status.items() if v in forbidden}
        if bad:
            raise ValueError(f'future decoder assignment leaked into raw snapshot: {sorted(bad)[:3]}')


def frozen_benchmark_snapshot(*, snapshot_id, factory_id, planning_time_seconds,
                              problem, operator_index, manager_overrides=(),
                              snapshot_as_of_seconds=None):
    """Create a benchmark planning snapshot without decoded future decisions.

    The frozen fixture currently proves master data, calendars and fixed
    reservations. It does not prove actual execution for the old decoder
    phenotype, so all nonterminal operations are explicitly UNPLANNED_FUTURE.
    """
    statuses = {}
    for op in problem.operations:
        order = problem.order_by_id[op.order_id]
        if int(order.factory_id) != int(factory_id):
            continue
        statuses[str(op.operation_key)] = 'UNPLANNED_FUTURE'
    return FactorySnapshotV1(
        snapshot_id=str(snapshot_id), source_type='FROZEN_BENCHMARK',
        factory_id=int(factory_id), planning_time_seconds=int(planning_time_seconds),
        problem=problem, operator_index=operator_index, operation_status=statuses,
        routing_provenance='fixture routing/master data; no decoded future schedule',
        calendar_provenance='fixture operator index and authoritative calendars',
        reservation_provenance='fixed reservation records only',
        execution_provenance='benchmark interpretation: no decoded schedule row is execution proof',
        manager_overrides=tuple(manager_overrides),
        snapshot_as_of_seconds=(int(planning_time_seconds) if snapshot_as_of_seconds is None
                                else int(snapshot_as_of_seconds)))


class ProductionDatabaseAdapter:
    """Read-only production adapter seam; loading is intentionally explicit."""
    def load_factory_snapshot(self, *, factory_id: int, planning_time_seconds: int,
                              snapshot_as_of_seconds: int | None = None):
        raise NotImplementedError(
            'ProductionDatabaseAdapter requires authoritative execution and commitment '
            'queries before full-database planning can be enabled.')
