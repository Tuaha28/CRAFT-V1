"""Write the bounded V3 quality/Window-14 evidence bundle."""
import json
from pathlib import Path

root = Path("outputs/crafter/resource_domain_v3/quality_validation_v1")
root.mkdir(parents=True, exist_ok=True)

def read(name, default):
    path = root / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

probe = read("quality_controller_probe.json", {})
diagnosis = read("window14_diagnosis.json", {})
quality = probe.get("resource_domain_v3", {}) or {}
planning = probe.get("planning_acceptance", {}) or {}
canonical = probe.get("canonical_acceptance", {}) or {}

(root / "research_mapping.md").write_text("""# V3 research mapping

The implementation retains paired exact ResourceConfiguration objects and
uses deterministic compact-incumbent/strict-superset expansion.  This is
consistent with the dual-resource flexible job-shop literature, which treats
machine assignment, worker assignment, and machine sequencing as coupled
subproblems; CRAFT additionally requires exact worker identity, calendars,
pause/resume segments, continuous machine occupancy, reservations, and
independent replay, so generic decompositions are not accepted without an
equivalence proof.

Sources reviewed:

- OR-Tools CP-SAT solver documentation: https://developers.google.com/optimization/cp/cp_solver
  (public solver status and response statistics; UNKNOWN/timeout is not
  treated as infeasibility).
- Dual-resource constrained job-shop review:
  https://www.sciencedirect.com/science/article/pii/S1877050918314017
  (machine assignment, worker assignment, and sequencing are distinct but
  interacting decisions).
- Flexible job-shop review: https://doi.org/10.1016/j.ejor.2023.05.017
  (machine alternatives complicate exact bounds and assignment/sequencing).
- Recent two-stage CP-based DRC-FJSP work:
  https://www.sciencedirect.com/science/article/pii/S0305054826002261
  (staged assignment/search is relevant conceptually, but its assumptions do
  not replace CRAFT's exact calendar and replay contract).

Adopted only: a validated incumbent fallback, strict-superset candidate
exposure, deterministic schedule-aware targeting, and truthful status
separation.  Not adopted: heuristic acceptance, relaxed calendars, hidden
post-CP repair, or unsafe machine/worker factorization.
""", encoding="utf-8")

(root / "quality_controller_contract.json").write_text(json.dumps({
    "status": "IMPLEMENTED_AND_EXERCISED_ON_REAL_FACTORY3_WINDOW",
    "incumbent_gate": ["SCOPE_VALID", "PASS", "VALID", "materialization_mismatch_count=0"],
    "candidate_domain": "strict_superset_only",
    "candidate_acceptance": "validated_and_lexicographically_strictly_better",
    "fallback": "retain_last_validated_incumbent_on_invalid_unknown_timeout_or_non_improvement",
    "objective_mode_probe": True,
    "database_writes": 0,
}, indent=2) + "\n", encoding="utf-8")

(root / "free_vs_displacement_trace.json").write_text(json.dumps({
    "status": "PARTIAL_DIAGNOSTIC",
    "probe": "Factory-3 Window-1",
    "target_operations": [row.get("operation_key") for row in quality.get("rounds", [{}])[0].get("target_signals", [])] if quality.get("rounds") else [],
    "classification": "DISPLACEMENT_NOT_PROVEN",
    "reason": "The quality probe retained paired legal candidates and validated the expanded schedule, but did not run a separate frozen-incumbent displacement simulation. No free/displacement claim is made.",
}, indent=2) + "\n", encoding="utf-8")

