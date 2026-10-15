"""Run the isolated CRAFT E1.3 freedom-opportunity audit."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path

from crafter.e0_5_semantic_reconciliation import _safe
from crafter.phenotype import validate
from crafter.real_data_adapter import SNAPSHOT_NAME, build_real_window_fixture
from .analysis import build_incumbent_index
from .freedom_audit import (
    _global_machine_reservations, audit_lines_and_k,
    audit_machine_identity, audit_timing, audit_workers,
    build_masks_and_recommendations, freedom_distribution,
)
from .resource_graph import build_resource_state_graph, causal_waiting
from .run_e1_1 import CR_PATH, EXPECTED_CR, EXPECTED_SOURCE
from .transaction import frozen_incumbent


OUTPUT_DIR = Path("outputs/crafter/e1_3_freedom_audit")
E12_DIR = Path("outputs/crafter/lns_e1_2_graph_freedom")


def _write_json(directory, name, value):
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_safe(value), indent=2, sort_keys=True, default=str)+"\n"
    path = directory/name
    temporary = path.with_suffix(path.suffix+".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)
    return hashlib.sha256(payload.encode()).hexdigest()


def _write_text(directory, name, value):
    directory.mkdir(parents=True, exist_ok=True)
    if not value.endswith("\n"):
        value += "\n"
    path = directory/name
    temporary = path.with_suffix(path.suffix+".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical_hash(value):
    raw = json.dumps(_safe(value), sort_keys=True, separators=(",", ":"),
                     default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _top(values, dimension, limit=20):
    return {"dimension": dimension, "count_available": len(values),
            "top": list(values[:limit])}


def _previous_neighborhood_audit(masks):
    stored = json.loads((E12_DIR/"selected_neighborhoods.json").read_text(
        encoding="utf-8"))
    by_key = {row["operation_key"]: row for row in masks}
    result = []
    for number, neighborhood in enumerate(stored):
        rows = [by_key[key] for key in neighborhood["operation_keys"]]
        result.append({
            "rank": number, "seed": neighborhood["seed"],
            "operations": len(rows), "factory_id": neighborhood["factory_id"],
            "machine_id_freedom": sum(row["machine_id_free_possible"] for row in rows),
            "line_freedom": sum(row["line_free_possible"] for row in rows),
            "k_freedom": sum(row["k_free_possible"] for row in rows),
            "worker_freedom_screen": sum(row["worker_free_possible"] for row in rows),
            "line_k_freedom": sum(row["combined_line_k_possible"] for row in rows),
            "operation_keys": neighborhood["operation_keys"],
        })
    return result


def _next_experiment(dimension_candidates):
    mapping = {"LINE": "TEST_LINE_FREEDOM", "K": "TEST_K_FREEDOM",
               "LINE_K": "TEST_LINE_K_FREEDOM",
               "WORKER": "TEST_WORKER_FREEDOM",
               "MACHINE_ID": "TEST_MULTI_RESOURCE_FREEDOM"}
    evidence = {}
    for dimension, values in dimension_candidates.items():
        top = values[:20]
        evidence[dimension] = {
            "available_operations": len(values),
            "top20_score_sum": sum(row["score"] for row in top),
            "top20_delivery_pressure_sum": sum(row["delivery_pressure"] for row in top),
        }
    viable = [dimension for dimension in mapping if evidence.get(
        dimension, {}).get("available_operations", 0) > 0]
    if not viable:
        return "REVISE_FREEDOM_MODEL", evidence
    selected = max(viable, key=lambda dimension: (
        evidence[dimension]["top20_score_sum"],
        evidence[dimension]["available_operations"], dimension))
    return mapping[selected], evidence


def run(output_dir=OUTPUT_DIR):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    fixture = build_real_window_fixture(order_limit=50)
    payload = json.loads(CR_PATH.read_text(encoding="utf-8"))
    rows = payload["decoder_details"]["phenotype_operations"]
    baseline = validate(fixture.problem,
        frozen_incumbent(fixture.problem, rows), fixture.operator_index)
    source = {
        "snapshot": fixture.source_metadata["snapshot_name"],
        "expected_source_fingerprint": EXPECTED_SOURCE,
        "actual_source_fingerprint": fixture.source_fingerprint,
        "expected_phenotype_hash": EXPECTED_CR,
        "actual_phenotype_hash": baseline.phenotype_hash,
        "orders": len(fixture.problem.orders),
        "operations": len(fixture.problem.operations),
        "validator_status": baseline.status.value,
        "validator_valid": baseline.validation_report["valid"],
        "database_writes": 0,
    }
    if not (source["snapshot"] == SNAPSHOT_NAME and
            source["actual_source_fingerprint"] == EXPECTED_SOURCE and
            source["actual_phenotype_hash"] == EXPECTED_CR and
            source["operations"] == len(rows) == 431 and
            source["validator_valid"]):
        raise RuntimeError("frozen E1.3 source identity mismatch")
    index = build_incumbent_index(
        fixture.problem, rows, fixture.scheduler.op_details)
    graph = build_resource_state_graph(
        fixture.problem, index, fixture.scheduler.op_details,
        fixture.operator_index)
    causal = causal_waiting(fixture.problem, index, fixture.scheduler.op_details)
    previous_graph = json.loads((E12_DIR/"resource_state_graph.json").read_text(
        encoding="utf-8"))
    current_relations = {"schema_version": graph["schema_version"],
                         "nodes": graph["nodes"], "edges": graph["edges"]}
    previous_relations = {"schema_version": previous_graph["schema_version"],
                          "nodes": previous_graph["nodes"],
                          "edges": previous_graph["edges"]}
    graph_identity = {
        "e1_2_resource_graph_sha256": hashlib.sha256(
            (E12_DIR/"resource_state_graph.json").read_bytes()).hexdigest(),
        "e1_2_canonical_relations_sha256": _canonical_hash(previous_relations),
        "e1_3_canonical_relations_sha256": _canonical_hash(current_relations),
        "schema_equal": graph["schema_version"] == previous_graph["schema_version"],
        "nodes_equal": graph["nodes"] == previous_graph["nodes"],
        "edges_equal": graph["edges"] == previous_graph["edges"],
        "identity_preserved": current_relations == previous_relations,
        "statistics": graph["statistics"],
    }
    if not graph_identity["identity_preserved"]:
        raise RuntimeError("E1.2 graph identity mismatch")

    machine_reservations = _global_machine_reservations(fixture.problem, index)
    machine_rows = audit_machine_identity(
        fixture.problem, index, machine_reservations)
    timing_rows = audit_timing(
        fixture.problem, index, fixture.scheduler.op_details,
        fixture.operator_index)
    line_rows, k_rows, combination_rows = audit_lines_and_k(
        fixture.problem, index, fixture.scheduler.op_details,
        fixture.operator_index, machine_reservations, fixture.scheduler)
    worker_rows = audit_workers(
        fixture.problem, index, fixture.scheduler.op_details,
        fixture.operator_index)
    masks, classes, alignment, recommendations, candidates = (
        build_masks_and_recommendations(
            fixture.problem, index, causal, timing_rows, machine_rows,
            line_rows, k_rows, worker_rows, combination_rows))
    distribution = freedom_distribution(
        fixture.problem, index, masks, classes)
    previous_neighborhoods = _previous_neighborhood_audit(masks)
    global_machine_rate = distribution["overall"]["freedoms"][
        "machine_id_free_possible"]["percentage"]
    neighborhood_machine = sum(row["machine_id_freedom"]
                               for row in previous_neighborhoods)
    neighborhood_total = sum(row["operations"] for row in previous_neighborhoods)
    if global_machine_rate < 5:
        machine_interpretation = "same-line machine-ID freedom is globally rare"
    elif neighborhood_machine == 0:
        machine_interpretation = "the previous neighborhoods lacked alternatives despite broader global availability"
    else:
        machine_interpretation = "previous neighborhoods contained some alternatives"
    previous_payload = {
        "neighborhoods": previous_neighborhoods,
        "unique_operation_count": len(set(key for row in previous_neighborhoods
                                          for key in row["operation_keys"])),
        "neighborhood_occurrences": neighborhood_total,
        "machine_freedom_occurrences": neighborhood_machine,
        "interpretation": machine_interpretation,
    }

    actionable_counts = {
        dimension: len(candidates.get(dimension, ()))
        for dimension in ("MACHINE_ID", "LINE", "K", "WORKER", "LINE_K")}
    frequent = [dimension for dimension, count in actionable_counts.items()
                if count >= 20]
    gnn_status = ("GNN_DATA_COLLECTION_READY" if len(frequent) >= 2 else
                  "GNN_NOT_READY")
    gnn = {
        "status": gnn_status,
        "frequent_action_dimensions_threshold": 20,
        "frequent_dimensions": frequent,
        "actionable_counts": actionable_counts,
        "action_space_non_empty": any(actionable_counts.values()),
        "repair_outcomes_available": False,
        "future_label_contract": (
            "Per-operation freedom selected, exact realization result, validator "
            "result, tardiness delta and churn from future controlled repairs."),
        "training_authorized": False,
        "disclosure": "Ready only to collect repair outcomes; not ready to train.",
    }
    next_experiment, next_evidence = _next_experiment(candidates)

    delayed_with_graph_alignment = sum(
        row["realized_delay"]["machine_wait_seconds"] > 0 and
        not row["freedom_mask"]["machine_id_free_possible"] and
        (row["freedom_mask"]["line_free_possible"] or
         row["freedom_mask"]["k_free_possible"])
        for row in alignment)
    graph_value = {
        "decision_space_mapper_result": (
            "YES" if delayed_with_graph_alignment > 0 else "INCONCLUSIVE"),
        "machine_delayed_without_machine_id_but_with_line_or_k":
            delayed_with_graph_alignment,
        "graph_only_information": [
            "realized physical-machine predecessor/successor blockers",
            "realized downstream routing/tardiness propagation context",
            "current exact worker assignments and calendar-pause attribution",
        ],
        "table_only_information": [
            "nominal eligible lines", "usable capability counts",
            "qualified worker pools", "legal machine-count bounds"],
        "statistics": graph["statistics"],
    }

    _write_json(output_dir, "source_identity.json", source)
    _write_json(output_dir, "graph_identity.json", graph_identity)
    _write_json(output_dir, "machine_id_freedom.json", machine_rows)
    _write_json(output_dir, "line_freedom.json", line_rows)
    _write_json(output_dir, "k_freedom.json", k_rows)
    _write_json(output_dir, "worker_freedom_screen.json", worker_rows)
    _write_json(output_dir, "line_k_opportunities.json", combination_rows)
    _write_json(output_dir, "freedom_mask.json", masks)
    _write_json(output_dir, "freedom_classes.json", classes)
    _write_json(output_dir, "delay_to_freedom_alignment.json", alignment)
    _write_json(output_dir, "freedom_distribution.json", distribution)
    _write_json(output_dir, "recommended_freedom_dimension_by_operation.json",
                recommendations)
    _write_json(output_dir, "top_line_candidates.json",
                _top(candidates.get("LINE", ()), "LINE"))
    _write_json(output_dir, "top_k_candidates.json",
                _top(candidates.get("K", ()), "K"))
    _write_json(output_dir, "top_line_k_candidates.json",
                _top(candidates.get("LINE_K", ()), "LINE_K"))
    _write_json(output_dir, "top_worker_candidates.json",
                _top(candidates.get("WORKER", ()), "WORKER"))
    _write_json(output_dir, "top_machine_id_candidates.json",
                _top(candidates.get("MACHINE_ID", ()), "MACHINE_ID"))
    _write_json(output_dir, "previous_neighborhood_audit.json", previous_payload)
    _write_json(output_dir, "gnn_readiness.json", gnn)
    _write_json(output_dir, "resource_graph_value.json", graph_value)
    _write_json(output_dir, "next_experiment_evidence.json", {
        "selected": next_experiment, "dimension_evidence": next_evidence,
        "selection_rule": "largest top-20 deterministic opportunity score sum",
    })

    overall = distribution["overall"]["freedoms"]
    report = (
        "# CRAFT E1.3 Freedom Opportunity Audit\n\n"
        f"- Source fingerprint: `{fixture.source_fingerprint}`\n"
        f"- CR phenotype: `{baseline.phenotype_hash}`\n"
        f"- Operations audited: {len(masks)}\n"
        f"- Graph identity preserved: {graph_identity['identity_preserved']}\n"
        f"- Same-line machine-ID freedom: {overall['machine_id_free_possible']['count']}/431 "
        f"({overall['machine_id_free_possible']['percentage']:.2f}%)\n"
        f"- Line freedom: {overall['line_free_possible']['count']}/431 "
        f"({overall['line_free_possible']['percentage']:.2f}%)\n"
        f"- k freedom: {overall['k_free_possible']['count']}/431 "
        f"({overall['k_free_possible']['percentage']:.2f}%)\n"
        f"- Worker freedom screen: {overall['worker_free_possible']['count']}/431 "
        f"({overall['worker_free_possible']['percentage']:.2f}%)\n"
        f"- Combined line+k: {overall['combined_line_k_possible']['count']}/431 "
        f"({overall['combined_line_k_possible']['percentage']:.2f}%)\n"
        f"- Previous-neighborhood machine freedom: {neighborhood_machine}/{neighborhood_total} occurrences\n"
        f"- Graph as decision-space mapper: `{graph_value['decision_space_mapper_result']}`\n"
        f"- GNN readiness: `{gnn_status}`\n"
        f"- Next experiment: `{next_experiment}`\n"
        "\nNo CP-SAT repair, GNN training, schedule mutation, or database write was performed.\n")
    _write_text(output_dir, "final_report.md", report)
    files = {}
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "artifact_hash_manifest.json":
            files[str(path.relative_to(output_dir)).replace("\\", "/")] = (
                hashlib.sha256(path.read_bytes()).hexdigest())
    audit_fingerprint = _canonical_hash({
        "source": source, "graph": graph_identity,
        "masks": masks, "distribution": distribution,
        "recommendations": recommendations,
    })
    _write_json(output_dir, "artifact_hash_manifest.json", {
        "source_fingerprint": fixture.source_fingerprint,
        "phenotype_hash": baseline.phenotype_hash,
        "audit_fingerprint": audit_fingerprint,
        "gnn_status": gnn_status, "next_experiment": next_experiment,
        "files": files,
    })
    return {"operations": len(masks), "audit_fingerprint": audit_fingerprint,
            "gnn_status": gnn_status, "next_experiment": next_experiment,
            "output_dir": str(output_dir)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.output_dir), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
