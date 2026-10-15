"""Generate compact report and hashes for a completed/blocked Factory-2 V1 run."""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter
from pathlib import Path


OUT = Path("outputs/crafter/factory2_full_v1_clean")


def load(name: str):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def main() -> None:
    progress = load("rolling_progress.json")
    windows = progress.get("windows", [])
    failure = progress.get("failure", {})
    runtimes = [float(w.get("window_total_seconds", w.get("total_seconds", 0.0))) for w in windows]
    accepted = [w for w in windows if w.get("accepted")]
    committed = sum(int(w.get("committed_count", len(w.get("committed_keys", [])))) for w in accepted)
    remaining = list(failure.get("remaining", progress.get("remaining", [])))
    cp_counts = Counter(str(w.get("cp_status")) for w in windows)
    report = {
        "status": "CRAFT_PRODUCTION_V1_FACTORY2_PARTIAL" if failure else "CRAFT_PRODUCTION_V1_FACTORY2_COMPLETE",
        "calendar_engine": "V1",
        "initial_schedulable_operations": 172,
        "windows_attempted": len(windows) + (1 if failure else 0),
        "windows_accepted": len(accepted),
        "total_committed": committed,
        "total_accounted": committed,
        "remaining": len(remaining),
        "remaining_keys": remaining,
        "first_failed_window": failure.get("window_id"),
        "first_blocker": failure.get("first_blocker"),
        "cp_status_counts": dict(cp_counts),
        "replay_failures": sum(w.get("replay_status") != "PASS" for w in accepted),
        "physical_validation_failures": sum(w.get("physical_status") != "VALID" for w in accepted),
        "scope_failures": sum(w.get("scope_status") != "SCOPE_VALID" for w in accepted),
        "database_writes": 0,
        "runtime": {
            "total_seconds": sum(runtimes),
            "average_seconds": statistics.mean(runtimes) if runtimes else 0.0,
            "median_seconds": statistics.median(runtimes) if runtimes else 0.0,
            "p95_seconds": sorted(runtimes)[max(0, math.ceil(0.95 * len(runtimes)) - 1)] if runtimes else 0.0,
            "maximum_seconds": max(runtimes) if runtimes else 0.0,
            "minimum_seconds": min(runtimes) if runtimes else 0.0,
        },
        "slowest_windows": [
            {
                "window_id": w.get("window_id"),
                "runtime_seconds": w.get("window_total_seconds", w.get("total_seconds", 0.0)),
                "calendar_regime_seconds": w.get("timing", {}).get("calendar_regime_generation_seconds", 0.0),
                "cp_solve_seconds": (w.get("timing", {}).get("canonical_seconds", 0.0)),
            }
            for w in sorted(windows, key=lambda x: x.get("window_total_seconds", 0.0), reverse=True)[:5]
        ],
        "per_window": [
            {
                "window_id": w.get("window_id"),
                "accepted": w.get("accepted"),
                "committed": w.get("committed_count", len(w.get("committed_keys", []))),
                "remaining": w.get("remaining_unscheduled_count"),
                "cp_status": w.get("cp_status"),
                "replay": w.get("replay_status"),
                "physical": w.get("physical_status"),
                "scope": w.get("scope_status"),
                "runtime_seconds": w.get("window_total_seconds", w.get("total_seconds", 0.0)),
                "resource_configurations": (w.get("production_controller", {}).get("recovery") or {}).get("resource_configurations"),
                "calendar_regimes": (w.get("production_controller", {}).get("recovery") or {}).get("regimes"),
                "timing": w.get("timing", {}),
            }
            for w in windows
        ],
    }
    (OUT / "factory2_full_run_report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md = [
        f"# Factory-2 Production V1\n\nStatus: `{report['status']}`\n",
        f"- Calendar engine: V1\n- Initial operations: 172\n- Windows accepted: {len(accepted)}\n- Total committed/accounted: {committed}\n- Remaining: {len(remaining)}\n- First blocker: `{failure.get('first_blocker', 'NONE')}`\n- Database writes: 0\n",
        "## Runtime\n",
        json.dumps(report["runtime"], indent=2),
        "\n## Remaining operations\n",
        ", ".join(remaining) if remaining else "NONE",
        "\n## Slowest windows\n",
        "\n".join(f"- {x['window_id']}: {x['runtime_seconds']:.3f}s" for x in report["slowest_windows"]),
    ]
    (OUT / "factory2_full_run_report.md").write_text("\n".join(md), encoding="utf-8")

    entries = []
    for path in sorted(OUT.rglob("*")):
        if not path.is_file() or path.name in {"artifact_hash_manifest.json", "hash_verification.json"}:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append({"bytes": path.stat().st_size, "path": path.relative_to(OUT).as_posix(), "sha256": digest})
    manifest = {"algorithm": "SHA-256", "artifact_count": len(entries), "artifacts": entries}
    (OUT / "artifact_hash_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    mismatches = []
    for item in entries:
        if hashlib.sha256((OUT / item["path"]).read_bytes()).hexdigest() != item["sha256"]:
            mismatches.append(item["path"])
    (OUT / "hash_verification.json").write_text(json.dumps({"database_writes": 0, "mismatches": mismatches, "verified": not mismatches}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
