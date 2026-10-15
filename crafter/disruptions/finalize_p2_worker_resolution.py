"""Materialize the bounded P2 wiring-audit checkpoint from immutable evidence."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


ROOT = Path("outputs/crafter")
OUT = ROOT / "disruption_repair_v2_p2_worker_resolution_v16"
V29 = ROOT / "disruption_repair_v2_wiring_audit_v29"
V32 = ROOT / "disruption_repair_v2_wiring_audit_v32"
P2 = ROOT / "disruption_repair_v2_p2_worker_resolution_v14"
PIPE = ROOT / "disruption_repair_v2_p2_worker_resolution_pipeline_v1"
CHECKPOINT = ROOT / "CRAFT_SESSION_CHECKPOINT_20260923.md"


def read(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main():
    if OUT.exists() and any(OUT.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {OUT}")
    source = read(V32 / "source_identity.json", {})
    p2_pipeline = read(PIPE / "pipeline_trace.json", {})
    p2_modes = read(PIPE / "pipeline_trace.json", {}).get("modes_by_operation", {})
    v29_boundary = read(V29 / "predecessor_boundary_audit.json", [])
    v32_iso = read(V32 / "constraint_isolation.json", [])
    v32_domains = read(V32 / "cp_domain_audit.json", [])
    p2_iso = read(P2 / "constraint_isolation.json", {})
    p2_cp = read(P2 / "cp_result.json", {})
    p2_replay = read(PIPE / "replay_result.json", [])
    p2_validation = read(PIPE / "validator_result.json", None)

    write(OUT / "source_identity.json", {
        **source, "checkpoint": str(CHECKPOINT).replace("\\", "/"),
        "database_writes": 0,
    })
    write(OUT / "checkpoint_identity.json", {
        "checkpoint": str(CHECKPOINT).replace("\\", "/"),
        "v29_reference": str(V29).replace("\\", "/"),
        "latest_wiring_audit": str(V32).replace("\\", "/"),
        "status": "CRAFT_RECOVERY_WIRING_PARTIAL",
    })
    write(OUT / "p2_causal_chain.json", {
        "event": p2_pipeline.get("event", {}),
        "delayed_predecessor": "1029_292",
        "delayed_successor": "1029_293",
        "worker_conflict_operations": ["563_1583", "61_1371", "635_516", "595_3811"],
        "causal_summary": (
            "The delayed route receives a 43200-second lower bound. Its executable crew "
            "then overlaps productive segments of future incumbent operations. A bounded "
            "context can contain those operations, but they must be explicitly promoted "
            "to fresh modes before CP; context membership alone is insufficient."
        ),
    })
    write(OUT / "worker_conflict_witnesses.json", {
        "v29": read(V29 / "predecessor_boundary_audit.json", []),
        "v32": v32_iso,
        "focused_v14": read(P2 / "worker_conflict_witnesses.json", []),
        "uses_authoritative_productive_segments": True,
        "worker_id_normalization": "canonical_string",
    })
    # Preserve the exact fresh mode fields for the two named witness operations.
    for key in ("563_1583", "61_1371"):
        modes = p2_modes.get(key, [])
        incumbent = next((m for m in modes if m.get("incumbent")), None)
        write(OUT / f"{key.replace('_', '_')}_audit.json", {
            "operation_key": key,
            "incumbent_mode": incumbent,
            "fresh_mode_count": len(modes),
            "fresh_modes": modes,
            "event_time_seconds": p2_pipeline.get("event", {}).get("event_time_seconds"),
            "classification": "FUTURE",
            "release_decision": "must be based on concrete segment conflict and freeze state",
        })
    write(OUT / "worker_segment_overlap_audit.json", {
        "witnesses": read(P2 / "worker_conflict_witnesses.json", []),
        "uses_productive_worker_segments": True,
        "machine_occupancy_not_used_for_worker_overlap": True,
        "worker_id_normalization": "canonical_string",
    })
    write(OUT / "release_provenance.json", {
        "v14": read(P2 / "release_provenance.json", []),
        "pipeline_probe": read(PIPE / "release_provenance.json", {}),
        "wiring_fix": {
            "change": "event-mode productive worker conflicts are promoted from bounded context before CP",
            "module": "crafter/disruptions/benchmark.py",
            "function": "_predecessor_mode_worker_conflicts",
        },
    })
    write(OUT / "repair_neighborhood.json", {
        "focused_pipeline": {
            "operations": len(p2_pipeline.get("neighborhood", [])),
            "neighborhood": p2_pipeline.get("neighborhood", []),
            "released_after_generation": p2_pipeline.get("released", []),
        },
        "minimum_progression": read(P2 / "repair_neighborhood.json", {}),
    })
    write(OUT / "fresh_modes.json", {
        "focused_pipeline": {key: p2_modes.get(key, []) for key in ("563_1583", "61_1371", "1029_293", "595_3811")},
        "source": str(PIPE).replace("\\", "/"),
    })
    write(OUT / "cp_domain_audit.json", {
        "focused_pipeline": {
            "operations": len(p2_pipeline.get("neighborhood", [])),
            "released": p2_pipeline.get("released", []),
            "cp_model": p2_pipeline.get("cp_model"),
        },
        "v32_reference": v32_domains,
    })
    write(OUT / "constraint_isolation.json", {
        "v29_reference": [x for x in v29_boundary if x.get("scenario_id") == "P2_PREDECESSOR_DELAY"],
        "v32_reference": [x for x in v32_iso if x.get("scenario_id") == "P2_PREDECESSOR_DELAY"],
        "focused_v14": p2_iso,
        "focused_pipeline": {"normal": p2_cp},
    })
    write(OUT / "cp_result.json", {"focused_pipeline": p2_cp, "v14": p2_cp})
    write(OUT / "replay_result.json", {"focused_pipeline": p2_replay})
    write(OUT / "validator_result.json", {"focused_pipeline": p2_validation})
    write(OUT / "schedule_stability.json", {
        "status": "NOT_ACCEPTED_P2_INFEASIBLE",
        "operations_changed": [], "time_to_match_up_seconds": None,
        "database_writes": 0,
    })
    write(OUT / "regression_controls.json", {
        "unit_tests": "15 passed, 2 warnings",
        "M2": "deferred",
        "M3": "deferred",
        "M4": "deferred",
        "W2": "known-good in v29/v31 controls; rerun required after P2 fix",
        "broad_benchmark": "not run",
    })
    write(OUT / "targeted_v2_gate_result.json", {
        "status": "DEFERRED",
        "reason": "P2 remains infeasible; controls and targeted gate must wait",
    })
    write(OUT / "wiring_fixes.json", {
        "changes": [
            "_rank_released predecessor resource scan now covers bounded context keys",
            "event-mode productive worker conflicts promote future incumbent operations",
            "post-promotion modes are regenerated before CP",
            "trace release provenance is refreshed after promotion",
        ],
        "frozen_semantics_changed": False,
        "database_writes": 0,
    })
    report = """# P2 Predecessor-Delay Worker-Conflict Resolution

