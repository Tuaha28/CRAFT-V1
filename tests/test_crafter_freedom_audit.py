from pathlib import Path
import json

from crafter.authoritative_semantics import authoritative_productive_seconds
from crafter.data_adapter import build_smoke_fixture
from crafter.lns_probe.analysis import build_incumbent_index
from crafter.lns_probe.freedom_audit import (
    _global_machine_reservations, audit_lines_and_k,
    audit_machine_identity, audit_timing, audit_workers,
    build_masks_and_recommendations,
)
from crafter.lns_probe.resource_graph import (
    build_resource_state_graph, causal_waiting,
)


def _fixture():
    fixture = build_smoke_fixture()
    payload = json.loads(Path(
        "outputs/crafter/crafter_smoke_result.json").read_text())
    rows = []
    for item in payload["phenotype"]["operations"]:
        assignment = item["assignment"]
        rows.append({
            "operation_key": assignment["operation_key"],
            "production_order_id": assignment["order_id"],
            "operation_id": assignment["operation_id"],
            "line_id": assignment["line_id"],
            "machine_ids": assignment["machine_ids"],
            "employee_ids": item["employee_ids"],
            "start_minutes": assignment["start_seconds"]/60,
            "productive_start_minutes": assignment["productive_start_seconds"]/60,
            "end_minutes": assignment["end_seconds"]/60,
            "setup_minutes": assignment["setup_seconds"]/60,
            "productive_minutes": assignment["productive_seconds"]/60,
            "worker_segments_by_employee": {
                employee: segments for employee, segments in item["worker_segments"]},
            "operator_sourcing_tier": item["sourcing_tier"],
        })
    details = {operation.operation_key: {
        "setup_mins": operation.setup_seconds/60,
        "serial_processing_mins": operation.serial_productive_seconds/60,
    } for operation in fixture.problem.operations}
    index = build_incumbent_index(fixture.problem, rows, details)
    return fixture, rows, details, index


def test_freedom_audit_is_deterministic_and_incumbent_timing_is_present():
    fixture, _rows, details, index = _fixture()
    first = audit_timing(
        fixture.problem, index, details, fixture.operator_index)
    second = audit_timing(
        fixture.problem, index, details, fixture.operator_index)
    assert first == second
    assert all(row["incumbent_option_present"] for row in first)


def test_machine_identity_freedom_requires_a_different_exact_set():
    fixture, _rows, _details, index = _fixture()
    reservations = _global_machine_reservations(fixture.problem, index)
    rows = audit_machine_identity(fixture.problem, index, reservations)
    for row in rows:
        assert row["machine_id_freedom"] == (
            row["different_machine_combination_count"] > 0)
        if row["machine_id_freedom"]:
            assert row["usable_alternative_machine_ids"]
        for machine_id in row["physically_realizable_machine_ids_at_incumbent_envelope"]:
            assert fixture.problem.machine_by_id[machine_id].is_usable


def test_line_and_k_audit_respects_factory_eligibility_and_authoritative_duration():
    fixture, _rows, details, index = _fixture()
    reservations = _global_machine_reservations(fixture.problem, index)
    lines1, counts1, combinations1 = audit_lines_and_k(
        fixture.problem, index, details, fixture.operator_index, reservations)
    lines2, counts2, combinations2 = audit_lines_and_k(
        fixture.problem, index, details, fixture.operator_index, reservations)
    assert (lines1, counts1, combinations1) == (lines2, counts2, combinations2)
    for row in lines1:
        factory = row["factory_id"]
        assert all(fixture.problem.line_by_id[line_id].factory_id == factory
                   for line_id in row["legal_eligible_line_ids"])
    for row in counts1:
        operation_key = row["operation_key"]
        operation = fixture.problem.operation_by_key[operation_key]
        for estimate in row["k_estimates"]:
            assert estimate["productive_seconds"] == (
                authoritative_productive_seconds(details[operation_key], estimate["k"]))
            assert estimate["k"] <= estimate["usable_machine_count"]
            assert estimate["k"] <= estimate["factory_qualified_worker_count"]
        assert operation.min_machine_count <= row["incumbent_k"] <= operation.max_machine_count


def test_worker_screen_and_masks_cover_every_operation_without_dense_graph_edges():
    fixture, _rows, details, index = _fixture()
    reservations = _global_machine_reservations(fixture.problem, index)
    machine = audit_machine_identity(fixture.problem, index, reservations)
    timing = audit_timing(fixture.problem, index, details, fixture.operator_index)
    line, kval, combined = audit_lines_and_k(
        fixture.problem, index, details, fixture.operator_index, reservations)
    workers = audit_workers(fixture.problem, index, details, fixture.operator_index)
    causal = causal_waiting(fixture.problem, index, details)
    masks, _classes, _alignment, _recommendations, _candidates = (
        build_masks_and_recommendations(
            fixture.problem, index, causal, timing, machine,
            line, kval, workers, combined))
    assert len(masks) == len(fixture.problem.operations) == 15
    assert all(row["status"] == "WORKER_FREEDOM_SCREEN_ONLY" for row in workers)
    graph1 = build_resource_state_graph(
        fixture.problem, index, details, fixture.operator_index)
    graph2 = build_resource_state_graph(
        fixture.problem, index, details, fixture.operator_index)
    assert graph1["nodes"] == graph2["nodes"]
    assert graph1["edges"] == graph2["edges"]
    assert graph1["statistics"]["potential_contention_edges"] == 0


def test_e1_2_zero_machine_freedom_is_reproduced_from_frozen_artifact():
    payload = json.loads(Path(
        "outputs/crafter/lns_e1_2_graph_freedom/machine_candidate_pools.json"
    ).read_text())
    assert sum(row["eligible_same_line_alternative_count"] > 0
               for neighborhood in payload.values()
               for row in neighborhood.values()) == 0
