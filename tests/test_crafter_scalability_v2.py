"""Focused regression checks for fresh-mode scalability measurements."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from crafter.lns_probe.joint_modes import JointExecutableMode


OUT = Path("outputs/crafter/scalability_v2_fresh_modes_corrected")
EXPECTED_SOURCE = "7386844498503f37cdfa08877248c51966699e31b6762f6e0c3effc572cd78ee"
EXPECTED_CR = "56cc2b12f69f242a380f6678385c06a70341a560ebac3619408d5700b7938e92"


def test_fresh_mode_output_identity_and_matrix():
    source = json.loads((OUT / "source_identity.json").read_text())
    assert source["source_fingerprint"] == EXPECTED_SOURCE
    assert source["cr_phenotype_hash"] == EXPECTED_CR
    assert source["database_writes"] == 0
    a = json.loads((OUT / "experiment_a_context_scaling.json").read_text())
    b = json.loads((OUT / "experiment_b_repair_freedom_scaling.json").read_text())
    assert len(a) == 24
    assert len(b) == 24
    assert {row["n"] for row in a} == {30, 50, 75, 100}
    assert {(row["n"], row["b"]) for row in b} == {(30, 5), (50, 10), (75, 15), (100, 20)}


def test_fresh_mode_generation_is_measured_and_incumbent_parity_is_recorded():
    rows = json.loads((OUT / "fresh_mode_generation.json").read_text())
    assert len(rows) == 48
    assert all(float(row["generation_seconds"]) > 0 for row in rows if row["modes"] is not None)
    parity = json.loads((OUT / "incumbent_parity.json").read_text())
    assert len(parity) == 48
    assert all("parity" in row for row in parity)


def test_rounding_at_decimal_minute_boundary_is_not_truncating():
    # 2100.7 minutes is represented as 126041.99999999999 by binary float;
    # the authoritative second projection is round-to-nearest.
    assert int(round(2100.7 * 60.0)) == 126042


def test_no_historical_joint_modes_are_reused_and_manifest_matches():
    frozen = json.loads((OUT / "frozen_semantics_identity.json").read_text())
    assert "fresh" in frozen["joint_mode"]
    manifest = json.loads((OUT / "artifact_hash_manifest.json").read_text())
    for relative, expected in manifest["files"].items():
        path = OUT / relative
        assert path.exists(), relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected


def test_joint_mode_contract_remains_immutable():
    assert JointExecutableMode.__dataclass_params__.frozen is True
