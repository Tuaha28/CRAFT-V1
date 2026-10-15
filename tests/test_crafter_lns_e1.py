from datetime import datetime
import json
from pathlib import Path

from crafter.authoritative_semantics import authoritative_productive_seconds
from crafter.data_adapter import build_smoke_fixture
from crafter.lns_probe.analysis import build_incumbent_index
from crafter.lns_probe.local_model import build_tier1_local_model, solve_local
from crafter.lns_probe.neighborhoods import (
    guided_neighborhood, random_neighborhood, transactional_footprint,
)
from crafter.lns_probe.transaction import (
    _machine_accepts, frozen_incumbent, materialize_machines,
    realize_transaction,
)
from crafter.lns_probe.anchored_model import (
    build_resource_anchored_model, machine_interval_feasible, solve_anchored,
)
from crafter.lns_probe.anchored_transaction import realize_anchored_transaction
from crafter.lns_probe.resource_graph import (
    build_resource_state_graph, causal_waiting, score_freedoms,
)
from crafter.lns_probe.selective_model import (
    build_machine_candidate_pools, build_selective_machine_model,
    solve_selective,
)
from crafter.lns_probe.selective_transaction import realize_selective_transaction
from crafter.phenotype import validate


def _fixture_rows():
    fixture = build_smoke_fixture()
    payload = json.loads(Path(
        "outputs/crafter/crafter_smoke_result.json").read_text())
    rows = []
    for item in payload["phenotype"]["operations"]:
        a = item["assignment"]
        rows.append({
            "operation_key": a["operation_key"],
            "production_order_id": a["order_id"],
            "operation_id": a["operation_id"],
            "line_id": a["line_id"],
            "machine_ids": a["machine_ids"],
            "employee_ids": item["employee_ids"],
            "start_minutes": a["start_seconds"]/60,
            "productive_start_minutes": a["productive_start_seconds"]/60,
            "end_minutes": a["end_seconds"]/60,
            "setup_minutes": a["setup_seconds"]/60,
            "productive_minutes": a["productive_seconds"]/60,
            "worker_segments_by_employee": {
                employee: segments for employee, segments in item["worker_segments"]
            },
            "operator_sourcing_tier": item["sourcing_tier"],
        })
    details = {
        op.operation_key: {
            "setup_mins": op.setup_seconds/60,
            "serial_processing_mins": op.serial_productive_seconds/60,
        }
        for op in fixture.problem.operations
    }
    index = build_incumbent_index(fixture.problem, rows, details)
    baseline = validate(
        fixture.problem, frozen_incumbent(fixture.problem, rows),
        fixture.operator_index)
    return fixture, rows, details, index, baseline


def test_neighborhoods_are_factory_local_and_reproducible():
    fixture, _rows, _details, index, _baseline = _fixture_rows()
    guided = guided_neighborhood(index, 8, seed=42)
    random1 = random_neighborhood(
        index, 8, factory_id=guided.factory_id, seed=99,
        preferred_window=(guided.start_seconds, guided.end_seconds))
    random2 = random_neighborhood(
        index, 8, factory_id=guided.factory_id, seed=99,
        preferred_window=(guided.start_seconds, guided.end_seconds))
    assert random1 == random2
    for neighborhood in (guided, random1):
        assert {fixture.problem.order_by_id[
            fixture.problem.operation_by_key[key].order_id].factory_id
                for key in neighborhood.operation_keys} == {neighborhood.factory_id}


def test_local_model_is_genuinely_local_and_preserves_boundaries():
    fixture, _rows, details, index, _baseline = _fixture_rows()
    neighborhood = guided_neighborhood(index, 6, seed=42)
    artifacts = build_tier1_local_model(
        fixture.problem, details, index, neighborhood)
    assert len(artifacts.starts) == len(neighborhood.operation_keys) == 6
    assert artifacts.size.optional_intervals == 0
    assert artifacts.size.variables < 500
    assert artifacts.size.constraints < 1500
    assert artifacts.model.Validate() == ""
    footprint = transactional_footprint(fixture.problem, index, neighborhood)
    assert footprint["factory_id"] == 1
    assert footprint["closure_status"] == "ACCEPTED"