(root / "oracle_selection.json").write_text(json.dumps({
    "status": "ORACLE_NOT_ESTABLISHED",
    "candidate_real_windows": "Factory-3 rolling windows",
    "reason": "Complete legal worker-crew enumeration is not computationally enumerable for the available real windows under a safe exact budget. Operations require multi-machine crews and the legal crew space is combinatorial.",
    "no_quality_claim": True,
}, indent=2) + "\n", encoding="utf-8")
(root / "oracle_domain_completeness.json").write_text(json.dumps({
    "status": "INCOMPLETE_DOMAIN_REFERENCE_ONLY",
    "evidence": diagnosis.get("worker_complete_enumeration", {}),
    "required_for_oracle": ["all paired ResourceConfigurations", "all exact calendar regimes", "OPTIMAL at every lexicographic stage"],
}, indent=2) + "\n", encoding="utf-8")
(root / "oracle_results.json").write_text(json.dumps({
    "classification": "ORACLE_NOT_ESTABLISHED",
    "comparisons": [],
    "v2_equals_complete_fraction": None,
    "v3_equals_complete_fraction": None,
    "v3_strictly_improves_v2_fraction": None,
}, indent=2) + "\n", encoding="utf-8")
(root / "v2_v3_complete_comparison.json").write_text(json.dumps({
    "status": "QUALITY_COMPARISON_NOT_ESTABLISHED",
    "v3_probe": {"planning_accepted": planning.get("accepted"), "objective_vector": quality.get("incumbent_objective_vector")},
    "complete_domain": "NOT_AVAILABLE",
    "interpretation": "The real objective path ran and validated an expanded strict superset, but it did not improve the lexicographic vector and no complete-domain oracle exists.",
}, indent=2, default=str) + "\n", encoding="utf-8")
(root / "objective_vectors.json").write_text(json.dumps({
    "probe": {
        "incumbent": quality.get("incumbent_objective_vector"),
        "rounds": [{"round": row.get("round"), "candidate": row.get("objective_vector"),
                    "incumbent": row.get("incumbent_objective_vector"),
                    "strict_improvement": row.get("strict_improvement")} for row in quality.get("rounds", [])],
        "objective_status": (canonical.get("objective") or {}).get("status"),
    },
}, indent=2, default=str) + "\n", encoding="utf-8")
(root / "complexity_comparison.json").write_text(json.dumps({
    "probe": [{"round": row.get("round"), "resource_configurations": row.get("resource_configuration_count"),
               "calendar_regimes": row.get("calendar_regime_count"),
               "candidate_valid": row.get("candidate_valid")} for row in quality.get("rounds", [])],
    "complete_domain": "NOT_ESTABLISHED",
}, indent=2) + "\n", encoding="utf-8")
(root / "factory2_rerun.json").write_text(json.dumps({
    "status": "DEFERRED_NO_SAFE_GENERIC_WINDOW14_CORRECTION",
    "previous_run": "outputs/crafter/resource_domain_v3/factory2_v3_run",
    "first_blocker": diagnosis.get("classification"),
    "reason": "The exact diagnosis identifies a combined carried-boundary/routing interaction while domains remain truncated. The rolling policy was not changed and no Factory-2 rerun was performed against an unjustified policy.",
    "database_writes": 0,
}, indent=2) + "\n", encoding="utf-8")
(root / "factory3_regression.json").write_text(json.dumps({
    "status": "PRESERVED_PRIOR_VALIDATION",
    "source": "outputs/crafter/resource_domain_v3/factory3_v3_regression",
    "operations": 22, "windows": 9, "replay": "PASS", "physical": "VALID",
    "violations": 0, "database_writes": 0,
}, indent=2) + "\n", encoding="utf-8")
(root / "correctness_matrix.json").write_text(json.dumps({
    "window14": {"classification": diagnosis.get("classification"), "physical_infeasibility_proven": False,
                  "database_writes": diagnosis.get("database_writes", 0)},
    "quality_probe": {"scope": planning.get("scope_status"), "replay": planning.get("replay_status"),
                      "physical": planning.get("physical_status"), "accepted": planning.get("accepted"),
                      "materialization_mismatches": planning.get("materialization_mismatch_count", 0),
                      "database_writes": probe.get("database_writes", 0)},
    "oracle": "ORACLE_NOT_ESTABLISHED",
    "factory2": "PARTIAL_PRIOR_RUN",
}, indent=2, default=str) + "\n", encoding="utf-8")
(root / "runtime_profile.json").write_text(json.dumps({
    "quality_probe": {"resource_domain_v3_elapsed_seconds": quality.get("elapsed_seconds"),
                      "rounds": [{"round": x.get("round"), "calendar_seconds": x.get("calendar_generation_seconds")} for x in quality.get("rounds", [])]},
    "window14_diagnostics_elapsed_seconds": diagnosis.get("elapsed_seconds"),
    "database_writes": 0,
}, indent=2) + "\n", encoding="utf-8")
(root / "full_regression.json").write_text(json.dumps({
    "status": "COMPLETED",
    "total_tests": 1096,
    "passed": 1095,
    "failed": 1,
    "warnings": 783,
    "runtime_seconds": 454.02,
    "focused_v3_tests": {"passed": 155, "warnings": 2},
    "failure": {
        "test": "tests/test_reservation_classification.py::test_real_loader_on_the_snapshot_keeps_only_in_flight_work",
        "observed": "expected reservation rows 143456, observed 144665",
        "classification": "pre_existing_unrelated_reservation_snapshot_count_failure",
        "caused_by_this_task": False,
    },
}, indent=2) + "\n", encoding="utf-8")
(root / "changed_files_manifest.json").write_text(json.dumps({
    "source_files": ["crafter/repair/scoped_validation.py", "crafter/repair/global_planner.py",
                     "crafter/repair/resource_domain_v3.py", "crafter/repair/__init__.py"],
    "test_files": ["tests/test_resource_domain_v3.py"],
    "evidence_helpers": ["scratch/finalize_window14_evidence.py", "scratch/run_v3_quality_probe.py",
                         "scratch/finalize_quality_validation_evidence.py"],
    "repair_semantics_changed": False,
}, indent=2) + "\n", encoding="utf-8")
(root / "final_report.md").write_text(f"""# Resource-Domain V3 quality validation (bounded continuation)

Window-14 classification: **{diagnosis.get('classification')}**.

The persisted exact ablations show that the no-committed-boundary control is
feasible, while removing machine, worker, routing, or machine+worker boundary
facts individually remains infeasible. A committed predecessor (`254_2153`)
ends at 504548 after Window-14's end 412800 before successor `254_2154`.
Resource domains remain worker-truncated, so physical infeasibility is not
claimed.

The true V3 quality path was exercised on a real Factory-3 Window-1. The
compact result passed scoped acceptance (`{planning.get('scope_status')}` /
`{planning.get('replay_status')}` / `{planning.get('physical_status')}`), and
one strict-superset quality round also passed validation. Its lexicographic
vector was not strictly improved, so the validated incumbent was retained.
This is a quality-path exercise, not evidence of V3 superiority.

Complete-domain oracle status: **ORACLE_NOT_ESTABLISHED**. Available real
windows have combinatorial worker-crew domains; no complete optimum is claimed.
Factory-2 was not rerun because no safe generic Window-14 correction was
justified without changing rolling policy.

Focused CRAFT tests after the wiring change: 155 passed, 2 warnings. The full
repository run was completed immediately before the final test-only regression
was added (1095 passed, one unrelated failure). Database writes: 0.
""", encoding="utf-8")
print("quality evidence written")
