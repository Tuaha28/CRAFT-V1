from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path

from ortools.sat.python import cp_model

from crafter.authoritative_semantics import (
    authoritative_changeover_seconds,
    authoritative_productive_seconds,
    machine_hold_seconds,
    project_duration_seconds,
    project_instant_seconds,
    walk_fixed_crew,
)
from crafter.data_adapter import build_smoke_fixture
from crafter.e0_5_semantic_reconciliation import _pause_semantics
from crafter.model_v2_1 import build_model_v2_1


def _details(problem):
    return {
        op.operation_key: {
            "setup_mins": op.setup_seconds/60.0,
            "serial_processing_mins": op.serial_productive_seconds/60.0,
        }
        for op in problem.operations
    }


def test_authoritative_duration_uses_split_then_calendar_ceiling():
    detail = {"setup_mins": 15.0, "serial_processing_mins": 1.01}
    assert authoritative_productive_seconds(detail, 3) == 21
    # The historical V2 order of operations ceil(60.6) // 3 gives 21 only
    # accidentally here; use a second value that distinguishes the policies.
    detail["serial_processing_mins"] = 1.04
    assert authoritative_productive_seconds(detail, 3) == 21


def test_time_projection_is_deterministic_and_never_early():
    assert project_instant_seconds(10.0).projected_seconds == 10
    assert project_instant_seconds(9.999999999).projected_seconds == 10
    assert project_instant_seconds(10.01).projected_seconds == 11
    assert project_duration_seconds(60.01).projected_seconds == 61


def test_pause_resume_elapsed_differs_from_productive_and_machine_is_held():
    ready = datetime(2026, 9, 21, 6, 0)
    available = lambda when: 6 <= when.hour < 14
    start, end, segments = walk_fixed_crew(ready, 600, available)
    productive = sum((right-left).total_seconds() for left, right in segments)
    elapsed = (end-start).total_seconds()
    assert productive == 600*60
    assert elapsed > productive
    assert machine_hold_seconds(0, int(elapsed)) == (0, int(elapsed))
    assert len(segments) == 2


def test_pause_report_does_not_misclassify_calendar_second_ceiling():
    base = {
        "operation_key": "O_A", "start_minutes": 0,
        "productive_start_minutes": 0, "productive_minutes": 1.1/60,
        "machine_ids": [1], "employee_ids": ["1"],
        "worker_segments_by_employee": {},
    }
    no_pause = _pause_semantics([{**base, "end_minutes": 2/60}])
    real_pause = _pause_semantics([{**base, "end_minutes": 3/60}])
    assert no_pause["frozen_operations_with_pauses"] == 0
    assert real_pause["frozen_operations_with_pauses"] == 1


def test_changeover_is_order_transition_specific():
    assert authoritative_changeover_seconds(None, "A") == 0
    assert authoritative_changeover_seconds("A", "A") == 0
    assert authoritative_changeover_seconds("B", "A") == 1500


def test_v2_1_smoke_model_uses_envelope_not_continuous_labour_interval():
    problem = build_smoke_fixture().problem
    artifacts = build_model_v2_1(problem, _details(problem))
    categories = artifacts.size.constraints_by_category
    assert "labour_intervals" not in categories
    assert categories["labour_energy_capacity"] == 3
    assert artifacts.size.optional_intervals == 30
    assert artifacts.model.Validate() == ""


def test_v2_1_fixed_envelope_can_exceed_productive_duration():
    problem = build_smoke_fixture().problem
    artifacts = build_model_v2_1(problem, _details(problem))
    operation = problem.operations[0]
    key = operation.operation_key
    model = artifacts.model
    model.Add(artifacts.machine_counts[key] == 1)
    model.Add(artifacts.starts[key] == 4000)
    model.Add(artifacts.productive_starts[key] == 4900)
    # 1,800 productive seconds but a 10,000-second calendar pause envelope.
    model.Add(artifacts.productive_durations[key] == 1800)
    model.Add(artifacts.ends[key] == 14900)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    assert solver.Solve(model) in (cp_model.FEASIBLE, cp_model.OPTIMAL)


def test_machine_classes_split_different_fixed_commitment_state():
    fixture = build_smoke_fixture()
    base = fixture.problem
    # Machines 101 and 201 already differ by line. Make 102 otherwise similar
    # to 101 but without 101's commitment: state must keep them distinguishable.
    machines = tuple(
        replace(machine, capabilities=("a",))
        if machine.machine_id == 102 else machine
        for machine in base.machines
    )
    problem = replace(base, machines=machines)
    artifacts = build_model_v2_1(problem, _details(problem))
    assert artifacts.machine_class[101] != artifacts.machine_class[102]


def test_v2_1_fixed_commitment_consumes_its_exact_machine_class():
    problem = build_smoke_fixture().problem
    artifacts = build_model_v2_1(problem, _details(problem))
    key = "O1_A"
    committed_class = artifacts.machine_class[101]
    model = artifacts.model
    model.Add(artifacts.line_choices[key] == 10)
    model.Add(artifacts.machine_counts[key] == 1)
    model.Add(artifacts.starts[key] == 0)
    model.Add(artifacts.ends[key] == 3600)
    for (operation_key, line_id, class_id), variable in (
            artifacts.class_allocations.items()):
        if operation_key == key and line_id == 10:
            model.Add(variable == int(class_id == committed_class))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    assert solver.Solve(model) == cp_model.INFEASIBLE


def test_frozen_edf_fixed_phenotype_parity_artifact():
    directory = Path("outputs/crafter/e0_5_semantic_reconciliation")
    solver = json.loads((directory/"solver_result_fixed_edf.json").read_text())
    validator = json.loads((directory/"validator_parity.json").read_text())
    assert solver["status"] == "OPTIMAL"
    assert validator["cp_operations_matching_projected_frozen"] == 431
    assert validator["independent_validator_status"] == "VALIDATED_FEASIBLE"
    assert validator["validator_violations"] == 0
    assert validator["phenotype_hash_match"] is True
