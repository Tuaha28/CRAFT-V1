"""Offline integrity checks for the CRAFT Industry Operating Model V1."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


OUT = Path("outputs/crafter/industry_operating_model_v1")


REQUIRED = {
    "industry_operating_model.json", "operation_lifecycle.json", "wip_contract.json",
    "bundle_lot_streaming_policy.json", "event_taxonomy.json",
    "rescheduling_trigger_policy.json", "freeze_zone_policy.json",
    "matchup_recovery_policy.json", "recovery_hierarchy.json",
    "machine_recovery_policy.json", "worker_recovery_policy.json",
    "predecessor_delay_policy.json", "production_overrun_policy.json",
    "quality_rework_policy.json", "material_shortage_policy.json",
    "utility_outage_policy.json", "urgent_order_policy.json",
    "order_change_policy.json", "combined_event_policy.json",
    "manager_intervention_policy.json", "resource_graph_contract.json",
    "cp_sat_contract.json", "joint_executable_mode_contract.json",
    "schedule_stability_contract.json", "policy_provenance.json",
    "implementation_audit.json", "gap_priority.json",
    "factory_confirmation_register.json", "source_identity.json",
    "final_report.md", "artifact_hash_manifest.json",
}


def test_industry_model_artifact_set_and_manifest():
    if not OUT.exists():
        return
    assert REQUIRED.issubset({path.name for path in OUT.iterdir()})
    manifest = json.loads((OUT / "artifact_hash_manifest.json").read_text())
    assert manifest["decision"] == "CRAFT_INDUSTRY_MODEL_V1_FROZEN"
    assert manifest["database_writes"] == 0
    for relative, expected in manifest["files"].items():
        assert hashlib.sha256((OUT / relative).read_bytes()).hexdigest() == expected


def test_industry_model_preserves_frozen_execution_contract():
    if not OUT.exists():
        return
    identity = json.loads((OUT / "source_identity.json").read_text())
    assert identity["snapshot"] == "parity_fix_20260916_s42"
    assert identity["source_fingerprint"] == "7386844498503f37cdfa08877248c51966699e31b6762f6e0c3effc572cd78ee"
    mode = json.loads((OUT / "joint_executable_mode_contract.json").read_text())
    assert mode["immutable_after_cp"] is True
    assert "machine_ids" in mode["fields"] and "worker_segments" in mode["fields"]
    cp = json.loads((OUT / "cp_sat_contract.json").read_text())
    assert "global factory model" in cp["prohibitions"]


def test_policy_provenance_and_confirmation_are_explicit():
    if not OUT.exists():
        return
    provenance = json.loads((OUT / "policy_provenance.json").read_text())
    assert "INDUSTRY_DERIVED_POLICY" in provenance["types"]
    register = json.loads((OUT / "factory_confirmation_register.json").read_text())
    assert register["questions"]
    assert register["does_not_block"]

