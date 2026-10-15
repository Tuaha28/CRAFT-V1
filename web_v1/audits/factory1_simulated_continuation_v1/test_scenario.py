"""Simulation isolation and independent rejection tests; no database access."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from web_v1.audits.factory1_simulated_continuation_v1.build_scenario import build
from web_v1.audits.factory1_simulated_continuation_v1.validate_scenario import validate

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


@pytest.fixture(scope="module")
def data():
    source = json.loads((ROOT / "web_v1/audits/execution_boundary_v1/factory1_source.json").read_text())
    normalized = json.loads((ROOT / "web_v1/audits/execution_boundary_v1/factory1_normalized_snapshot.json").read_text())
    scenario = json.loads((HERE / "scenario.json").read_text())
    return source, normalized, scenario


def _rehash(scenario):
    content = {k: v for k, v in scenario.items() if k != "scenario_hash"}
    scenario["scenario_hash"] = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_repeatable_source_derived_scenario(data):
    source, normalized, scenario = data
    first = build(source, normalized)
    second = build(source, normalized)
    assert first == second
    assert first["partial_operations"] == scenario["partial_operations"]
    assert len(first["partial_operations"]) == 82
    assert sum(len(r["fixed_in_flight_holds"]) for r in first["partial_operations"]) == 35
    assert scenario["craft_input_allowed"] is False


def test_independent_validator_accepts_only_simulation_artifact(data):
    assert validate(*data)["status"] == "PASS"


@pytest.mark.parametrize("mutation, expected", [
    ("quantity", "QUANTITY_LEDGER"),
    ("machine", "HOLD_IDENTITY"),
    ("admission", "SIMULATION_ISOLATION_BROKEN"),
])
def test_independent_validator_rejects_mutations(data, mutation, expected):
    source, normalized, original = data
    scenario = copy.deepcopy(original)
    if mutation == "quantity":
        scenario["partial_operations"][0]["unstarted_residual_quantity"] += 1
    elif mutation == "machine":
        row = next(r for r in scenario["partial_operations"] if r["fixed_in_flight_holds"])
        row["fixed_in_flight_holds"][0]["machine_id"] = -1
    else:
        scenario["production_admission_enabled"] = True
    _rehash(scenario)
    result = validate(source, normalized, scenario)
    assert result["status"] == "FAIL"
    assert any(expected in error for error in result["errors"])
