"""Capture the existing Factory-2 benchmark input from its named DB snapshot.

This is an offline experiment input, not a live scheduling adapter.  It reads
the historical snapshot once and writes a hash-pinned pickle for later runs.
"""
from __future__ import annotations

import hashlib
import json
import pickle
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "outputs/crafter/factory2_172_snapshot_capture_v1"
SNAPSHOT_NAME = "parity_fix_20260916_s42"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    if OUT.exists() and any(OUT.iterdir()):
        raise RuntimeError(f"refusing to overwrite existing capture: {OUT}")

    # This must precede imports of database.db, utils.config, or CRAFT code.
    from database.ga_snapshot import activate_snapshot, get_snapshot_db_name, list_snapshots

    snapshots = {row["snapshot_name"]: row for row in list_snapshots()}
    snapshot = snapshots.get(SNAPSHOT_NAME)
    if snapshot is None or snapshot["snapshot_db"] != get_snapshot_db_name(SNAPSHOT_NAME):
        raise RuntimeError("named historical snapshot is unavailable")
    activate_snapshot(SNAPSHOT_NAME)

    from crafter.disruptions.benchmark import _load_state
    from crafter.lns_probe.run_e1_1 import CR_PATH, EXPECTED_CR, EXPECTED_SOURCE

    loaded = _load_state()
    problem, _, _, _, _, _, _, checked, _ = loaded
    factory_keys = sorted(
        op.operation_key for op in problem.operations
        if problem.order_by_id[op.order_id].factory_id == 2
    )
    historical = json.loads(
        (ROOT / "outputs/crafter/factory2_172_v1/initial_operation_accounting.json").read_text()
    )
    expected_keys = sorted(historical["operations_requiring_scheduling"])
    if factory_keys != expected_keys or len(factory_keys) != 172:
        raise RuntimeError("Factory-2 operation identity differs from historical accounting")
    if checked.phenotype_hash != EXPECTED_CR or checked.status.value != "VALIDATED_FEASIBLE":
        raise RuntimeError("frozen baseline phenotype no longer validates")

    payload = pickle.dumps(loaded, protocol=pickle.HIGHEST_PROTOCOL)
    roundtrip = pickle.loads(payload)
    if [op.operation_key for op in roundtrip[0].operations] != [
        op.operation_key for op in problem.operations
    ]:
        raise RuntimeError("capture round trip changed operation identities")

    OUT.mkdir(parents=True, exist_ok=False)
    input_path = OUT / "input.pkl"
    input_path.write_bytes(payload)
    manifest = {
        "capture_version": 1,
        "classification": "NEW_CONTROLLED_SNAPSHOT_CAPTURE_NOT_HISTORICAL_EQUIVALENCE",
        "snapshot_name": SNAPSHOT_NAME,
        "snapshot_database": snapshot["snapshot_db"],
        "snapshot_source_database": snapshot["source_db"],
        "snapshot_origin": snapshot["origin_timestamp"].isoformat(),
        "factory_id": 2,
        "all_operations": len(problem.operations),
        "factory_operations": len(factory_keys),
        "factory_operation_keys_sha256": hashlib.sha256(
            json.dumps(factory_keys, separators=(",", ":")).encode()
        ).hexdigest(),
        "baseline_phenotype_sha256": checked.phenotype_hash,
        "historical_expected_source_sha256": EXPECTED_SOURCE,
        "historical_source_equivalence_claimed": False,
        "input_sha256": sha256(input_path),
        "input_bytes": input_path.stat().st_size,
        "cr_artifact_sha256": sha256(ROOT / CR_PATH),
        "duration_artifact_sha256": sha256(
            ROOT / "outputs/crafter/e0_5_semantic_reconciliation/authoritative_duration_semantics.json"
        ),
        "database_writes": 0,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: manifest[key] for key in (
        "classification", "all_operations", "factory_operations", "input_sha256", "input_bytes"
    )}))


if __name__ == "__main__":
    main()