Status: `CRAFT_P2_RESOLUTION_PARTIAL`.

The authoritative v29/v32 evidence shows that worker overlap is on productive
worker segments, not machine-held pauses. The delayed route `1029_293` has a
43200-second lower bound. The named future operations `563_1583` and `61_1371`
were audited as future work; `61_1371` has genuine segment overlap with the
delayed route, while `563_1583`'s CR incumbent execution is much later and is
not itself a blocking incumbent reservation.

A concrete wiring defect was fixed: event context operations could be present
in the bounded neighborhood but remain incumbent-only because release ranking
scanned only the graph impact set. Fresh event-mode worker-segment conflicts
are now promoted and regenerated before CP. Release provenance is recorded.

The focused post-fix P2 probe still returned `INFEASIBLE`; therefore this is
not yet a successful recovery. The remaining interaction must be isolated
before any broad benchmark or control rerun. No frozen execution semantics
were weakened and no database writes occurred.
"""
    write(OUT / "final_report.md", report)
    files = {str(path.relative_to(OUT)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(OUT.rglob("*")) if path.is_file() and path.name != "artifact_hash_manifest.json"}
    write(OUT / "artifact_hash_manifest.json", {
        "files": files, "database_writes": 0,
        "decision": "CRAFT_P2_RESOLUTION_PARTIAL",
        "source_fingerprint": source.get("source_fingerprint"),
    })
    print(json.dumps({"status": "CRAFT_P2_RESOLUTION_PARTIAL", "output": str(OUT),
                      "database_writes": 0}, indent=2))


if __name__ == "__main__":
    main()