def test_local_duration_semantics_match_v2_1_authority():
    fixture, _rows, details, index, _baseline = _fixture_rows()
    neighborhood = guided_neighborhood(index, 5, seed=42)
    artifacts = build_tier1_local_model(
        fixture.problem, details, index, neighborhood)
    for key in neighborhood.operation_keys:
        k = len(index.by_key[key]["machine_ids"])
        assert artifacts.bounds[key]["productive"] == (
            authoritative_productive_seconds(details[key], k))


def test_same_order_and_different_order_changeover_boundary():
    reservations = [(0, 1000, "A", "old")]
    assert _machine_accepts(reservations, 1000, 1200, "A")
    assert not _machine_accepts(reservations, 1000, 1200, "B")
    assert _machine_accepts(reservations, 2500, 2700, "B")


def test_exact_machine_materialization_observes_outside_reservations():
    fixture, _rows, _details, index, _baseline = _fixture_rows()
    key = "O1_A"
    row = index.by_key[key]
    assignment = {key: {
        "start_seconds": int(row["start_minutes"]*60),
        "productive_start_seconds": int(row["productive_start_minutes"]*60),
        "end_seconds": int(row["end_minutes"]*60),
    }}
    result, machine_ids = materialize_machines(
        fixture.problem, index, (key,), assignment)
    assert result["success"]
    assert len(machine_ids[key]) == len(row["machine_ids"])


def test_tier1_solve_enters_search_or_returns_incumbent_quickly(tmp_path):
    fixture, _rows, details, index, _baseline = _fixture_rows()
    neighborhood = guided_neighborhood(index, 8, seed=42)
    artifacts = build_tier1_local_model(
        fixture.problem, details, index, neighborhood)
    result, assignment = solve_local(
        artifacts, seed=42, time_limit=3, log_path=tmp_path/"local.log")
    assert result["wall_time_seconds"] < 3.5
    assert result["solution_count"] >= 1
    assert assignment is not None


def test_outside_operations_freeze_and_rollback_is_exact():
    fixture, _rows, details, index, baseline = _fixture_rows()
    keys = tuple(index.order_operations["O1"])
    from crafter.lns_probe.neighborhoods import Neighborhood
    features = [index.operation_features[key] for key in keys]
    neighborhood = Neighborhood(
        "guided", 42, len(keys), 1, keys[0], keys, ("O1",),
        tuple(sorted({row["line_id"] for row in features})),
        tuple(sorted({row["capability"] for row in features})),
        min(row["start_seconds"] for row in features),
        max(row["end_seconds"] for row in features))
    assignment = {
        key: {
            "start_seconds": index.operation_features[key]["start_seconds"],
            "productive_start_seconds": int(
                index.by_key[key]["productive_start_minutes"]*60),
            "end_seconds": index.operation_features[key]["end_seconds"],
        }
        for key in keys
    }
    result, candidate = realize_transaction(
        fixture.problem, fixture.operator_index, details, index,
        neighborhood, assignment, baseline)
    # Equal baseline quality must rollback; no invalid schedule can commit.
    assert result["accepted"] is False
    assert result["rollback_hash"] == baseline.phenotype_hash
    assert result["outside_operations_unchanged"] is True
    assert result["validator_valid"] is True
    assert candidate.phenotype_hash is not None


def test_fixed_commitment_and_pause_resume_regressions_remain_external():
    fixture, _rows, _details, _index, baseline = _fixture_rows()
    assert fixture.problem.resources.fixed_machine_commitments
    assert baseline.validation_report["valid"]
    # The LNS probe delegates exact worker identity/segments to the unchanged
    # worker allocator and never puts individual employees in the CP model.
    assert any(item.worker_segments for item in baseline.operations)


