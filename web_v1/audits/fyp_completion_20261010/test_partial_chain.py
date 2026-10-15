import json
from copy import deepcopy
from pathlib import Path

import pytest

from web_v1.audits.fyp_completion_20261010.validate_partial_chain import validate

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent


@pytest.fixture(scope="module")
def captured():
    source = json.loads((ROOT / "web_v1/audits/execution_boundary_v1/factory1_source.json").read_text(encoding="utf-8"))
    snapshot = json.loads((ROOT / "web_v1/audits/execution_boundary_v1/factory1_normalized_snapshot.json").read_text(encoding="utf-8"))
    scenario = json.loads((ROOT / "web_v1/audits/factory1_simulated_continuation_v1/scenario.json").read_text(encoding="utf-8"))
    candidate = json.loads((HERE / "partial_chain_candidate.json").read_text(encoding="utf-8"))
    return source, snapshot, scenario, candidate


def check(captured, changed):
    source, snapshot, scenario, _ = captured
    return validate(source, snapshot, scenario, changed)


def test_saved_partial_chain_is_independently_accepted(captured):
    assert check(captured, captured[3])["status"] == "PASS"


def test_shortened_productive_work_is_rejected(captured):
    changed = deepcopy(captured[3])
    changed["selected_schedule"][1]["worker_segments"] = [[1, 2]]
    assert any("PRODUCTIVE_WORK" in e for e in check(captured, changed)["errors"])


def test_fixed_machine_conflict_is_rejected(captured):
    changed = deepcopy(captured[3])
    changed["selected_schedule"][1]["resource_configuration"]["machine_ids"] = [702]
    changed["selected_schedule"][1]["resource_configuration"]["line_id"] = 8
    changed["selected_schedule"][1]["start_seconds"] = 0
    assert any("MACHINE_OVERLAP" in e for e in check(captured, changed)["errors"])


def test_early_successor_is_rejected(captured):
    changed = deepcopy(captured[3])
    changed["selected_schedule"][2]["start_seconds"] = 0
    assert "FUTURE_WHOLE_OPERATION_PRECEDENCE" in check(captured, changed)["errors"]


def test_fixed_worker_conflict_is_rejected(captured):
    changed = deepcopy(captured[3])
    changed["selected_schedule"][1]["resource_configuration"]["worker_ids"] = ["3838"]
    changed["selected_schedule"][1]["worker_segments"][0][0] = 0
    assert any("WORKER_OVERLAP" in e for e in check(captured, changed)["errors"])


def test_missing_operation_is_rejected(captured):
    changed = deepcopy(captured[3])
    changed["selected_schedule"].pop()
    assert "CANDIDATE_OPERATION_IDENTITY" in check(captured, changed)["errors"]
