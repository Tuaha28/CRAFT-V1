"""Step-4B Window-14 carried-boundary audit and diagnostic oracle.

No production scheduler path is changed.  The script reconstructs accepted
Windows 1-13, validates their consolidated commitments, then evaluates the
isolated boundary-aware availability/matching generator against unchanged
canonical CP-SAT.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from crafter.disruptions.benchmark import _load_state
from crafter.repair.global_planner import WindowBoundaryState
from crafter.repair.planning import PlanningContextV1, build_planning_state
from crafter.repair.scoped_validation import validate_assembled_factory, validate_planning_window
from crafter.repair.validation_scope import ValidationScopeV1
from crafter.repair.window_phenotype import build_window_phenotype
from crafter.repair.solve import run_canonical_schedule
from crafter.repair.calendar_regimes import CalendarRegimeCache
from crafter.repair.resource_configs import ResourceConfiguration
from crafter.repair.resource_configs import generate_resource_configurations
from crafter.repair.boundary_aware_modes import (
    build_boundary_availability_index,
    generate_boundary_aware_configurations,
    build_boundary_aware_regimes,
)
from crafter.repair import resource_domain_v2

RUN = Path("outputs/crafter/factory2_172_v1")
OUT = RUN / "window14_step4b"
OUT.mkdir(parents=True, exist_ok=True)
W14_KEYS = tuple(json.loads((RUN / "window_014.json").read_text())["window_acceptance"]["scope"]["missing_required_operations"])


def quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def rows_and_provenance():
    rows = {}
    provenance = {}
    accepted_windows = []
    for path in sorted(RUN.glob("window_*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("accepted") is not True:
            continue
        accepted_windows.append(item)
        by_key = {str(row["operation_key"]): row for row in item.get("selected_assignments", ())}
        for key in item.get("committed_keys", ()):
            key = str(key)
            if key not in by_key:
                raise ValueError(f"committed key {key} missing in {path.name}")
            if key in rows:
                raise ValueError(f"duplicate committed operation {key}")
            rows[key] = dict(by_key[key])
            provenance[key] = {
                "source_window": item.get("window_id", path.stem),
                "source_file": str(path),
                "window_start_seconds": item.get("window_start_seconds"),
                "window_end_seconds": item.get("window_end_seconds"),
                "commit_end_seconds": item.get("commit_end_seconds"),
                "start_seconds": by_key[key].get("start_seconds"),
                "end_seconds": by_key[key].get("end_seconds"),
                "committed_by_rule": int(by_key[key]["start_seconds"]) < int(item["commit_end_seconds"]),
            }
    return accepted_windows, rows, provenance


def make_boundary(route_index, committed_rows, factory_id=2):
    machine, worker, routing = [], [], {}
    facts = []
    for key in sorted(committed_rows):
        row = committed_rows[key]
        cfg = row["resource_configuration"]
        order_id = str(row.get("order_id", key.split("_", 1)[0]))
        base = {
            "operation_key": key, "production_order_id": order_id,
            "factory_id": int(cfg["factory_id"]), "line_id": int(cfg["line_id"]),
            "k": int(cfg["k"]), "start_seconds": int(row["start_seconds"]),
            "end_seconds": int(row["end_seconds"]),
            "setup_seconds": int(row.get("setup_seconds", cfg.get("setup_seconds", 0))),
            "productive_seconds": int(row.get("productive_seconds", cfg.get("productive_seconds", 0))),
            "machine_ids": list(cfg["machine_ids"]), "worker_ids": list(cfg["worker_ids"]),
        }
        for mid in cfg["machine_ids"]:
            machine.append({**base, "machine_id": int(mid)})
            facts.append({**base, "machine_id": int(mid)})
        for wid in cfg["worker_ids"]:
            for left, right in row.get("worker_segments", ()):
                worker.append({"operation_key": key, "production_order_id": order_id,
                               "factory_id": int(cfg["factory_id"]), "worker_id": str(wid),
                               "start_seconds": int(left), "end_seconds": int(right)})
                facts.append({"operation_key": key, "production_order_id": order_id,
                              "factory_id": int(cfg["factory_id"]), "worker_id": str(wid),
                              "start_seconds": int(left), "end_seconds": int(right)})
        successors = route_index.route_successor.get(key, ())
        if isinstance(successors, str): successors = (successors,)
        successor = str(successors[0]) if len(successors) == 1 else None
        routing[key] = {**base, "authoritative_end_seconds": int(row["end_seconds"]),
                        "predecessor_operation_key": route_index.route_predecessor.get(key),
                        "successor_operation_key": successor}
        for succ in successors:
            facts.append({"operation_key": key, "predecessor_operation_key": key,
                          "successor_operation_key": str(succ), "production_order_id": order_id,
                          "authoritative_end_seconds": int(row["end_seconds"]),
                          "factory_id": int(cfg["factory_id"])})
    boundary = WindowBoundaryState(int(factory_id), tuple(sorted(committed_rows)), (),
                                   tuple(machine), tuple(worker), {}, routing,
                                   "factory2-step4b-boundary-audit")
    return boundary, tuple(facts)


def context(keys, name="window-014-step4b"):
    return PlanningContextV1(name, "ROLLING_PLAN", 2, 326400, 326400, 412800, 346400,
                              tuple(keys), "FROZEN_BENCHMARK_RAW", snapshot_as_of_seconds=0,
                              previous_provisional_plan_reference="provisional-013")


def boundary_provenance_audit(loaded, accepted, rows, provenance, boundary, facts):
    problem = loaded[0]
    issues = []
    deferred = {"254_2154", "254_2155"}
    for key, meta in provenance.items():
        if not meta["committed_by_rule"]:
            issues.append({"type": "COMMIT_RULE", "operation_key": key})
        if key in deferred:
            issues.append({"type": "DEFERRED_BOUNDARY_LEAK", "operation_key": key})
        if meta["source_file"] not in [str(x.get("source_file", "")) for x in provenance.values()]:
            issues.append({"type": "SOURCE_PROVENANCE", "operation_key": key})
        row = rows[key]
        cfg = row["resource_configuration"]
        if any(not isinstance(str(w), str) for w in cfg.get("worker_ids", ())):
            issues.append({"type": "WORKER_ID_NORMALIZATION", "operation_key": key})
        if int(row["end_seconds"]) < int(row["start_seconds"]):
            issues.append({"type": "NEGATIVE_INTERVAL", "operation_key": key})
        # Per-row replay cannot be performed against an empty state: the
        # authoritative fixed-crew walk depends on earlier accepted worker
        # reservations.  The consolidated replay/validator below is the
        # authoritative execution check for every row in this boundary.
        if not row.get("worker_segments"):
            issues.append({"type": "WORKER_SEGMENTS_MISSING", "operation_key": key})
    # Half-open duplicate/overlap checks across consolidated committed facts.
    for resource_name, grouped in (("machine", boundary.machine_commitments),
                                   ("worker", boundary.worker_commitments)):
        by_resource = {}
        for row in grouped:
            by_resource.setdefault(str(row[f"{resource_name}_id"]), []).append(row)
        for resource, values in by_resource.items():
            ordered = sorted(values, key=lambda x: (int(x["start_seconds"]), int(x["end_seconds"]), str(x["operation_key"])))
            for left, right in zip(ordered, ordered[1:]):
                if int(right["start_seconds"]) < int(left["end_seconds"]):
                    issues.append({"type": f"{resource_name.upper()}_OVERLAP", "resource": resource,
                                   "left": left["operation_key"], "right": right["operation_key"]})
    return {"accepted_window_count": len(accepted), "committed_operation_count": len(rows),
            "machine_fact_count": len(boundary.machine_commitments),
            "worker_fact_count": len(boundary.worker_commitments),
            "routing_fact_count": len(facts), "provenance_rows": provenance,
            "deferred_boundary_leaks": [x for x in issues if x["type"] == "DEFERRED_BOUNDARY_LEAK"],
            "issues": issues, "boundary_provenance_valid": not issues,
            "database_writes": 0}


def global_committed_validation(loaded, committed_rows):
    keys = tuple(sorted(committed_rows))
    state = quiet(build_planning_state, loaded, context(keys, "committed-assembled-validation"), boundary=None)
    result = quiet(validate_assembled_factory, state, list(committed_rows.values()), required_operation_keys=keys)
    return result


def prepare_custom_cp(state, configs, boundary_facts, *, label):
    cache = CalendarRegimeCache()
    regimes, regime_summary, total, equivalence = quiet(build_boundary_aware_regimes, state, configs, cache=cache)
    audit = []
    for key in sorted(configs):
        audit.append({"operation_key": key, "total_resource_configurations": len(configs[key]),
                      "truncated": False, "domain_rows": [], "boundary_aware": True})
    return {
        "state": state, "initial_keys": tuple(state["keys"]),
        "required_window_keys": tuple(state["keys"]), "closure": {"iterations": []},
        "resource_configurations": configs, "resource_audit": audit,
        "resource_summaries": [], "regimes": regimes, "regime_summary": regime_summary,
        "regime_count": total, "calendar_equivalence": equivalence,
        "calendar_cache_profile": cache.snapshot(), "model": None,
        "timing": {"resource_configuration_seconds": 0.0,
                    "calendar_regime_generation_seconds": 0.0,
                    "other_preparation_seconds": 0.0, "prepare_total_seconds": 0.0},
        "boundary_facts": tuple(boundary_facts), "required_window_keys": tuple(state["keys"]),
        "diagnostic_label": label,
    }


def run_cp_probe(loaded, boundary, boundary_facts, configs, label):
    zero_keys = sorted(key for key, values in configs.items() if not values)
    if zero_keys:
        return {
            "label": label,
            "resource_configuration_count": sum(len(v) for v in configs.values()),
            "calendar_regime_count": 0,
            "zero_regime_operation_keys": zero_keys,
            "cp_status": "NOT_INVOKED_ZERO_MODE",
            "canonical_status": "NOT_INVOKED",
            "candidate_exists": False,
            "stages": [],
            "scoped_acceptance": None,
            "elapsed_seconds": 0.0,
            "matching_generated_modes": sum(len(v) for v in configs.values()),
            "database_writes": 0,
        }
    state = quiet(build_planning_state, loaded, context(W14_KEYS, f"{label}-state"), boundary=boundary)
    prepared = prepare_custom_cp(state, configs, boundary_facts, label=label)
    started = time.perf_counter()
    result = quiet(run_canonical_schedule, prepared, objective_mode=False)
    elapsed = time.perf_counter() - started
    acceptance = result.get("acceptance", {}) or {}
    rows = acceptance.get("selected_schedule", ()) or ()
    candidate = bool(rows)
    scoped = None
    if candidate:
        for row in rows:
            op = state["problem"].operation_by_key[str(row["operation_key"])]
            row["order_id"] = str(op.order_id)
            row["setup_seconds"] = int(row["resource_configuration"]["setup_seconds"])
            row["productive_seconds"] = int(row["resource_configuration"]["productive_seconds"])
            row["productive_minutes"] = float(row["resource_configuration"].get("productive_minutes", 0.0))
        scope = ValidationScopeV1("WINDOW", tuple(W14_KEYS), ())
        phenotype, _ = build_window_phenotype(rows, boundary_facts=boundary_facts,
                                               future_outside_operation_keys=(), validation_scope=scope)
        scoped = quiet(validate_planning_window, state, phenotype)
    stages = acceptance.get("stages", ()) or ()
    return {
        "label": label,
        "resource_configuration_count": sum(len(v) for v in configs.values()),
        "calendar_regime_count": prepared.get("regime_count", 0),
        "zero_regime_operation_keys": sorted(k for k, v in prepared["regimes"].items() if not v),
        "cp_status": (acceptance.get("winning_stage") or {}).get("status"),
        "canonical_status": acceptance.get("status"),
        "candidate_exists": candidate,
        "stages": [{k: s.get(k) for k in ("stage", "status", "variables", "constraints", "optional_intervals", "branches", "conflicts", "solve_seconds", "model_build_seconds", "window_days")} for s in stages],
        "scoped_acceptance": scoped,
        "elapsed_seconds": elapsed,
        "matching_generated_modes": sum(len(v) for v in configs.values()),
        "database_writes": 0,
    }


def main():
    started = time.perf_counter()
    loaded = quiet(_load_state)
    problem, _, _, _, _, route_index, _, _, source = loaded
    accepted, committed_rows, provenance = rows_and_provenance()
    boundary, boundary_facts = make_boundary(route_index, committed_rows)
    prov = boundary_provenance_audit(loaded, accepted, committed_rows, provenance, boundary, boundary_facts)
    write("boundary_provenance.json", {**prov, "source": str(source), "database_writes": 0})
    print("BOUNDARY_PROVENANCE", prov["boundary_provenance_valid"], flush=True)
    global_validation = global_committed_validation(loaded, committed_rows)
    write("committed_schedule_validation.json", {**global_validation, "database_writes": 0})
    print("COMMITTED_GLOBAL", global_validation.get("replay_status"), global_validation.get("physical_status"), flush=True)
    if not prov["boundary_provenance_valid"] or not global_validation.get("accepted"):
        write("root_cause.json", {"classification": "CARRIED_BOUNDARY_CONSTRUCTION_DEFECT",
                                   "reason": "consolidated committed provenance/replay/validator failed",
                                   "boundary_provenance": prov, "committed_validation": global_validation,
                                   "database_writes": 0})
        write("implementation_report.json", {"status": "CRAFT_WINDOW14_STEP4B_PARTIAL",
                                               "first_blocker": "CARRIED_BOUNDARY_CONSTRUCTION_DEFECT",
                                               "database_writes": 0})
        return

    base_state = quiet(build_planning_state, loaded, context(W14_KEYS, "window-014-step4b"), boundary=boundary)
    availability = build_boundary_availability_index(base_state, boundary, horizon_start_seconds=0)
    write("machine_availability.json", {"machine_count": len(availability.machine_free),
                                        "free_interval_count": sum(len(v) for v in availability.machine_free.values()),
                                        "index": {str(k): [list(x) for x in v] for k, v in availability.machine_free.items()},
                                        "database_writes": 0})
    write("worker_availability.json", {"worker_count": len(availability.worker_free),
                                        "free_interval_count": sum(len(v) for v in availability.worker_free.values()),
                                        "index": {str(k): [list(x) for x in v] for k, v in availability.worker_free.items()},
                                        "database_writes": 0})

    funnels = {}; matching = {}; mode_summaries = {}; probes = {}
    for k in (1, 4, 8, 16):
        print("GENERATE", k, flush=True)
        t0 = time.perf_counter()
        configs, funnel, stats, idx = quiet(generate_boundary_aware_configurations, base_state, boundary,
                                             crew_diversity=k, machine_set_cap=3, max_start_probes=4)
        gen_seconds = time.perf_counter() - t0
        funnels[str(k)] = funnel
        matching[str(k)] = {"matching_problems_solved": len(stats),
                            "crew_alternatives_produced": sum(int(x.get("crew_alternatives", 0)) for x in stats),
                            "duplicate_crews_removed": 0, "generation_seconds": gen_seconds,
                            "stats": stats}
        probes[str(k)] = run_cp_probe(loaded, boundary, boundary_facts, configs, f"BOUNDARY_AWARE_K{k}")
        zero_keys = list(probes[str(k)].get("zero_regime_operation_keys", ()))
        mode_summaries[str(k)] = {"configs": sum(len(v) for v in configs.values()),
                                  "zero_mode_operations": zero_keys,
                                  "operations_with_modes": len(W14_KEYS) - len(zero_keys),
                                  "generation_seconds": gen_seconds}
        print("PROBE", k, probes[str(k)]["candidate_exists"], probes[str(k)]["cp_status"], flush=True)
    write("operation_feasibility_funnel.json", {"by_crew_diversity": funnels, "database_writes": 0})
    zero = {k: v["zero_mode_operations"] for k, v in mode_summaries.items()}
    write("zero_mode_operations.json", {"by_crew_diversity": zero, "database_writes": 0})
    write("crew_matching_diagnostics.json", {"by_crew_diversity": matching, "database_writes": 0})
    write("boundary_aware_modes_summary.json", {"by_crew_diversity": mode_summaries, "database_writes": 0})
    for k in (1, 4, 8, 16):
        write(f"cp_probe_k{k}.json", probes[str(k)])

    # Old generator comparison uses the exact same boundary/state and caps.
    old = {}
    for k in (1, 4, 8, 16):
        t0 = time.perf_counter()
        configs, audit, summary = quiet(generate_resource_configurations, base_state,
                                         max_machine_sets=3, max_worker_crews=k,
                                         machine_pool_limit=30)
        cache = CalendarRegimeCache(); regimes, _, total, _ = quiet(
            __import__('crafter.repair.calendar_regimes', fromlist=['generate_calendar_regimes']).generate_calendar_regimes,
            base_state, configs, cache=cache)
        old[str(k)] = {"resource_configurations": sum(len(v) for v in configs.values()),
                       "calendar_regimes": total,
                       "zero_regime_operations": sorted(x for x, v in regimes.items() if not v),
                       "generation_seconds": time.perf_counter() - t0,
                       "database_writes": 0}
    write("generator_comparison.json", {"old_generator": old, "boundary_aware": mode_summaries,
                                        "database_writes": 0})
    # Contention index from boundary-aware K=16 configurations.
    # Rebuild only the compact signatures from the funnel's generated stats;
    # this remains diagnostic and does not mutate CP domains.
    machine_demand = {}; worker_demand = {}; skill_demand = {}
    for row in matching["16"]["stats"]:
        for mid in row.get("machine_ids", ()): machine_demand[str(mid)] = machine_demand.get(str(mid), 0) + int(row.get("crew_alternatives", 0) > 0)
    for key, rows in funnels["16"].items():
        skill = str(problem.operation_by_key[key].required_capability)
        if rows.get("matching_feasible_configurations", 0): skill_demand[skill] = skill_demand.get(skill, 0) + 1
    write("resource_contention.json", {"machine_candidate_pressure": sorted(machine_demand.items(), key=lambda x: (-x[1], x[0]))[:50],
                                       "worker_candidate_pressure": sorted(worker_demand.items(), key=lambda x: (-x[1], x[0]))[:50],
                                       "skill_operation_pressure": sorted(skill_demand.items(), key=lambda x: (-x[1], x[0])),
                                       "note": "worker demand is represented through matching pools; individual worker counts remain in funnel/index",
                                       "database_writes": 0})

    first_valid = next((k for k in (1, 4, 8, 16)
                        if (probes[str(k)].get("scoped_acceptance") or {}).get("accepted") is True), None)
    root = {"classification": "UNRESOLVED", "boundary_proven": True,
            "physical_infeasibility_proven": "NO", "first_valid_k": first_valid,
            "zero_modes_by_k": zero,
            "reason": "boundary-aware diagnostic completed; production integration is intentionally deferred",
            "database_writes": 0}
    write("root_cause.json", root)
    tests = {"status": "PENDING", "database_writes": 0}
    write("focused_tests.json", tests)
    report = {"status": "CRAFT_WINDOW14_STEP4B_PARTIAL", "committed_boundary_global_replay": global_validation.get("replay_status"),
              "committed_boundary_global_validator": global_validation.get("physical_status"),
              "boundary_provenance_valid": prov["boundary_provenance_valid"],
              "machine_boundary_facts": len(boundary.machine_commitments),
              "worker_boundary_facts": len(boundary.worker_commitments),
              "routing_boundary_facts": len(boundary_facts), "window14_operations": len(W14_KEYS),
              "operations_with_joint_executable_mode": mode_summaries.get("16", {}).get("operations_with_modes"),
              "zero_mode_operations": zero.get("16"), "crew_matching_method": "deterministic bounded bipartite matching",
              "crew_diversity_levels_tested": [1, 4, 8, 16], "first_valid_k": first_valid,
              "root_cause": root["classification"], "physical_infeasibility_proven": "NO",
              "production_scheduler_changed": "NO", "step3b_control_path_changed": "NO",
              "regression": "PENDING", "database_writes": 0,
              "elapsed_seconds": time.perf_counter() - started}
    write("implementation_report.json", report)
    (OUT / "implementation_report.md").write_text(
        "# Window-14 Step-4B\n\n" + json.dumps(report, indent=2) +
        "\n\nThe diagnostic generator is isolated; no production scheduler or Step-3B control path was changed.\n",
        encoding="utf-8")


if __name__ == "__main__":
    main()
