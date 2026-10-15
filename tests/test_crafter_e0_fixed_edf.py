from crafter.data_adapter import build_scaling_fixture
from crafter.e0_fixed_edf_gate import (
    _machine_classes,
    _named_variables,
    expected_v2_durations,
    project_seconds,
)
from crafter.model_v2 import build_model_v2


def test_integer_projection_is_explicit_and_bounded():
    exact = project_seconds(12.0)
    fractional = project_seconds(12.4)
    assert exact == {
        "source_seconds": 12.0,
        "projected_integer_seconds": 12,
        "projection_error_seconds": 0.0,
        "already_integer": True,
    }
    assert fractional["already_integer"] is False
    assert abs(fractional["projection_error_seconds"]) <= 0.5


def test_v2_duration_expectation_uses_exact_integer_ceiling():
    operation = build_scaling_fixture(1).operations[0]
    values = expected_v2_durations(operation, 3)
    assert values["productive"] == (
        operation.serial_productive_seconds + 2) // 3
    assert values["duration"] == operation.setup_seconds + values["productive"]
    assert values["machine_load"] == 3 * values["duration"]


def test_internal_variable_index_exposes_every_v2_variable_family():
    problem = build_scaling_fixture(3)
    artifacts = build_model_v2(problem)
    names = _named_variables(artifacts.model)
    assert any(name.startswith("machine_load_") for name in names)
    assert any(name.startswith("class_demand_") for name in names)
    assert any(name.startswith("load_") for name in names)
    assert "makespan" in names
    assert "load_imbalance" in names


def test_e0_machine_class_reconstruction_matches_v2_class_count():
    problem = build_scaling_fixture(6)
    artifacts = build_model_v2(problem)
    members, machine_class, class_sizes = _machine_classes(problem)
    assert len(members) == artifacts.size.machine_equivalence_classes
    assert len(machine_class) == sum(class_sizes.values())
    assert set(machine_class) == {
        machine.machine_id for machine in problem.machines if machine.is_usable
    }
