from copy import deepcopy

import pytest

from web_v1.backend.integrations.craft.residual_preparation import (
    CONTRACT,
    ResidualPreparationError,
    prepare_residual_for_craft,
    validate_preparation,
)
from .evidence_fixtures import committed_packet, splittable_packet


def test_committed_residual_is_prepared_but_not_admitted():
    prepared = prepare_residual_for_craft(committed_packet())
    assert prepared["contract"] == CONTRACT
    assert prepared["status"] == "PREPARED_NOT_ADMITTED"
    assert prepared["craft_input_allowed"] is False
    assert prepared["operations"][0]["craft_operation_key"] == "1_1"
    assert prepared["operations"][0]["immutable_history"]["completed_quantity"] == 40
    assert validate_preparation(prepared)["valid"]


def test_splittable_residual_keeps_remaining_work_and_original_identity():
    prepared = prepare_residual_for_craft(splittable_packet())
    row = prepared["operations"][0]
    assert row["remaining_executable_work"]["remaining_quantity"] == 60
    assert row["remaining_executable_work"]["residual_lot_ids"] == ["lot-3"]
    assert row["craft_operation_key"] == row["original_operation_key"]
    assert validate_preparation(prepared)["valid"]


def test_invalid_evidence_is_rejected_before_preparation():
    packet = committed_packet()
    packet["operations"][0]["remaining_work"]["source"] = "FUTURE_ACTUAL"
    with pytest.raises(ResidualPreparationError):
        prepare_residual_for_craft(packet)


def test_independent_validator_rejects_duplicate_identity_or_open_admission():
    prepared = prepare_residual_for_craft(committed_packet())
    duplicate = deepcopy(prepared)
    duplicate["operations"].append(deepcopy(duplicate["operations"][0]))
    result = validate_preparation(duplicate)
    assert not result["valid"]
    assert "DUPLICATE_OPERATION_IDENTITY" in result["errors"]

    open_gate = deepcopy(prepared)
    open_gate["craft_input_allowed"] = True
    assert not validate_preparation(open_gate)["valid"]


def test_no_scheduler_core_is_created_by_preparation():
    prepared = prepare_residual_for_craft(committed_packet())
    assert "scheduling_problem" not in prepared
    assert "cp_model" not in prepared
    assert prepared["craft_compatibility"] == "BLOCKED"
