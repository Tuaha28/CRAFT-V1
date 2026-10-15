"""Focused checks for the frozen-semantics scalability harness."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


OUT = Path("outputs/crafter/scalability_v1_final6")
EXPECTED_SOURCE = "7386844498503f37cdfa08877248c51966699e31b6762f6e0c3effc572cd78ee"
EXPECTED_CR = "56cc2b12f69f242a380f6678385c06a70341a560ebac3619408d5700b7938e92"


def test_scalability_source_and_frozen_semantics():
    source = json.loads((OUT / "source_identity.json").read_text())
    frozen = json.loads((OUT / "frozen_semantics_identity.json").read_text())
    assert source["source_fingerprint"] == EXPECTED_SOURCE
    assert source["cr_phenotype_hash"] == EXPECTED_CR
    assert source["operations"] == 431
    assert source["database_writes"] == 0
    assert "ceil" in frozen["duration"]
    assert "immutable" in frozen.get("joint_mode_contract", frozen.get("joint_mode", ""))


def test_global_graph_preserves_full_state():
    graph = json.loads((OUT / "global_graph_summary.json").read_text())
    assert graph["nodes_by_type"]["operations"] == 431
    assert graph["nodes_by_type"]["orders"] == 50
    assert graph["edges_by_type"]["order_contains_operation"] == 431
    assert graph["potential_contention_edges"] == 0


def test_ladder_and_release_budget_are_deterministic():
    boundary = json.loads((OUT / "scalability_boundary.json").read_text())
    assert boundary["max_tested_neighborhood_size"] == 100
    assert {int(x) for x in boundary["validation_rate_by_size"]} == {30, 50, 75, 100}
    rows = json.loads((OUT / "neighborhood_definitions.json").read_text())
    assert len(rows) == 24
    assert all(len(row["released"]) <= 5 for row in rows)
    assert all(len(row["operation_keys"]) == row["size"] for row in rows)


def test_all_artifact_manifest_entries_match():
    manifest = json.loads((OUT / "artifact_hash_manifest.json").read_text())
    for relative, expected in manifest["files"].items():
        path = OUT / relative
        assert path.exists(), relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, relative


def test_stress_probes_do_not_mutate_state():
    stress = json.loads((OUT / "stress_scenarios.json").read_text())
    urgent = json.loads((OUT / "urgent_order_scenario.json").read_text())
    assert stress["database_writes"] == 0
    assert urgent["database_writes"] == 0
    assert urgent["hard_constraints_preserved"] is True
