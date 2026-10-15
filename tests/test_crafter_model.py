from collections import defaultdict
from dataclasses import replace

import pytest

from crafter.contracts import (
    CrafterStatus, FixedMachineCommitment, LabourCapacitySpec, LineSpec,
    MachineSpec, OperationSpec, OrderSpec, ResourceState, SchedulingProblem,
)
from crafter.data_adapter import build_scaling_fixture, build_smoke_fixture
from crafter.model import (
    COMPACT_PAIRWISE, CONSERVATIVE_BUFFER, EXPANDED_PAIRWISE, build_model,
)
from crafter.hints import CrafterHint, OperationHint, apply_hint
from crafter.objective import ObjectiveWeights, scalar_objective
from crafter.solver import solve


@pytest.fixture(scope="module")
def solved():
    fixture = build_smoke_fixture()
    solution = solve(fixture.problem, time_limit_seconds=10.0)
    assert solution.status == CrafterStatus.MASTER_FEASIBLE
    return fixture, solution


def test_master_feasible_is_explicitly_not_final_feasibility(solved):
    _fixture, solution = solved
    assert solution.status == CrafterStatus.MASTER_FEASIBLE
    assert solution.status != CrafterStatus.VALIDATED_FEASIBLE
    assert any("provisional" in text for text in solution.diagnostics)


def test_precedence_and_release_seconds(solved):
    fixture, solution = solved
    by_key = {x.operation_key: x for x in solution.assignments}
    for order in fixture.problem.orders:
        chain = sorted(
            (op for op in fixture.problem.operations if op.order_id == order.order_id),
            key=lambda op: op.sequence,
        )
        assert by_key[chain[0].operation_key].start_seconds >= order.release_seconds
        for left, right in zip(chain, chain[1:]):
            assert by_key[left.operation_key].end_seconds <= by_key[right.operation_key].start_seconds


def test_line_and_physical_machine_eligibility(solved):
    fixture, solution = solved
    op_by_key = fixture.problem.operation_by_key
    machine_by_id = fixture.problem.machine_by_id
    order_by_id = fixture.problem.order_by_id
    for row in solution.assignments:
        op = op_by_key[row.operation_key]
        assert row.line_id in op.eligible_line_ids
        assert len(row.machine_ids) == row.machine_count
        assert len(set(row.machine_ids)) == row.machine_count
        for machine_id in row.machine_ids:
            machine = machine_by_id[machine_id]
            assert machine.line_id == row.line_id
            assert machine.factory_id == order_by_id[row.order_id].factory_id
            assert machine.supports(op.required_capability)


def test_machine_no_overlap_and_fixed_commitment(solved):
    fixture, solution = solved
    by_machine = defaultdict(list)
    for fixed in fixture.problem.resources.fixed_machine_commitments:
        by_machine[fixed.machine_id].append((fixed.start_seconds, fixed.end_seconds, "fixed"))
    for row in solution.assignments:
        for machine_id in row.machine_ids:
            by_machine[machine_id].append((row.start_seconds, row.end_seconds, row.operation_key))
    for intervals in by_machine.values():
        intervals.sort()
        for left, right in zip(intervals, intervals[1:]):
            assert left[1] <= right[0], (left, right)


def test_placeholder_changeover_is_respected_on_shared_assets(solved):
    fixture, solution = solved
    by_machine = defaultdict(list)
    for fixed in fixture.problem.resources.fixed_machine_commitments:
        by_machine[fixed.machine_id].append(
            (fixed.start_seconds, fixed.end_seconds, fixed.production_order_id))
    for row in solution.assignments:
        for machine_id in row.machine_ids:
            by_machine[machine_id].append(
                (row.start_seconds, row.end_seconds, row.order_id))
    for intervals in by_machine.values():
        intervals.sort()
        for left, right in zip(intervals, intervals[1:]):
            required_gap = fixture.problem.changeover_seconds if left[2] != right[2] else 0
            assert right[0] >= left[1] + required_gap, (left, right)


def test_machine_count_duration_is_exact(solved):
    fixture, solution = solved
    op_by_key = fixture.problem.operation_by_key
    for row in solution.assignments:
        op = op_by_key[row.operation_key]
        expected_productive = (
            op.serial_productive_seconds + row.machine_count - 1
        ) // row.machine_count
        assert row.productive_seconds == expected_productive
        assert row.end_seconds-row.start_seconds == op.setup_seconds+expected_productive


def test_objective_sign_sanity():
    weights = ObjectiveWeights()
    base = scalar_objective(0, 0, 100, 10, weights)
    assert scalar_objective(1, 0, 100, 10, weights) > base
    assert scalar_objective(0, 1, 100, 10, weights) > base
    assert scalar_objective(0, 0, 101, 10, weights) > base
    assert scalar_objective(0, 0, 100, 11, weights) > base
    with pytest.raises(ValueError):
        ObjectiveWeights(tardiness=-1)


def test_cp_sat_objective_contains_no_negative_penalty_coefficient():
    proto = build_model(build_scaling_fixture(6)).model.Proto()
    assert proto.objective.scaling_factor == 1.0
    assert proto.objective.coeffs
    assert all(coefficient >= 0 for coefficient in proto.objective.coeffs)