def test_full_phenotype_hash_is_deterministic_at_commit_boundary():
    fixture, rows, _details, _index, baseline = _fixture_rows()
    repeated = validate(
        fixture.problem, frozen_incumbent(fixture.problem, rows),
        fixture.operator_index)
    assert repeated.validation_report["valid"]
    assert repeated.phenotype_hash == baseline.phenotype_hash


def test_anchored_options_keep_exact_machine_and_worker_identities():
    fixture, _rows, details, index, _baseline = _fixture_rows()
    neighborhood = guided_neighborhood(index, 6, seed=42)
    artifacts = build_resource_anchored_model(
        fixture.problem, details, fixture.operator_index, index, neighborhood)
    for key, options in artifacts.options.items():
        machine_ids = tuple(sorted(int(x) for x in index.by_key[key]["machine_ids"]))
        worker_ids = tuple(sorted(str(x) for x in index.by_key[key]["employee_ids"]))
        assert all(option.machine_ids == machine_ids for option in options)
        assert all(option.employee_ids == worker_ids for option in options)


def test_anchored_machine_boundary_changeover_is_authoritative():
    same = [(0, 1000, "A", "outside")]
    assert machine_interval_feasible(same, 1000, 1200, "A")
    assert not machine_interval_feasible(same, 1000, 1200, "B")
    assert machine_interval_feasible(same, 2500, 2700, "B")
    future = [(4000, 5000, "B", "outside")]
    assert not machine_interval_feasible(future, 2000, 3000, "A")
    assert machine_interval_feasible(future, 0, 2000, "A")


def test_anchored_worker_segments_hold_machine_through_pause():
    fixture, _rows, details, index, _baseline = _fixture_rows()
    neighborhood = guided_neighborhood(index, 6, seed=42)
    artifacts = build_resource_anchored_model(
        fixture.problem, details, fixture.operator_index, index, neighborhood)
    for options in artifacts.options.values():
        for option in options:
            assert option.worker_segments
            assert option.start_seconds <= int((
                option.worker_segments[0][0]-fixture.problem.origin).total_seconds())
            assert option.end_seconds == int((
                option.worker_segments[-1][1]-fixture.problem.origin).total_seconds())


def test_anchored_model_preserves_routing_boundaries_and_is_small(tmp_path):
    fixture, _rows, details, index, _baseline = _fixture_rows()
    neighborhood = guided_neighborhood(index, 6, seed=42)
    artifacts = build_resource_anchored_model(
        fixture.problem, details, fixture.operator_index, index, neighborhood)
    assert artifacts.size.operations == 6
    assert artifacts.size.variables < 1000
    assert artifacts.size.constraints < 3000
    result, selected = solve_anchored(
        artifacts, seed=42, seconds_limit=3, log_path=tmp_path/"anchored.log")
    assert selected is not None
    for left, right in index.route_successor.items():
        if left in selected and right in selected:
            assert selected[left].end_seconds <= selected[right].start_seconds


def test_anchored_transaction_requires_validator_and_freezes_outside(tmp_path):
    fixture, _rows, details, index, baseline = _fixture_rows()
    neighborhood = guided_neighborhood(index, 6, seed=42)
    artifacts = build_resource_anchored_model(
        fixture.problem, details, fixture.operator_index, index, neighborhood)
    _result, selected = solve_anchored(
        artifacts, seed=42, seconds_limit=3, log_path=tmp_path/"anchored2.log")
    transaction, candidate = realize_anchored_transaction(
        fixture.problem, fixture.operator_index, neighborhood, selected, baseline)
    assert transaction["outside_operations_unchanged"] is True
    assert transaction["machine_ids_unchanged"] is True
    assert transaction["worker_ids_unchanged"] is True
    if transaction["accepted"]:
        assert transaction["validator_valid"] is True
        assert transaction["improvement_seconds"] > 0
    else:
        assert transaction["rollback_hash"] == baseline.phenotype_hash


