"""Small real Factory-3 quality-path probe; no commits or database writes."""
import json
from pathlib import Path
import sys

sys.path.insert(0, ".")

from crafter.disruptions.benchmark import _load_state
from crafter.repair.global_planner import factory_future_operation_keys
from crafter.repair.planning import PlanningContextV1, build_planning_state
from crafter.repair.resource_domain_v3 import ResourceDomainPolicyV3, run_v3_planning_schedule
from crafter.repair.scoped_validation import validate_canonical_planning_candidate

ROOT = Path("outputs/crafter/resource_domain_v3/quality_validation_v1")
FACTORY3 = Path("outputs/crafter/resource_domain_v3/factory3_v3_regression/window_001.json")


def main():
    loaded = _load_state()
    persisted = json.loads(FACTORY3.read_text(encoding="utf-8"))
    required = tuple(persisted["pre_solve_required_keys"])
    context = PlanningContextV1(
        "quality-probe-f3-w1", "INITIAL_PLAN", 3, 0, 0, 86400, 20000,
        required, "FROZEN_BENCHMARK_RAW", snapshot_as_of_seconds=0)
    state = build_planning_state(loaded, context)
    future = factory_future_operation_keys(state, 3)

    def validator(result):
        checked = validate_canonical_planning_candidate(
            result, result.get("state", state), required_operation_keys=required,
            boundary_facts=(), future_outside_operation_keys=future)
        result["planning_acceptance"] = checked
        return bool(checked.get("accepted"))

    result = run_v3_planning_schedule(
        state, objective_mode=True, objective_time_limit_seconds=15,
        policy=ResourceDomainPolicyV3(max_expansion_rounds=1,
                                      max_expansion_operations=2,
                                      diagnostic_time_limit_seconds=5,
                                      max_resource_configurations=5000,
                                      max_calendar_regimes=10000),
        incumbent_validator=validator)
    payload = {
        "fixture": "Factory-3 persisted Window-1 real planning state",
        "objective_mode": True,
        "required_operation_keys": required,
        "future_outside_operation_count": len(future),
        "planning_acceptance": result.get("planning_acceptance"),
        "resource_domain_v3": result.get("resource_domain_v3"),
        "canonical_acceptance": result.get("acceptance"),
        "database_writes": result.get("acceptance", {}).get("database_writes", 0),
    }
    (ROOT / "quality_controller_probe.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    (ROOT / "incumbent_diagnostics.json").write_text(
        json.dumps({"diagnostics": (result.get("resource_domain_v3", {}) or {}).get(
            "incumbent_quality_diagnostics", ())}, indent=2, default=str) + "\n",
        encoding="utf-8")
    (ROOT / "candidate_signal_trace.json").write_text(
        json.dumps({"rounds": (result.get("resource_domain_v3", {}) or {}).get(
            "rounds", ())}, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": (result.get("resource_domain_v3", {}) or {}).get("status"),
        "accepted": (result.get("planning_acceptance", {}) or {}).get("accepted"),
        "rounds": len((result.get("resource_domain_v3", {}) or {}).get("rounds", ())),
        "db_writes": payload["database_writes"],
    }, default=str), flush=True)


if __name__ == "__main__":
    main()
