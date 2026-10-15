import pytest
from datetime import datetime

from crafter.contracts import (
    CrafterSolution, CrafterStatus, LabourCapacitySpec, LineSpec, MachineSpec,
    OperationAssignment, OperationSpec, OrderSpec, SchedulingProblem,
    SolverStatistics,
)
from crafter.data_adapter import build_smoke_fixture
from crafter.phenotype import validate
from crafter.solver import solve
from crafter.worker_bridge import allocate_exact_workers


@pytest.fixture(scope="module")
def master():
    fixture = build_smoke_fixture()
    solution = solve(fixture.problem, time_limit_seconds=10.0)
    assert solution.status == CrafterStatus.MASTER_FEASIBLE
    return fixture, solution


def test_exact_worker_bridge_and_independent_validator_pass(master):
    fixture, solution = master
    worker = allocate_exact_workers(fixture.problem, solution, fixture.operator_index)
    assert worker.status == CrafterStatus.WORKER_FEASIBLE
    final = validate(fixture.problem, worker, fixture.operator_index)
    assert final.status == CrafterStatus.VALIDATED_FEASIBLE
    assert final.validation_report["valid"] is True
    assert final.validation_report["counts"]["errors"] == 0
    assert final.phenotype_hash


def test_worker_bridge_preserves_exact_fixed_worker_segments(master):
    fixture, solution = master
    worker = allocate_exact_workers(fixture.problem, solution, fixture.operator_index)
    seen = {}
    for operation in worker.operations:
        for employee, segments in operation.worker_segments:
            for start, end in segments:
                for old_start, old_end in seen.get(employee, ()):
                    assert end <= old_start or old_end <= start
                seen.setdefault(employee, []).append((start, end))
    fixed = fixture.problem.resources.fixed_worker_commitments[0]
    fixed_start = fixture.problem.origin.timestamp()+fixed.start_seconds
    fixed_end = fixture.problem.origin.timestamp()+fixed.end_seconds
    for start, end in seen.get(fixed.employee_id, ()):
        assert end.timestamp() <= fixed_start or fixed_end <= start.timestamp()


def test_worker_infeasibility_is_structured_and_not_reported_final(master):
    fixture, solution = master
    empty_index = {
        "by_line": {}, "by_factory": {}, "by_skill": {},
        "line_factory": {10: 1, 20: 1},
        "shift_of": {}, "crew_of": {}, "efficiency_of": {},
    }
    result = allocate_exact_workers(fixture.problem, solution, empty_index)
    assert result.status == CrafterStatus.MASTER_FEASIBLE_WORKER_INFEASIBLE
    assert result.worker_failure is not None
    assert result.worker_failure.required_workers >= 1
    assert result.worker_failure.reason == "skill"


def test_worker_bridge_preserves_same_worker_pause_resume_segments():
    problem = SchedulingProblem(
        problem_id="segmented-worker-bridge",
        origin=datetime(2026, 9, 21, 7, 0),
        horizon_seconds=100_000,
        lines=(LineSpec(1, 1),),
        machines=(MachineSpec(1, 1, 1, ("a",)),),
        orders=(OrderSpec("O", 1, 0, None),),
        operations=(OperationSpec("O_A", "O", "A", 1, "a", (1,), 10_800, 0),),
        labour_capacities=(LabourCapacitySpec(1, "a", 1),),
        changeover_seconds=1500,
    )
    stats = SolverStatistics("test", "FEASIBLE", 0.0, 0.0, 0.0, 0.0, 0.0,
                             0, 0, 0, 0, 0, 0.0)
    solution = CrafterSolution(
        CrafterStatus.MASTER_FEASIBLE,
        (OperationAssignment("O_A", "O", "A", 1, (1,), 1,
                             0, 0, 90_000, 0, 10_800),),
        stats,
    )
    operator_index = {
        "by_line": {(1, "a"): [7]},
        "by_factory": {(1, "a"): [7]},
        "by_skill": {"a": [7]},
        "line_factory": {1: 1},
        "shift_of": {7: (7.0, 9.0)},
        "crew_of": {}, "efficiency_of": {(7, "a"): 1.0},
    }
    result = allocate_exact_workers(problem, solution, operator_index)
    assert result.status == CrafterStatus.WORKER_FEASIBLE
    segments = result.operations[0].worker_segments[0][1]
    assert len(segments) == 2
    assert segments[0][0] == datetime(2026, 9, 21, 7, 0)
    assert segments[0][1] == datetime(2026, 9, 21, 9, 0)
    assert segments[1][0] == datetime(2026, 9, 22, 7, 0)
    assert segments[1][1] == datetime(2026, 9, 22, 8, 0)
