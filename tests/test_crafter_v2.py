from collections import defaultdict
from dataclasses import replace

import pytest

from crafter.contracts import (
    CrafterSolution, CrafterStatus, OperationAssignment, SolverStatistics,
)
from crafter.data_adapter import build_scaling_fixture, build_smoke_fixture
from crafter.machine_materializer import materialize_machines
from crafter.model import CONSERVATIVE_BUFFER
from crafter.model_v2 import build_model_v2
from crafter.objective import scalar_objective
from crafter.phenotype import validate
from crafter.solver import solve
from crafter.solver_v2 import solve_v2
from crafter.worker_bridge import allocate_exact_workers


@pytest.fixture(scope="module")
def v2_smoke():
    fixture = build_smoke_fixture()
    master, artifacts = solve_v2(
        fixture.problem, time_limit_seconds=15, max_deterministic_time=3)
    assert master.status == CrafterStatus.MASTER_FEASIBLE
    machine = materialize_machines(fixture.problem, master)
    assert machine.status == CrafterStatus.MACHINE_FEASIBLE
    worker = allocate_exact_workers(
        fixture.problem, machine.solution, fixture.operator_index)
    final = validate(fixture.problem, worker, fixture.operator_index)
    assert final.status == CrafterStatus.VALIDATED_FEASIBLE
    return fixture, master, artifacts, machine, final


def test_compressed_line_choice_and_machine_count_are_legal(v2_smoke):
    fixture, master, _artifacts, _machine, _final = v2_smoke
    for row in master.assignments:
        op = fixture.problem.operation_by_key[row.operation_key]
        assert row.line_id in op.eligible_line_ids
        assert op.min_machine_count <= row.machine_count <= op.max_machine_count


def test_exact_k_to_duration_lookup(v2_smoke):
    fixture, master, _artifacts, _machine, _final = v2_smoke
    for row in master.assignments:
        op = fixture.problem.operation_by_key[row.operation_key]
        expected = (op.serial_productive_seconds+row.machine_count-1)//row.machine_count
        assert row.productive_seconds == expected
        assert row.end_seconds-row.start_seconds == op.setup_seconds+expected


def test_operation_level_labour_demand_is_not_per_mode():
    problem = build_scaling_fixture(15)
    artifacts = build_model_v2(problem)
    assert artifacts.size.constraints_by_category["labour_intervals"] == 15
    assert artifacts.size.implicit_line_count_modes == 60
    assert artifacts.size.optional_intervals == 30


def test_v2_objective_coefficients_and_signs_are_nonnegative():
    proto = build_model_v2(build_scaling_fixture(6)).model.Proto()
    assert proto.objective.coeffs
    assert all(value >= 0 for value in proto.objective.coeffs)
    base = scalar_objective(3, 2, 1_000, 100)
    assert scalar_objective(4, 2, 1_000, 100) > base
    assert scalar_objective(3, 3, 1_000, 100) > base
    assert scalar_objective(3, 2, 1_001, 100) > base
    assert scalar_objective(3, 2, 1_000, 101) > base


def test_physical_materialization_and_multicapability_no_overlap(v2_smoke):
    fixture, _master, _artifacts, machine, _final = v2_smoke
    occupied = defaultdict(list)
    for row in machine.solution.assignments:
        assert len(row.machine_ids) == row.machine_count
        spec = fixture.problem.operation_by_key[row.operation_key]
        for machine_id in row.machine_ids:
            physical = fixture.problem.machine_by_id[machine_id]
            assert physical.line_id == row.line_id
            assert physical.supports(spec.required_capability)
            occupied[machine_id].append((
                row.start_seconds,
                row.end_seconds+fixture.problem.changeover_seconds))
    for intervals in occupied.values():
        intervals.sort()
        assert all(left[1] <= right[0]
                   for left, right in zip(intervals, intervals[1:]))


def test_fixed_machine_commitment_is_respected(v2_smoke):
    fixture, _master, _artifacts, machine, _final = v2_smoke
    fixed = fixture.problem.resources.fixed_machine_commitments[0]
    fixed_end = fixed.end_seconds+fixture.problem.changeover_seconds
    for row in machine.solution.assignments:
        if fixed.machine_id in row.machine_ids:
            assert row.start_seconds >= fixed_end or (
                row.end_seconds+fixture.problem.changeover_seconds
                <= fixed.start_seconds)


def test_materialization_failure_state_is_structured(v2_smoke):
    fixture, master, _artifacts, _machine, _final = v2_smoke
    # Force two overlapping two-machine A operations onto line 10, which has
    # only machines 101/102 compatible with A (and 101 is fixed initially).
    a_rows = [row for row in master.assignments
              if fixture.problem.operation_by_key[row.operation_key].required_capability == "a"][:2]
    forced = tuple(replace(row, line_id=10, machine_count=2,
                           start_seconds=0, productive_start_seconds=900,
                           end_seconds=1800, productive_seconds=900)
                   for row in a_rows)
    result = materialize_machines(
        fixture.problem, replace(master, assignments=forced))
    assert result.status == CrafterStatus.MASTER_FEASIBLE_MACHINE_INFEASIBLE
    assert result.failure is not None
    assert result.failure.reason


def test_worker_bridge_and_validator_are_unchanged(v2_smoke):
    _fixture, _master, _artifacts, machine, final = v2_smoke
    assert machine.solution.status == CrafterStatus.MACHINE_FEASIBLE
    assert final.status == CrafterStatus.VALIDATED_FEASIBLE
    assert final.validation_report["valid"] is True


def test_v1_v2_tiny_semantics_are_comparable(v2_smoke):
    fixture, _master, _artifacts, _machine, final_v2 = v2_smoke
    v1 = solve(fixture.problem, formulation=CONSERVATIVE_BUFFER,
               time_limit_seconds=15, max_deterministic_time=3)
    final_v1 = validate(
        fixture.problem,
        allocate_exact_workers(fixture.problem, v1, fixture.operator_index),
        fixture.operator_index)
    assert final_v1.status == final_v2.status == CrafterStatus.VALIDATED_FEASIBLE
    assert len(final_v1.operations) == len(final_v2.operations) == 15
    assert {item.assignment.operation_key for item in final_v1.operations} == {
        item.assignment.operation_key for item in final_v2.operations}


def test_compressed_size_attribution_is_complete():
    size = build_model_v2(build_scaling_fixture(15)).size
    assert size.total_variables == sum(size.variables_by_category.values())
    assert size.total_constraints == sum(size.constraints_by_category.values())
    assert size.optional_intervals == size.eligible_line_choices
    assert size.machine_equivalence_classes > 0
    assert size.class_allocation_variables > 0