@pytest.mark.parametrize(
    "formulation", (EXPANDED_PAIRWISE, COMPACT_PAIRWISE, CONSERVATIVE_BUFFER))
def test_model_size_diagnostics_are_complete(formulation):
    artifacts = build_model(build_scaling_fixture(6), formulation=formulation)
    size = artifacts.size
    assert size.operation_count == 6
    assert size.candidate_modes > 0
    assert size.candidate_machine_assignments > 0
    assert size.total_variables == sum(size.variables_by_category.values())
    assert size.total_constraints == sum(size.constraints_by_category.values())
    assert size.optional_intervals == artifacts.optional_intervals
    if formulation == CONSERVATIVE_BUFFER:
        assert size.changeover_pairs == 0
    else:
        assert size.changeover_pairs > 0
        assert size.changeover_variables == 2 * size.changeover_pairs


def _forced_machine_problem():
    from datetime import datetime
    return SchedulingProblem(
        problem_id="crafter-forced-machine-changeover",
        origin=datetime(2026, 9, 21, 7, 0),
        horizon_seconds=20_000,
        lines=(LineSpec(1, 1),),
        machines=(MachineSpec(1, 1, 1, ("a", "b")),),
        orders=(
            OrderSpec("OLD", 1, 0, None),
            OrderSpec("NEW", 1, 0, 19_000),
        ),
        operations=(
            OperationSpec("NEW_A", "NEW", "A", 1, "a", (1,), 1800, 0),
            OperationSpec("NEW_B", "NEW", "B", 2, "b", (1,), 1200, 0),
        ),
        labour_capacities=(
            LabourCapacitySpec(1, "a", 1), LabourCapacitySpec(1, "b", 1),
        ),
        resources=ResourceState(fixed_machine_commitments=(
            FixedMachineCommitment(1, 0, 1000, "OLD"),
        )),
        changeover_seconds=1500,
    )


@pytest.mark.parametrize("formulation", (COMPACT_PAIRWISE, CONSERVATIVE_BUFFER))
def test_revised_formulations_preserve_machine_identity_and_fixed_commitment(formulation):
    result = solve(
        _forced_machine_problem(), formulation=formulation,
        time_limit_seconds=5.0, max_deterministic_time=0.5,
    )
    assert result.status == CrafterStatus.MASTER_FEASIBLE
    rows = sorted(result.assignments, key=lambda row: row.start_seconds)
    assert all(row.machine_ids == (1,) for row in rows)
    assert rows[0].start_seconds >= 2500  # fixed end + OLD->NEW changeover
    assert rows[0].end_seconds <= rows[1].start_seconds
    if formulation == CONSERVATIVE_BUFFER:
        assert rows[1].start_seconds >= rows[0].end_seconds + 1500


def test_compact_pairwise_uses_fixed_machine_last_order_identity():
    problem = _forced_machine_problem()
    same_order_resources = replace(
        problem.resources,
        fixed_machine_commitments=(FixedMachineCommitment(1, 0, 1000, "NEW"),),
    )
    result = solve(
        replace(problem, resources=same_order_resources),
        formulation=COMPACT_PAIRWISE,
        time_limit_seconds=5.0,
        max_deterministic_time=0.5,
    )
    rows = sorted(result.assignments, key=lambda row: row.start_seconds)
    assert rows[0].start_seconds == 1000


def test_worker_capacity_bound_prunes_only_impossible_machine_counts():
    problem = build_scaling_fixture(6)
    capacities = tuple(replace(row, capacity=1) for row in problem.labour_capacities)
    artifacts = build_model(replace(problem, labour_capacities=capacities))
    assert all(
        mode.machine_count == 1
        for modes in artifacts.modes.values()
        for mode in modes
    )


def test_compact_selection_prevents_multicapability_machine_double_booking():
    fixture = build_smoke_fixture()
    result = solve(
        fixture.problem, formulation=COMPACT_PAIRWISE,
        time_limit_seconds=5.0, max_deterministic_time=0.5,
    )
    assert result.status == CrafterStatus.MASTER_FEASIBLE
    by_machine = defaultdict(list)
    for row in result.assignments:
        for machine_id in row.machine_ids:
            by_machine[machine_id].append((row.start_seconds, row.end_seconds))
    for intervals in by_machine.values():
        intervals.sort()
        assert all(left[1] <= right[0] for left, right in zip(intervals, intervals[1:]))


def test_full_hint_is_optional_and_only_populates_solution_hint(solved):
    fixture, solution = solved
    artifacts = build_model(fixture.problem, formulation=COMPACT_PAIRWISE)
    constraint_count = len(artifacts.model.Proto().constraints)
    hint = CrafterHint(
        source="validated-smoke",
        operations=tuple(
            OperationHint(
                row.operation_key, row.start_seconds, row.line_id, row.machine_ids)
            for row in solution.assignments
        ),
    )
    apply_hint(fixture.problem, artifacts, hint)
    proto = artifacts.model.Proto()
    assert len(proto.solution_hint.vars) > 0
    assert len(proto.constraints) == constraint_count
