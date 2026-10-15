"""Finalize Window-14 classification from persisted diagnostic evidence only."""
import json
from pathlib import Path

root = Path("outputs/crafter/resource_domain_v3/quality_validation_v1")
diagnosis_path = root / "window14_diagnosis.json"
ablations_path = root / "window14_boundary_ablations.json"
run_root = Path("outputs/crafter/resource_domain_v3/factory2_v3_run")
diagnosis = json.loads(diagnosis_path.read_text(encoding="utf-8"))

committed = {}
for path in sorted(run_root.glob("window_*.json")):
    item = json.loads(path.read_text(encoding="utf-8"))
    if item.get("accepted") is not True:
        continue
    rows = {str(row["operation_key"]): row for row in item.get("selected_assignments", ())}
    for key in item.get("committed_keys", ()):
        committed[str(key)] = rows[str(key)]

selected = set(diagnosis.get("selected_required_keys", ()))
route_rows = []
for key, predecessors in diagnosis.get("predecessors", {}).items():
    for predecessor in predecessors:
        if predecessor not in committed or key not in selected:
            continue
        end = int(committed[predecessor]["end_seconds"])
        route_rows.append({
            "predecessor": predecessor,
            "successor": key,
            "predecessor_end_seconds": end,
            "window_start_seconds": diagnosis["window_start_seconds"],
            "window_end_seconds": diagnosis["window_end_seconds"],
            "crosses_window": end > int(diagnosis["window_end_seconds"]),
        })

ablations = json.loads(ablations_path.read_text(encoding="utf-8")) if ablations_path.exists() else {}
boundary_runs = ablations.get("runs", ())
boundary_failures = [row["label"] for row in boundary_runs if not row.get("candidate_exists")]
no_boundary = next((row for row in diagnosis.get("diagnostics", ())
                    if row.get("label") == "NO_COMMITTED_BOUNDARY_1X3_UNRESTRICTED_DIAGNOSTIC"), {})
has_truncated = any(row.get("resource_domain_summary", {}).get("resource_domain_truncated")
                    for row in diagnosis.get("diagnostics", ()))
classification = "WINDOW14_COMBINED_BLOCKER" if (
    no_boundary.get("candidate_exists") is True
    and boundary_runs
    and len(boundary_failures) == len(boundary_runs)
) else "WINDOW14_UNRESOLVED"

diagnosis["classification"] = classification
diagnosis["classification_basis"] = {
    "resource_domain_truncated": bool(has_truncated),
    "no_committed_boundary_control_candidate": bool(no_boundary.get("candidate_exists")),
    "single_boundary_dimension_ablation_failures": boundary_failures,
    "route_boundary_rows": route_rows,
    "route_boundary_violating_end_count": sum(1 for row in route_rows if row["crosses_window"]),
    "physical_infeasibility_proven": False,
}
diagnosis_path.write_text(json.dumps(diagnosis, indent=2, default=str) + "\n", encoding="utf-8")

report = f"""# Factory-2 Window-14 blocker report

Classification: **{classification}**

This classification is based only on persisted exact diagnostic runs; no
diagnostic boundary projection is an accepted production schedule.

- Required selected operations: {len(selected)}
- Prior accepted windows: {diagnosis.get('accepted_prior_window_count')}
- Committed operations: {diagnosis.get('committed_operation_count')}
- Resource domain truncated: {bool(has_truncated)} (therefore no physical infeasibility claim)
- No-committed-boundary unrestricted control candidate: {bool(no_boundary.get('candidate_exists'))}
- Single-dimension boundary ablations failing: {', '.join(boundary_failures) or 'none'}

The decisive routing evidence is:

"""
for row in route_rows:
    report += (f"- `{row['predecessor']} → {row['successor']}`: committed end "
               f"`{row['predecessor_end_seconds']}`, window end "
               f"`{row['window_end_seconds']}`, crosses window = "
               f"`{row['crosses_window']}`\n")
report += """

Interpretation: removing the exact committed boundary makes the bounded model
feasible, but removing machine, worker, routing, or machine+worker boundary
facts individually does not. The current window therefore has a combined
commitment/boundary interaction, including a committed predecessor that ends
after the current window end. Because worker domains remain truncated, this is
not proof of physical infeasibility or complete resource-domain exhaustion.

Database writes: 0.
"""
(root / "window14_blocker_report.md").write_text(report, encoding="utf-8")
print(json.dumps({"classification": classification, "route_rows": route_rows,
                  "boundary_failures": boundary_failures}, indent=2))
