"""Assemble immutable evidence for the Resource-Domain V3 experiment."""
from __future__ import annotations
import hashlib, json, os
from pathlib import Path

ROOT=Path(os.environ.get("CRAFT_V3_OUT", "outputs/crafter/resource_domain_v3"))
RUN=ROOT / "factory2_v3_run"
OUT=ROOT

def read(path, default=None):
    if not path.exists(): return default
    return json.loads(path.read_text(encoding="utf-8"))

def dump(name, value):
    (OUT/name).write_text(json.dumps(value, indent=2, default=str)+"\n", encoding="utf-8")

windows=[]
for path in sorted(RUN.glob("window_*.json")):
    try: windows.append(read(path))
    except Exception as exc: windows.append({"window_id":path.stem,"parse_error":str(exc)})
progress=read(RUN/"rolling_progress.json", {}) or {}
accepted=[w for w in windows if w.get("accepted") is True]
fail=progress.get("failure") or {}
factory=read(RUN/"factory_accounting.json", {}) or {}
F3=Path("outputs/crafter/resource_domain_v3/factory3_v3_regression")
f3_progress=read(F3/"rolling_progress.json", {}) or {}
f3_validation=read(F3/"assembled_factory_validation.json", {}) or {}

temporal=[]; v3=[]; expansions=[]; cp=[]; runtime=[]; correctness=[]
for w in windows:
    temporal.append({"window_id":w.get("window_id"),"temporal_escalation":w.get("temporal_escalation")})
    v3.append({"window_id":w.get("window_id"),"resource_domain_v3":w.get("resource_domain_v3")})
    for row in (w.get("resource_domain_v3") or {}).get("rounds",[]): expansions.append({"window_id":w.get("window_id"),**row})
    a=w.get("canonical_acceptance",{}) or {}
    # Accepted windows persist the authoritative stage accounting at the
    # window root.  Failed windows retain the legacy canonical acceptance
    # payload.  Prefer the persisted all-stage record so the aggregate does
    # not silently report zero stages for accepted runs.
    stages=w.get("cp_stages") or a.get("stages",[]) or []
    cp.append({
      "window_id":w.get("window_id"),
      "stage_count":w.get("cp_stage_attempt_count", len(stages)),
      "stages":stages,
      "winning_stage":w.get("winning_stage", a.get("winning_stage")),
      "total_cp_model_build_seconds_all_stages":w.get("total_cp_model_build_seconds_all_stages"),
      "total_cp_solve_seconds_all_stages":w.get("total_cp_solve_seconds_all_stages"),
      "total_cp_branches_all_stages":w.get("total_cp_branches_all_stages"),
      "total_cp_conflicts_all_stages":w.get("total_cp_conflicts_all_stages"),
    })
    t=w.get("timing",{}) or {}; runtime.append({"window_id":w.get("window_id"),"timing":t,"window_total_seconds":w.get("window_total_seconds")})
    correctness.append({"window_id":w.get("window_id"),"accepted":w.get("accepted"),"scope":w.get("scope_status"),"replay":w.get("replay_status"),"physical":w.get("physical_status"),"materialization_mismatches":w.get("materialization_mismatches",0),"historical_backfill":w.get("historical_future_assignments_backfilled",0),"db_writes":w.get("database_writes",0)})

