from copy import deepcopy

import pytest

from web_v1.backend.integrations.craft.residual_adapter import (
    CONTRACT,
    ResidualAdapterError,
    build_residual_view,
    validate_residual_view,
)
from .evidence_fixtures import committed_packet, splittable_packet


def test_committed_packet_preserves_history_residual_and_fixed_resources():
    view = build_residual_view(committed_packet())
    assert view["contract"] == CONTRACT
    assert view["craft_input_allowed"] is False
    row = view["operations"][0]
    assert row["original_operation_key"] == "1_1"
    assert row["execution_boundary"]["completed_quantity"] == 40
    assert row["execution_boundary"]["remaining_quantity"] == 60
    assert row["residual_work"]["productive_seconds"] == 28800
    assert {r["resource_id"] for r in row["resource_commitments"]} == {"M1", "W1", "L1"}
    assert validate_residual_view(view)["valid"]


def test_splittable_packet_preserves_original_identity_without_admitting_craft():
    view = build_residual_view(splittable_packet())
    row = view["operations"][0]
    assert row["mode"] == "SPLITTABLE_RESIDUAL"
    assert row["residual_work"]["residual_optimizable_in_future"] is True
    assert row["residual_work"]["lot_ids"] == ["lot-3"]
    assert view["craft_compatibility"] == "BLOCKED"
    assert validate_residual_view(view)["valid"]


def test_invalid_evidence_never_reaches_the_adapter():
    packet = committed_packet()
    packet["operations"][0]["quantity_ledger"]["remaining"] = 59
    with pytest.raises(ResidualAdapterError):
        build_residual_view(packet)


def test_residual_lot_quantity_must_reconcile_to_remaining_work():
    packet = committed_packet()
    packet["operations"][0]["continuation_lots"][0]["quantity"] = 19
    with pytest.raises(ResidualAdapterError) as exc:
        build_residual_view(packet)
    assert any(error["code"] == "IN_FLIGHT_LOT_LEDGER_MISMATCH" for error in exc.value.errors)


def test_reordered_packet_has_identical_view_hash():
    packet = committed_packet()
    reordered = deepcopy(packet)
    operation = reordered["operations"][0]
    operation["continuation_lots"].reverse()
    operation["resource_commitments"].reverse()
    for commitment in operation["resource_commitments"]:
        commitment["segments"].reverse()
    first = build_residual_view(packet)
    second = build_residual_view(reordered)
    assert first["view_hash"] == second["view_hash"]
    assert first == second


def test_successor_boundary_and_resource_overlap_are_checked_independently():
    packet = committed_packet()
    packet["operations"][0]["precedence"]["successor_earliest_ready"] = "2026-09-09T21:00:00"
    with pytest.raises(ResidualAdapterError):
        build_residual_view(packet)

    packet = committed_packet()
    packet["operations"][0]["resource_commitments"][0]["segments"][1]["start"] = "2026-09-10T05:30:00"
    with pytest.raises(ResidualAdapterError):
        build_residual_view(packet)


def test_adapter_does_not_mark_real_live_partial_operations_resolved():
    # The live audit has no accepted evidence packets; an empty accepted view
    # is valid only as an adapter shape, never as a resolution claim.
    assert build_residual_view({
        "contract": "RESIDUAL_EVIDENCE_INTAKE_V1",
        "database": "garment_fyp_db",
        "factory_id": 1,
        "as_of": "2026-09-10T00:00:00",
        "source_snapshot_hash": "empty-proof",
        "operations": [],
    })[
        "operations"
    ] == []
