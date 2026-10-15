"""Short, single-scenario P2 pipeline probe.

This is intentionally not the broad disruption benchmark.  It exercises the
normal event -> release -> fresh modes -> CP -> replay/validator path for the
P2 predecessor-delay case only, with diagnostics disabled to keep the probe
bounded.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .v2_recovery_wiring_audit import _events, _load_state, _run_case


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run(output_dir: Path):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    problem, operator_index, rows, durations, op_details, index, baseline, checked, source = _load_state()
    event = next(event for event in _events(problem, rows, index)
                 if event.event_id == "P2_PREDECESSOR_DELAY")
    trace = _run_case(problem, operator_index, rows, op_details, index, baseline, checked,
                      event, n=100, b=20, diagnose=True)
    _write(output_dir / "source_identity.json", {
        "source_fingerprint": source, "snapshot": "parity_fix_20260916_s42",
        "scenario_id": event.event_id, "database_writes": 0,
    })
    _write(output_dir / "pipeline_trace.json", trace)
    _write(output_dir / "release_provenance.json", {
        "event_release_conflicts": trace.get("event_release_conflicts", []),
        "released": trace.get("released", []),
        "database_writes": 0,
    })
    _write(output_dir / "cp_result.json", trace.get("solve", {"status": trace.get("mode_generation_status")}))
    _write(output_dir / "replay_result.json", trace.get("replay", []))
    _write(output_dir / "validator_result.json", trace.get("validation"))
    _write(output_dir / "wiring_fixes.json", {
        "fixes": ["promote event-mode productive worker conflicts from bounded context before CP"],
        "database_writes": 0,
    })
    files = {str(path.relative_to(output_dir)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(output_dir.rglob("*")) if path.is_file()}
    _write(output_dir / "artifact_hash_manifest.json", {
        "files": files, "database_writes": 0, "status": trace.get("solve", {}).get("status", trace.get("mode_generation_status")),
    })
    return {"status": trace.get("solve", {}).get("status", trace.get("mode_generation_status")),
            "released": trace.get("released", []), "event_release_conflicts": trace.get("event_release_conflicts", []),
            "validation": trace.get("validation"), "database_writes": 0}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir), indent=2, default=str))
