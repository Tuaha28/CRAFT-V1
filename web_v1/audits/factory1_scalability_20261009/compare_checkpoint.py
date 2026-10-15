"""Compare the older CP performance checkpoint with the admitted source.

The checkpoint remains a synthetic/performance artifact. This script never
solves it or treats its selected operations as safe live execution.
"""
from __future__ import annotations

import hashlib
import json
import pickle
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent
SOURCE = ROOT / "web_v1/audits/execution_boundary_v1/factory1_source.json"
NORMALIZED = ROOT / "web_v1/audits/execution_boundary_v1/factory1_normalized_snapshot.json"
CHECKPOINT = ROOT / "outputs/crafter/factory1_cp_parallelism_audit_v1/checkpoint/checkpoint_000_stage_1.prepared.pkl"
PROTECTED = ROOT / "web_v1/audits/industrial_readiness_v1/protected_before.json"


def checked_bytes(path: Path, expected: str) -> bytes:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected:
        raise RuntimeError(f"protected input changed: {path}")
    return content


def main() -> int:
    source_manifest = json.loads(SOURCE.with_name("artifact_hash_manifest.json").read_text())
    source = json.loads(checked_bytes(SOURCE, source_manifest[SOURCE.name]))
    normalized = json.loads(checked_bytes(NORMALIZED, source_manifest[NORMALIZED.name]))
    protected = json.loads(PROTECTED.read_text())

    # Trusted repository-owned pickle only. Prohibit accidental DB access
    # during import/deserialization of historical classes.
    import pymysql
    pymysql.connect = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("checkpoint comparison forbids database access"))
    payload = checked_bytes(CHECKPOINT, protected[CHECKPOINT.relative_to(ROOT).as_posix()])
    prepared = pickle.loads(payload)

    source_keys = {f"{row['production_order_id']}_{row['operation_id']}" for row in source["operations"]}
    checkpoint_keys = {op.operation_key for op in prepared["state"]["problem"].operations}
    states = {row["operation_key"]: row["execution_state"] for row in normalized["operations"]}
    selected = set(prepared["required_window_keys"])
    report = {
        "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "checkpoint_sha256": hashlib.sha256(payload).hexdigest(),
        "source_operation_count": len(source_keys),
        "checkpoint_operation_count": len(checkpoint_keys),
        "source_only_operation_keys": sorted(source_keys - checkpoint_keys),
        "checkpoint_only_operation_keys": sorted(checkpoint_keys - source_keys),
        "selected_operation_count": len(selected),
        "selected_partial_operation_keys": sorted(key for key in selected if states.get(key) == "PARTIALLY_EXECUTED"),
        "selected_completed_operation_keys": sorted(key for key in selected if states.get(key) == "FULLY_COMPLETED"),
        "safe_live_scheduling_input": False,
        "reason": "source membership differs and selected operations include unresolved partial execution",
        "database_reads": 0,
        "database_writes": 0,
        "solver_invoked": False,
    }
    target = HERE / (Path(sys.argv[1]).name if len(sys.argv) > 1 else "checkpoint_comparison.json")
    if target.exists():
        raise RuntimeError("refusing to overwrite checkpoint comparison evidence")
    target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "source_operation_count", "checkpoint_operation_count",
        "source_only_operation_keys", "selected_partial_operation_keys",
    )}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
