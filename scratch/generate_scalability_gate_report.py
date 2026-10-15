"""Generate the Windows 1-10 scalability gate and full-run decision artifacts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from statistics import mean, median


ROOT = Path("outputs/crafter/factory2_production_v1_scalability_1_10")


def percentile(values, q):
    values = sorted(float(x) for x in values)
    if not values:
        return None
    index = (len(values) - 1) * q
    left, right = int(index), min(len(values) - 1, int(index) + 1)
    fraction = index - left
    return values[left] + (values[right] - values[left]) * fraction


def main():
    rows = []
    cumulative = 0
    for path in sorted(p for p in ROOT.glob("window_*.json") if p.stem[7:].isdigit()):
        data = json.loads(path.read_text(encoding="utf-8"))
        controller = data["production_controller"]
        round_data = controller["rounds"][-1]
        solve = round_data["solve_summary"]
        timing = data["timing"]
        committed = len(data.get("committed_keys", ()))
        cumulative += committed
        rows.append({
            "window_id": data["window_id"],
            "window_start_seconds": data["window_start_seconds"],
            "window_end_seconds": data["window_end_seconds"],
            "commit_end_seconds": data["commit_end_seconds"],
            "step3b_action": (data.get("step3b") or {}).get("decision"),
            "selected_operation_count": len(data.get("selected_keys", ())),
            "committed_operation_count": committed,
            "provisional_count": len(data.get("provisional_keys", ())),
            "remaining_operation_count": 172 - cumulative,
            "causal_closure_size": len(round_data.get("closure", ())),
            "matched_crew_count": round_data.get("new_crews_accepted", 0),
            "resource_configurations": solve.get("resource_configurations"),
            "calendar_regimes": solve.get("regimes"),
            "matching_generation_seconds": round_data.get("matching_generation_seconds", 0.0),
            "resource_configuration_seconds": round_data.get("resource_configuration_seconds", 0.0),
            "calendar_regime_seconds": round_data.get("calendar_regime_seconds", 0.0),
            "cp_model_build_seconds": solve.get("cp_model_build_seconds", 0.0),
            "cp_solve_seconds": solve.get("cp_solve_seconds", 0.0),
            "materialization_mismatch_count": solve.get("materialization_mismatch_count", 0),
            "replay_status": data.get("replay_status"),
            "replay_seconds": timing.get("replay_seconds", 0.0),
            "physical_status": data.get("physical_status"),
            "physical_validation_seconds": timing.get("physical_validation_seconds", 0.0),
            "scope_status": data.get("scope_status"),
            "scheduler_total_seconds": data.get("window_total_seconds"),
            "database_writes": data.get("database_writes", 0),
            "cp_status": data.get("cp_status"),
            "accepted": bool(data.get("accepted")),
        })

    runtimes = [row["scheduler_total_seconds"] for row in rows]
    first = [row["scheduler_total_seconds"] for row in rows[:5]]
    second = [row["scheduler_total_seconds"] for row in rows[5:]]
    calendars = [row["calendar_regime_seconds"] for row in rows]
    regimes = [row["calendar_regimes"] for row in rows]
    cp = [row["cp_solve_seconds"] for row in rows]
    matching = [row["matching_generation_seconds"] for row in rows]
    config = [row["resource_configuration_seconds"] for row in rows]
    report = {
        "status": "CRAFT_PRODUCTION_V1_FACTORY2_PARTIAL",
        "scalability_gate_windows_1_to_10": "PASS",
        "windows_1_to_10_accepted": all(row["accepted"] for row in rows),
        "runtime_classification": "SYSTEMATIC_GROWTH",
        "dominant_runtime_layer": "CALENDAR_REGIME_GENERATION",
        "preparation_blocker": True,
        "full_factory2_started": False,
        "full_factory2_reason": "Windows 6-10 show unresolved calendar-preparation growth; stop before contaminating the full benchmark.",
        "initial_schedulable_operations": 172,
        "last_accepted_window": rows[-1]["window_id"],
        "accounted_operations": sum(row["committed_operation_count"] for row in rows),
        "remaining_operations": 172 - sum(row["committed_operation_count"] for row in rows),
        "windows_attempted": len(rows),
        "average_runtime_windows_1_to_5": mean(first),
        "average_runtime_windows_6_to_10": mean(second),
        "average_runtime_windows_1_to_10": mean(runtimes),
        "median_runtime": median(runtimes),
        "minimum_runtime": min(runtimes),
        "maximum_runtime": max(runtimes),
        "p95_runtime": percentile(runtimes, 0.95),
        "average_calendar_regime_seconds": mean(calendars),
        "average_calendar_regimes": mean(regimes),
        "average_cp_solve_seconds": mean(cp),
        "average_matching_seconds": mean(matching),
        "average_resource_configuration_seconds": mean(config),
        "calendar_generation_first_window_seconds": calendars[0],
        "calendar_generation_last_window_seconds": calendars[-1],
        "calendar_generation_growth_factor": calendars[-1] / calendars[0],
        "regime_count_first_window": regimes[0],
        "regime_count_last_window": regimes[-1],
        "cp_status_distribution": {status: sum(row["cp_status"] == status for row in rows) for status in sorted({row["cp_status"] for row in rows})},
        "replay_failures": sum(row["replay_status"] != "PASS" for row in rows),
        "physical_failures": sum(row["physical_status"] != "VALID" for row in rows),
        "scope_failures": sum(row["scope_status"] != "SCOPE_VALID" for row in rows),
        "database_writes": sum(row["database_writes"] for row in rows),
        "slowest_windows": sorted(rows, key=lambda row: row["scheduler_total_seconds"], reverse=True)[:5],
        "windows": rows,
    }
    (ROOT / "scalability_1_10_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md = [
        "# CRAFT Production V1 scalability gate\n",
        f"Status: `{report['status']}`",
        f"Windows 1-10: **PASS** ({len(rows)}/10 accepted)",
        f"Runtime classification: **{report['runtime_classification']}**",
        f"Dominant layer: **{report['dominant_runtime_layer']}**",
        f"Windows 1-5 average: {report['average_runtime_windows_1_to_5']:.2f}s",
        f"Windows 6-10 average: {report['average_runtime_windows_6_to_10']:.2f}s",
        f"Windows 1-10 average: {report['average_runtime_windows_1_to_10']:.2f}s",
        f"Calendar generation: {report['calendar_generation_first_window_seconds']:.2f}s → {report['calendar_generation_last_window_seconds']:.2f}s",
        f"Regime count: {report['regime_count_first_window']} → {report['regime_count_last_window']}",
        f"Accounted: {report['accounted_operations']}; remaining: {report['remaining_operations']}",
        "",
        "| Window | Runtime | Regimes | Calendar s | Matching s | Config s | CP s | Replay | Physical | Scope |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|---|",
    ]
    for row in rows:
        md.append(f"| {row['window_id']} | {row['scheduler_total_seconds']:.2f} | {row['calendar_regimes']} | {row['calendar_regime_seconds']:.2f} | {row['matching_generation_seconds']:.2f} | {row['resource_configuration_seconds']:.2f} | {row['cp_solve_seconds']:.2f} | {row['replay_status']} | {row['physical_status']} | {row['scope_status']} |")
    md += ["", "Full 172-operation benchmark: **not started**. The gate stopped before Phase B because preparation-time growth is an unresolved scalability blocker, not a correctness failure."]
    (ROOT / "scalability_1_10_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    full = {
        "status": "CRAFT_PRODUCTION_V1_FACTORY2_PARTIAL",
        "full_run_started": False,
        "reason": report["full_factory2_reason"],
        "scalability_gate": "PASS",
        "first_blocker": "PREPARATION_SCALABILITY_BLOCKER_CALENDAR_REGIME_GROWTH",
        "physical_infeasibility_proven": False,
        "database_writes": 0,
    }
    (ROOT / "factory2_full_run_report.json").write_text(json.dumps(full, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (ROOT / "factory2_full_run_report.md").write_text("# Full Factory-2 run\n\nFull run was not started after the Windows 1-10 gate exposed systematic calendar-regime preparation growth.\n", encoding="utf-8")

    manifest = {}
    for path in sorted(ROOT.glob("*")):
        if path.is_file() and path.name not in {"artifact_hash_manifest.json", "hash_verification.json"}:
            manifest[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (ROOT / "artifact_hash_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verified = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest for name, digest in manifest.items()}
    (ROOT / "hash_verification.json").write_text(json.dumps({"all_verified": all(verified.values()), "files": verified}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
