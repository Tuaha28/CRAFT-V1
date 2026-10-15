"""Frozen Window-14 route-aware adaptive crew-expansion regression.

The script rebuilds only the accepted Windows 1-13 boundary and the persisted
Window-14 operation set.  It never commits to the database and never relaxes
the canonical CP, replay, or physical-validator semantics.
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
from crafter.repair.boundary_aware_modes import build_boundary_availability_index
from crafter.repair.calendar_regimes import CalendarRegimeCache, generate_calendar_regimes
from crafter.repair.planning import build_planning_state
from crafter.repair.resource_configs import generate_resource_configurations
from crafter.repair.route_aware_crew_expansion import (
    EXPANSION_PROVENANCE,
    RouteAwareCrewExpansionPolicyV1,
    audit_strict_superset,
    configuration_signature,
    diagnose_worker_route_pressure,
    run_route_aware_adaptive_crew_expansion,
    serialize_policy,
)
from crafter.repair.solve import run_canonical_schedule
from crafter.repair.temporal_escalation import TemporalEscalationPolicyV3
from scratch.diagnose_window14_v3 import _scoped_acceptance
from scratch.run_window14_step4b import (
    RUN,
    W14_KEYS,
    context,
    make_boundary,
    quiet,
    rows_and_provenance,
)


OUT = RUN / "window14_step4c"
OUT.mkdir(parents=True, exist_ok=True)


def write(name, value):
    (OUT / name).write_text(
        json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def build_regime_callback(state):
    def build(configurations, start_probes=None):
        cache = CalendarRegimeCache()
        started = time.perf_counter()
        regimes, summary, total, equivalence = quiet(
            generate_calendar_regimes, state, configurations, cache=cache,
            additional_breakpoints_by_operation=start_probes or {})
        return regimes, {
            "resource_configuration_count": sum(len(values) for values in configurations.values()),
            "calendar_regime_count": int(total),
            "zero_regime_operation_keys": sorted(key for key, values in regimes.items() if not values),
            "generation_seconds": time.perf_counter() - started,
            "equivalence": equivalence,
            "cache_profile": cache.snapshot(),
            "per_operation": {
                str(row["operation_key"]): {
                    "resource_configurations": int(row["resource_configurations"]),
                    "regime_count": int(row["regime_count"]),
                    "raw_regime_count": int(row["raw_regime_count"]),
                }
                for row in summary
            },
            "additional_breakpoints_by_operation": {
                str(key): list(values) for key, values in sorted((start_probes or {}).items())
            },
        }
    return build


def solve_callback(state, boundary_facts):
    plan = TemporalEscalationPolicyV3().plans[1]

    def solve(round_index, configurations, regimes, regime_metadata):
        prepared = {
            "state": state,
            "initial_keys": tuple(state["keys"]),
            "required_window_keys": tuple(state["keys"]),
            "closure": {"iterations": []},
            "resource_configurations": configurations,
            "resource_audit": [],
            "resource_summaries": [],
            "regimes": regimes,
            "regime_summary": [],
            "regime_count": regime_metadata["calendar_regime_count"],
            "calendar_equivalence": regime_metadata["equivalence"],
            "calendar_cache_profile": regime_metadata["cache_profile"],
            "model": None,
            "timing": {},
            "boundary_facts": tuple(boundary_facts),
        }
        started = time.perf_counter()
        result = quiet(
            run_canonical_schedule,
            prepared,
            objective_mode=False,
            plan=plan,
            unrestricted_final_stage=True,
        )
        elapsed = time.perf_counter() - started
        acceptance = result.get("acceptance", {}) or {}
        candidate = bool(acceptance.get("selected_schedule"))
        scoped = quiet(
            _scoped_acceptance, result, state, boundary_facts, W14_KEYS
        ) if candidate else None
        accepted = bool(scoped and scoped.get("accepted") is True)
        stages = [
            {key: row.get(key) for key in (
                "stage", "status", "config_limit", "window_days", "time_scope",
                "resource_configurations", "regimes", "variables", "constraints",
                "optional_intervals", "branches", "conflicts",
                "model_build_seconds", "solve_seconds",
            )}
            for row in (acceptance.get("stages", ()) or ())
        ]
        return {
            "round": int(round_index),
            "accepted": accepted,
            "candidate_exists": candidate,
            "canonical_status": acceptance.get("status"),
            "winning_cp_status": (acceptance.get("winning_stage") or {}).get("status"),
            "stages": stages,
            "resource_configuration_count": regime_metadata["resource_configuration_count"],
            "calendar_regime_count": regime_metadata["calendar_regime_count"],
            "zero_regime_operation_keys": regime_metadata["zero_regime_operation_keys"],
            "calendar_generation_seconds": regime_metadata["generation_seconds"],
            "solve_acceptance_seconds": elapsed,
            "selected_schedule": acceptance.get("selected_schedule", ()) if candidate else (),
            "scoped_acceptance": scoped,
            "replay": (scoped or {}).get("replay", {}).get("replay_status") if scoped else "NOT_REACHED",
            "physical_validator": (scoped or {}).get("physical", {}).get("physical_status") if scoped else "NOT_REACHED",
            "database_writes": 0,
        }
    return solve


def selected_config_signature(row):
    config = row["resource_configuration"]
    return (
        str(row["operation_key"]),
        int(config["factory_id"]),
        int(config["line_id"]),
        int(config["k"]),
        tuple(int(value) for value in config["machine_ids"]),
        tuple(sorted(str(value) for value in config["worker_ids"])),
        int(config["setup_seconds"]),
        int(config["productive_seconds"]),
        float(config.get("productive_minutes", 0.0)),
    )


def main():
    experiment_started = time.perf_counter()
    # This directory belongs only to the reproducible Step-4C probe.  Remove
    # stale round records from an earlier bounded attempt so final evidence
    # contains exactly the rounds executed by this run.
    for path in OUT.glob("adaptive_round_*.json"):
        path.unlink()
    loaded = quiet(_load_state)
    _accepted, committed, provenance = rows_and_provenance()
    boundary, boundary_facts = make_boundary(loaded[5], committed)
    state = quiet(
        build_planning_state,
        loaded,
        context(W14_KEYS, "window-014-step4c"),
        boundary=boundary,
    )
    availability = build_boundary_availability_index(
        state, boundary, horizon_start_seconds=0)

    base_started = time.perf_counter()
    base_configs, resource_audit, base_summary = quiet(
        generate_resource_configurations,
        state,
        max_machine_sets=3,
        max_worker_crews=16,
        machine_pool_limit=30,
    )
    base_generation_seconds = time.perf_counter() - base_started
    policy = RouteAwareCrewExpansionPolicyV1()
    build_regimes = build_regime_callback(state)
    base_regimes, base_regime_metadata = build_regimes(base_configs, {})
    pressure = diagnose_worker_route_pressure(
        state, base_configs, base_regimes, resource_audit)

    write("starting_state.json", {
        "factory_id": 2,
        "window_id": "window-014",
        "window_start_seconds": 326400,
        "window_end_seconds": 412800,
        "commit_end_seconds": 346400,
        "window_operation_keys": list(W14_KEYS),
        "window_operation_count": len(W14_KEYS),
        "committed_operation_count": len(committed),
        "committed_operation_keys": sorted(committed),
        "machine_boundary_facts": len(boundary.machine_commitments),
        "worker_boundary_facts": len(boundary.worker_commitments),
        "routing_boundary_facts": len(boundary_facts),
        "source_provenance_count": len(provenance),
        "base_resource_configurations": sum(len(values) for values in base_configs.values()),
        "base_calendar_regimes": base_regime_metadata["calendar_regime_count"],
        "base_generation_seconds": base_generation_seconds,
        "policy": serialize_policy(policy),
        "database_writes": 0,
    })
    write("worker_pressure_diagnosis.json", {
        "diagnostics": list(pressure),
        "database_writes": 0,
    })
    write("route_pressure_analysis.json", {
        "pressure_operations": [row for row in pressure if row["route_pressure"]],
        "first_pressure_operation": next(
            (row["operation_key"] for row in pressure if row["route_pressure"]), None),
        "database_writes": 0,
    })

    result = run_route_aware_adaptive_crew_expansion(
        state,
        availability,
        base_configs,
        resource_audit,
        build_regimes,
        solve_callback(state, boundary_facts),
        policy=policy,
    )

    rounds = result.get("rounds", ())
    for row in rounds:
        write(f"adaptive_round_{int(row['round'])}.json", {**row, "database_writes": 0})
    write("crew_generation_rounds.json", {
        "rounds": [
            {"round": row["round"],
             "operations_expanded": row.get("operations_expanded", ()),
             "crew_generation": row.get("crew_generation", ()),
             "added_crews_by_operation": row.get("added_crews_by_operation", {})}
            for row in rounds
        ],
        "database_writes": 0,
    })
    write("crew_diversity.json", {
        "rounds": [
            {"round": row["round"], "crew_generation": row.get("crew_generation", ())}
            for row in rounds if row.get("crew_generation")
        ],
        "ranking": ["physical_legality", "earlier_execution", "lower_worker_contention", "deterministic_signature"],
        "database_writes": 0,
    })
    write("expansion_closure.json", {
        "rounds": [
            {"round": row["round"], "closure": row.get("closure", ()),
             "operations_expanded": row.get("operations_expanded", ())}
            for row in rounds
        ],
        "database_writes": 0,
    })
    superset = audit_strict_superset(base_configs, result["configurations"])
    write("domain_superset_audit.json", {**superset, "database_writes": 0})

    selected = (result.get("selected_result") or {}).get("selected_schedule", ()) or ()
    base_signatures = {
        configuration_signature(config)
        for values in base_configs.values()
        for config in values
    }
    selected_new = []
    for row in selected:
        signature = selected_config_signature(row)
        if signature not in base_signatures:
            selected_new.append({
                "operation_key": str(row["operation_key"]),
                "configuration_signature": list(signature),
                "worker_ids": list(row["resource_configuration"]["worker_ids"]),
                "machine_ids": list(row["resource_configuration"]["machine_ids"]),
                "start_seconds": row.get("start_seconds"),
                "end_seconds": row.get("end_seconds"),
            })
    write("selected_new_crews.json", {
        "selected_new_crew_count": len(selected_new),
        "selected_new_crews": selected_new,
        "database_writes": 0,
    })

    selected_result = result.get("selected_result") or {}
    accepted = bool(selected_result.get("accepted"))
    if accepted:
        classification = "CRAFT_WINDOW14_STEP4C_VALIDATED"
        followup = "WORKER_CREW_TRUNCATION_RECOVERED_BY_ROUTE_AWARE_EXPANSION"
    else:
        classification = "CRAFT_WINDOW14_STEP4C_PARTIAL"
        followup = str(result.get("status") or "UNRESOLVED")
    write("window14_result.json", {
        "status": classification,
        "adaptive_controller_status": result.get("status"),
        "candidate_exists": bool(selected_result.get("candidate_exists")),
        "accepted": accepted,
        "selected_new_crew_count": len(selected_new),
        "selected_result": selected_result,
        "database_writes": 0,
    })
    scoped = selected_result.get("scoped_acceptance") or {}
    write("replay_validation.json", {
        "status": selected_result.get("replay", "NOT_REACHED"),
        "details": scoped.get("replay"),
        "database_writes": 0,
    })
    write("physical_validation.json", {
        "status": selected_result.get("physical_validator", "NOT_REACHED"),
        "details": scoped.get("physical"),
        "database_writes": 0,
    })
    write("root_cause_followup.json", {
        "starting_root_cause": "WORKER_CREW_TRUNCATION_DEFECT",
        "classification": followup,
        "physical_infeasibility_proven": "NO",
        "production_scheduler_changed": "NO",
        "step3b_control_path_changed": "NO",
        "database_writes": 0,
    })

    report = {
        "status": classification,
        "starting_root_cause": "WORKER_CREW_TRUNCATION_DEFECT",
        "base_domain_configs": sum(len(values) for values in base_configs.values()),
        "base_domain_regimes": base_regime_metadata["calendar_regime_count"],
        "route_pressure_detected": any(row["route_pressure"] for row in pressure),
        "first_pressure_operation": next((row["operation_key"] for row in pressure if row["route_pressure"]), None),
        "expansion_closure": sorted({key for row in rounds for key in row.get("operations_expanded", ())}),
        "rounds": rounds,
        "strict_superset_verified": superset["strict_superset_or_equal"],
        "new_crews_used_by_selected_schedule": len(selected_new),
        "window14_candidate": bool(selected_result.get("candidate_exists")),
        "replay": selected_result.get("replay", "NOT_REACHED"),
        "physical_validator": selected_result.get("physical_validator", "NOT_REACHED"),
        "physical_infeasibility_proven": "NO",
        "production_scheduler_changed": "NO",
        "step3b_control_path_changed": "NO",
        "regression": "PENDING",
        "database_writes": 0,
        "elapsed_seconds": time.perf_counter() - experiment_started,
    }
    write("implementation_report.json", report)
    write("focused_tests.json", {"status": "PENDING", "database_writes": 0})
    write("changed_files_manifest.json", {
        "production_scheduler_changed": False,
        "step3b_control_path_changed": False,
        "database_writes": 0,
        "files_added_or_changed": [
            "crafter/repair/boundary_aware_modes.py",
            "crafter/repair/route_aware_crew_expansion.py",
            "tests/test_route_aware_crew_expansion.py",
            "scratch/run_window14_step4c.py",
        ],
    })


if __name__ == "__main__":
    main()
