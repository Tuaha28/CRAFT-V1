from copy import deepcopy

from web_v1.backend.integrations.craft.evidence_intake import (
    CONTRACT, normalized_packet, validate_evidence,
)
from .evidence_fixtures import committed_packet, splittable_packet


def test_committed_continuation_across_shift_boundary_is_accepted():
    result = validate_evidence(committed_packet())
    assert result["valid"]
    assert result["craft_compatibility"] == "BLOCKED"


def test_splittable_unstarted_residual_requires_explicit_proof():
    result = validate_evidence(splittable_packet())
    assert result["valid"]
    assert result["craft_compatibility"] == "BLOCKED"


def test_missing_remaining_work_is_rejected():
    packet = committed_packet()
    packet["operations"][0]["remaining_work"]["productive_seconds"] = None
    assert not validate_evidence(packet)["valid"]


def test_future_actual_is_not_evidence():
    packet = committed_packet()
    packet["operations"][0]["remaining_work"]["source"] = "FUTURE_ACTUAL"
    result = validate_evidence(packet)
    assert not result["valid"]
    assert any(e["code"] == "PROJECTED_ACTUAL_NOT_EVIDENCE" for e in result["errors"])


def test_quantity_inconsistency_is_rejected():
    packet = committed_packet()
    packet["operations"][0]["quantity_ledger"]["remaining"] = 59
    assert not validate_evidence(packet)["valid"]


def test_resource_overlap_is_rejected():
    packet = committed_packet()
    packet["operations"][0]["resource_commitments"][0]["segments"][1]["start"] = "2026-09-10T05:00:00"
    result = validate_evidence(packet)
    assert not result["valid"]
    assert any(e["code"] == "RESOURCE_OVERLAP" for e in result["errors"])


def test_stale_authorization_is_rejected():
    packet = committed_packet()
    packet["operations"][0]["provenance"]["valid_until"] = "2026-09-09T23:59:59"
    assert not validate_evidence(packet)["valid"]


def test_post_as_of_evidence_capture_is_allowed_when_valid_for_as_of():
    packet = committed_packet()
    packet["operations"][0]["provenance"]["captured_at"] = "2026-09-10T12:00:00"
    assert validate_evidence(packet)["valid"]


def test_successor_release_before_predecessor_is_rejected():
    packet = committed_packet()
    packet["operations"][0]["precedence"]["successor_earliest_ready"] = "2026-09-09T21:00:00"
    assert not validate_evidence(packet)["valid"]


def test_missing_calendar_or_assignment_is_rejected():
    packet = committed_packet()
    del packet["operations"][0]["resource_availability"]["worker:W1"]
    result = validate_evidence(packet)
    assert not result["valid"]
    assert any(e["code"] == "RESOURCE_CALENDAR_MISSING" for e in result["errors"])


def test_input_order_normalization_is_deterministic():
    packet = committed_packet()
    reordered = deepcopy(packet)
    reordered["operations"] = list(reversed(reordered["operations"]))
    assert normalized_packet(packet) == normalized_packet(reordered)
    assert normalized_packet(packet)["contract"] == CONTRACT


def test_nested_lot_resource_and_segment_order_is_deterministic():
    packet = committed_packet()
    reordered = deepcopy(packet)
    operation = reordered["operations"][0]
    operation["continuation_lots"].reverse()
    operation["resource_commitments"].reverse()
    for commitment in operation["resource_commitments"]:
        commitment["segments"].reverse()
    assert normalized_packet(packet) == normalized_packet(reordered)