def test_resource_state_graph_is_sparse_complete_and_deterministic():
    fixture, rows, details, index, _baseline = _fixture_rows()
    graph1 = build_resource_state_graph(
        fixture.problem, index, details, fixture.operator_index)
    graph2 = build_resource_state_graph(
        fixture.problem, index, details, fixture.operator_index)
    assert graph1["nodes"] == graph2["nodes"]
    assert graph1["edges"] == graph2["edges"]
    assert len(graph1["nodes"]["operations"]) == len(rows) == 15
    assert graph1["statistics"]["potential_contention_edges"] == 0
    assert len(graph1["edges"]["routing_precedence"]) == 10
    assert len(graph1["edges"]["current_machine_assignment"]) == sum(
        len(row["machine_ids"]) for row in rows)
    assert len(graph1["edges"]["current_worker_assignment"]) == sum(
        len(row["employee_ids"]) for row in rows)


def test_causal_signals_and_freedom_scores_are_deterministic():
    fixture, _rows, details, index, _baseline = _fixture_rows()
    neighborhood = guided_neighborhood(index, 8, seed=42)
    pools, pool_details = build_machine_candidate_pools(
        fixture.problem, index, neighborhood)
    causal1 = causal_waiting(fixture.problem, index, details)
    causal2 = causal_waiting(fixture.problem, index, details)
    assert causal1 == causal2
    simple1, graph1 = score_freedoms(
        fixture.problem, index, causal1, neighborhood, pools)
    simple2, graph2 = score_freedoms(
        fixture.problem, index, causal2, neighborhood, pools)
    assert (simple1, graph1) == (simple2, graph2)
    assert set(simple1) == set(neighborhood.operation_keys)
    assert all(set(row["incumbent_machine_ids"]) <=
               set(row["candidate_machine_ids"])
               for row in pool_details.values())


def test_selective_machine_model_keeps_f1_and_selects_exact_k_for_f2(tmp_path):
    fixture, _rows, details, index, baseline = _fixture_rows()
    neighborhood = guided_neighborhood(index, 8, seed=42)
    pools, pool_details = build_machine_candidate_pools(
        fixture.problem, index, neighborhood)
    candidates = [key for key in neighborhood.operation_keys
                  if pool_details[key]["alternative_machine_ids"]]
    freed = tuple(candidates[:min(2, len(candidates))])
    assert freed
    artifacts = build_selective_machine_model(
        fixture.problem, details, fixture.operator_index, index,
        neighborhood, freed, pools)
    assert artifacts.model.Validate() == ""
    assert artifacts.size.f2_operations == len(freed)
    assert artifacts.size.variables < 5000
    result, selected = solve_selective(
        artifacts, seed=42, seconds_limit=3,
        log_path=tmp_path/"selective.log")
    assert selected is not None
    for key in neighborhood.operation_keys:
        incumbent = tuple(sorted(int(value) for value in
                                 index.by_key[key]["machine_ids"]))
        chosen = selected[key]["machine_ids"]
        assert len(chosen) == len(incumbent)
        if key not in freed:
            assert chosen == incumbent
        for machine_id in chosen:
            machine = fixture.problem.machine_by_id[machine_id]
            operation = fixture.problem.operation_by_key[key]
            assert machine.line_id == int(index.by_key[key]["line_id"])
            assert machine.supports(operation.required_capability)
    transaction, candidate = realize_selective_transaction(
        fixture.problem, fixture.operator_index, index, neighborhood,
        selected, baseline)
    assert transaction["machine_verification_success"] is True
    assert transaction["worker_ids_unchanged"] is True
    assert transaction["outside_operations_unchanged"] is True
    assert transaction["validator_valid"] is True
    assert candidate.validation_report["valid"]
    if not transaction["accepted"]:
        assert transaction["rollback_hash"] == baseline.phenotype_hash