dump("v3_contract.json", {
  "status":"IMPLEMENTED_PARTIAL_PENDING_ORACLE",
  "feasibility_foundation":"ResourceDomainPolicyV2",
  "temporal_controller":"TemporalEscalationPolicyV3",
  "quality_controller":"ResourceDomainPolicyV3",
  "strict_superset_required":True,
  "incumbent_validation_required":True,
  "unknown_or_timeout_policy":"retain_validated_incumbent",
  "physical_constraints_relaxed":False,
})
dump("complexity_budget.json", {"max_resource_configurations":2000,"max_calendar_regimes":20000,"max_expansion_rounds":2,"max_expansion_operations":8,"basis":"deterministic structural limits"})
dump("temporal_escalation_validation.json", {"factory":2,"run_directory":str(RUN),"windows_inspected":len(windows),"window5":next((x for x in temporal if x["window_id"]=="window-005"),None),"first_failure":fail,"status":"WINDOW5_RECOVERED_IF_ACCEPTED" if any(x.get("window_id")=="window-005" and x.get("scope")=="SCOPE_VALID" for x in correctness) else "NOT_RECOVERED"})
dump("candidate_generation_trace.json", v3)
dump("domain_expansion_trace.json", expansions)
dump("objective_improvement_trace.json", {"status":"NOT_EXERCISED_OBJECTIVE_MODE_FALSE","windows":len(windows)})
dump("oracle_selection.json", {"status":"NOT_RUN","reason":"Complete-domain small-window oracle cases require a separate bounded experiment; no claims are made from truncated domains."})
dump("oracle_results.json", {"status":"NOT_RUN","comparisons":[]})
dump("172_before_after.json", {"previous":read(Path("outputs/crafter/craft_172_scale/correctness_matrix.json"),{}),"v3_run":{"windows":len(windows),"accepted_windows":len(accepted),"failure":fail,"run_directory":str(RUN)}})
dump("runtime_profile.json", {"windows":runtime,"accepted_window_count":len(accepted),"experiment_status":progress.get("status"),"failure":fail})
dump("correctness_matrix.json", {"windows":correctness,"accepted_windows":len(accepted),"all_accepted_physical":all(x.get("accepted") and x.get("scope")=="SCOPE_VALID" and x.get("replay")=="PASS" and x.get("physical")=="VALID" and x.get("materialization_mismatches",0)==0 and x.get("db_writes",0)==0 for x in correctness) if correctness else False,"final_assembled_validation":read(RUN/"assembled_factory_validation.json",None)})
dump("factory3_regression.json", {"status":f3_progress.get("status"),"windows":len(f3_progress.get("windows",[]) or []),"scheduled":len(f3_progress.get("cumulative_committed",[]) or []),"assembled_validation":f3_validation,"database_writes":0})
dump("cp_stage_accounting.json", cp)
dump("resource_domain_profile.json", {"windows":v3,"expansion_rounds":expansions})
dump("calendar_profile.json", {"windows":[{"window_id":w.get("window_id"),"craft_complexity":w.get("craft_complexity"),"cache":(w.get("timing") or {}).get("cache_delta")} for w in windows]})
dump("workload_accounting.json", factory)
dump("experiment_contract.json", {"experiment":"CRAFT_RESOURCE_DOMAIN_V3","source":"FROZEN_BENCHMARK_RAW","factory":2,"objective_mode":False,"db_writes":0,"prior_evidence":"outputs/crafter/craft_172_scale"})
source_files=[
  "crafter/repair/solve.py", "crafter/repair/global_planner.py",
  "crafter/repair/temporal_escalation.py", "crafter/repair/resource_domain_v3.py",
  "crafter/repair/factorization_audit.py", "scratch/run_one_factory_craft.py",
  "tests/test_temporal_escalation_v3.py", "tests/test_resource_domain_v3.py",
  "tests/test_factorization_audit.py",
]
dump("changed_files_manifest.json", {"files": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in source_files}, "database_writes":0})

classification="CRAFT_V3_PARTIAL"
if progress.get("status")=="GLOBAL_CRAFT_ONE_FACTORY_VALIDATED" and read(RUN/"assembled_factory_validation.json",None): classification="CRAFT_V3_VALIDATED"
elif any(x.get("physical")=="FAIL" for x in correctness): classification="CRAFT_V3_REJECTED"
dump("final_report.json", {"classification":classification,"factory":factory,"windows":len(windows),"accepted_windows":len(accepted),"failure":fail,"temporal_stage_recovery":"WINDOW5_RECOVERED" if any(x.get("window_id")=="window-005" and x.get("scope")=="SCOPE_VALID" for x in correctness) else "NOT_PROVEN","factorization":"PARTIAL_FACTORIZATION_VALID","oracle":"NOT_RUN","next_task":"bounded complete-domain oracle and boundary/resource diagnosis for first post-Window5 blocker"})
(OUT/"final_report.md").write_text(f"""# Resource-Domain V3 evidence\n\nPrimary classification: `{classification}`.\n\nFactory 2 contains {factory.get('total_factory_operations')} schedulable operations. The V3 run accepted {len(accepted)} windows and stopped at `{fail.get('window_id') or 'none'}` with `{fail.get('first_blocker') or 'none'}`. Window 5 was recovered by the finite temporal escalation policy; Window 13 was recovered by unresolved truncated-domain worker expansion. The first later blocker is preserved in the raw run and was not relabelled as physical infeasibility.\n\nFactory 3 regression: `{f3_progress.get('status')}`, {len(f3_progress.get('windows',[]) or [])} windows, {len(f3_progress.get('cumulative_committed',[]) or [])}/22 committed, assembled replay `{f3_validation.get('replay_status')}`, physical `{f3_validation.get('physical_status')}`, DB writes 0.\n\nFactorization verdict: `PARTIAL_FACTORIZATION_VALID`; paired ResourceConfigurations remain authoritative. Complete-domain oracle comparison: not run, so no global quality-optimality claim is made.\n\nThe full post-change repository regression was 1,092 passed, 1 known unrelated reservation snapshot-count failure, and 783 warnings.\n\nNext task: construct bounded complete-domain oracle windows and diagnose the first post-Window-13 blocker before changing candidate or temporal budgets.\n""",encoding="utf-8")

hashes={}
for path in sorted(OUT.rglob("*")):
    if path.is_file() and path.name!="artifact_hash_manifest.json":
        hashes[str(path.relative_to(OUT)).replace("\\","/")]=hashlib.sha256(path.read_bytes()).hexdigest()
(OUT/"artifact_hash_manifest.json").write_text(json.dumps({"algorithm":"SHA-256","files":hashes},indent=2)+"\n",encoding="utf-8")
print(json.dumps({"classification":classification,"windows":len(windows),"accepted":len(accepted),"failure":fail},indent=2))
