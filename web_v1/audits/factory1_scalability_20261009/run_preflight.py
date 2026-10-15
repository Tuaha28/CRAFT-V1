"""Offline, read-only Factory-1 source identity and execution-admission check.

This deliberately does not call the CRAFT solver. A blocked execution boundary
must not be turned into a supposedly schedulable workload by dropping rows.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import psutil


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
HERE = Path(__file__).resolve().parent
SOURCE = ROOT / "web_v1/audits/execution_boundary_v1/factory1_source.json"
SOURCE_MANIFEST = SOURCE.with_name("artifact_hash_manifest.json")
PRIOR_PROFILE = ROOT / "web_v1/audits/industrial_readiness_v1/factory1_normalization_after.json"


def main() -> int:
    process = psutil.Process()
    peak = [process.memory_info().rss]
    stop = threading.Event()

    def sample_memory() -> None:
        while not stop.wait(0.05):
            peak[0] = max(peak[0], process.memory_info().rss)

    watcher = threading.Thread(target=sample_memory, daemon=True)
    watcher.start()
    timings = {}
    started = time.perf_counter()
    cpu_started = time.process_time()
    try:
        mark = time.perf_counter()
        expected = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))[SOURCE.name]
        raw = SOURCE.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise RuntimeError("authoritative Factory-1 capture SHA-256 mismatch")
        timings["source_read_and_verify_seconds"] = time.perf_counter() - mark
        mark = time.perf_counter()
        source = json.loads(raw)
        timings["source_deserialize_seconds"] = time.perf_counter() - mark

        from web_v1.backend.integrations.craft.snapshot_adapter import normalize_snapshot
        from web_v1.backend.integrations.craft.boundary_validator import validate_snapshot

        mark = time.perf_counter()
        normalized = normalize_snapshot(source)
        timings["normalization_seconds"] = time.perf_counter() - mark
        mark = time.perf_counter()
        validation = validate_snapshot(normalized, source)
        timings["independent_boundary_validation_seconds"] = time.perf_counter() - mark
    finally:
        stop.set()
        watcher.join(timeout=1)
        peak[0] = max(peak[0], process.memory_info().rss)

    prior = json.loads(PRIOR_PROFILE.read_text(encoding="utf-8"))
    states = dict(sorted(Counter(row["execution_state"] for row in normalized["operations"]).items()))
    source_keys = {f"{row['production_order_id']}_{row['operation_id']}" for row in source["operations"]}
    if (normalized["snapshot_hash"] != prior["snapshot_hash"] or len(source_keys) != 2879 or
            states != {"FULLY_COMPLETED": 171, "PARTIALLY_EXECUTED": 82, "UNSTARTED": 2626} or
            validation["error_counts"] != {"BOUNDARY_UNRESOLVED": 82}):
        raise RuntimeError("Factory-1 source, normalization, or admission baseline changed")

    timings["total_wall_seconds"] = time.perf_counter() - started
    report = {
        "source": SOURCE.relative_to(ROOT).as_posix(),
        "source_sha256": actual,
        "dataset": source["dataset"],
        "reference_time": source["reference_time"],
        "factory_id": source["factory_id"],
        "source_counts": {key: len(source[key]) for key in ("orders", "operations", "lots", "wip")},
        "resource_counts": {key: len(value) for key, value in sorted(source["resources"].items())},
        "operation_states": states,
        "snapshot_hash": normalized["snapshot_hash"],
        "snapshot_hash_matches_prior_profile": True,
        "boundary_validation": {
            "status": validation["status"],
            "valid_for_craft": validation["valid_for_craft"],
            "error_counts": validation["error_counts"],
            "unresolved_operation_keys": validation["unresolved_operation_keys"],
        },
        "timings": timings,
        "cpu_seconds": time.process_time() - cpu_started,
        "peak_rss_mb": peak[0] / 1024**2,
        "database_reads": 0,
        "database_writes": 0,
        "scheduler_invoked": False,
        "live_admission": False,
    }
    HERE.mkdir(parents=True, exist_ok=True)
    target = HERE / (Path(sys.argv[1]).name if len(sys.argv) > 1 else "preflight_v2.json")
    if target.exists():
        raise RuntimeError("refusing to overwrite Factory-1 preflight evidence")
    target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "operations": len(source_keys), "states": states,
        "boundary_status": validation["status"], "timings": timings,
        "peak_rss_mb": report["peak_rss_mb"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
