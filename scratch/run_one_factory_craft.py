"""Execute the bounded, factory-local rolling CRAFT proof for Factory 3."""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import sys
import time
import traceback
from pathlib import Path
from statistics import median

sys.path.insert(0, ".")

from crafter.disruptions.benchmark import _load_state
from crafter.repair.global_planner import (
    CommittedScheduleState,
    GlobalCRAFTPlanner,
    RollingWindow,
    WindowBoundaryState,
    detect_provisional_hard_freeze,
    factory_future_operation_keys,
)
from crafter.repair.calendar_regimes import CalendarRegimeCache
from crafter.repair.planning import (
    PlanningContextV1,
    build_planning_state,
    prepare_planning_schedule,
    select_precedence_safe_window,
)
from crafter.repair.scoped_validation import (
    validate_assembled_factory,
    validate_planning_window,
)
from crafter.repair.solve import run_canonical_schedule
from crafter.repair.resource_domain_v2 import (
    ResourceDomainPolicyV2,
    run_adaptive_planning_schedule,
)
from crafter.repair.temporal_escalation import (
    TemporalEscalationPolicyV3,
    run_temporal_escalated_planning_schedule,
)
from crafter.repair.resource_domain_v3 import (
    ResourceDomainPolicyV3,
    run_v3_planning_schedule,
)
from crafter.repair.validation_scope import ValidationScopeV1
from crafter.repair.window_phenotype import build_window_phenotype
from crafter.repair.rolling_horizon_v2 import (
    FAST_FORWARD_TO_EVENT,
    HORIZON_EXTENSION_REQUIRED,
    NEIGHBORHOOD_EXPANSION_REQUIRED,
    NO_EXECUTABLE_WORK,
    SOLVE_READY_WORK,
    UNRESOLVED_ROLLING_STATE,
    apply_rolling_orchestration_decision_v2,
    advance_zero_commit_rolling_epoch,
    decide_rolling_orchestration_v2,
)
from crafter.repair.boundary_aware_modes import build_boundary_availability_index
from crafter.repair.calendar_regimes import generate_calendar_regimes
from crafter.repair.route_aware_crew_expansion import (
    RouteAwareCrewExpansionPolicyV1,
    audit_strict_superset,
    run_route_aware_adaptive_crew_expansion,
)
from crafter.repair.fast_mode import OptimizationWindowWatchdog, fast_mode_config
from crafter.production.controller_v1 import ProductionControllerV1, ProductionControllerConfigV1


OUT = Path(os.environ.get("CRAFT_ONE_FACTORY_OUT",
                          "outputs/crafter/global_craft_one_factory_v1"))
OUT.mkdir(parents=True, exist_ok=True)
STARTED = time.perf_counter()


def _load_run_state():
    """Use a hash-pinned, repository-created capture when one is supplied."""
    capture_path = os.environ.get("CRAFT_ONE_FACTORY_CAPTURE_PATH")
    if not capture_path:
        return _load_state()
    expected = os.environ.get("CRAFT_ONE_FACTORY_CAPTURE_SHA256")
    if not expected:
        raise RuntimeError("capture SHA-256 is required")
    content = Path(capture_path).read_bytes()
    if hashlib.sha256(content).hexdigest() != expected:
        raise RuntimeError("frozen benchmark capture SHA-256 mismatch")
    loaded = pickle.loads(content)
    if not isinstance(loaded, tuple) or len(loaded) != 9:
        raise RuntimeError("invalid frozen benchmark capture shape")
    return loaded


def write_json(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def persist_window_record(record):
    """Drop live/internal CP objects from benchmark evidence in Fast mode."""
    if os.environ.get("CRAFT_FAST_EVIDENCE", "0") != "1":
        return record
    keep = {
        "window_id", "factory_id", "snapshot_as_of_seconds", "window_start_seconds",
        "window_end_seconds", "commit_end_seconds", "pre_solve_required_keys",
        "selected_keys", "committed_keys", "provisional_keys", "future_outside_keys",
        "boundary_keys", "admission_provenance", "provisional_operations_carried",
        "provisional_operations_changed", "provisional_operations_unchanged",
        "provisional_hard_frozen", "committed_operations_changed", "cp_solver_status",
        "cp_status", "resource_domain_policy", "craft_complexity", "materialization_mismatches",
        "scope_status", "replay_status", "physical_status", "planning_scope_status",
        "planning_replay_status", "planning_physical_status", "planning_accepted", "accepted",
        "historical_future_assignments_backfilled", "database_writes", "timing", "window_total_seconds",
        "total_seconds", "step3b", "step3b_execution", "adaptive_crew_recovery",
        "production_controller",
    }
    return {key: value for key, value in record.items() if key in keep}


def safe_row(row):
    return json.loads(json.dumps(row, default=str))


def numeric_summary(values):
    values = [int(value) for value in values]
    return {"min": min(values, default=0),
            "median": median(values) if values else 0,
            "max": max(values, default=0)}


def flatten_boundary(boundary):
    if boundary is None:
        return ()
    facts = [dict(row) for row in boundary.machine_commitments]
    facts.extend(dict(row) for row in boundary.worker_commitments)
    for key, route in boundary.routing_completion_state.items():
        facts.append({"operation_key": key, **dict(route),
                      "authoritative_end_seconds": route["authoritative_end_seconds"]})
    return tuple(facts)


def merge_boundaries(committed):
    if not committed.boundaries:
        return None
    machine = []
    worker = []
    crossing = []
    orders = {}
    routing = {}
    for boundary in committed.boundaries:
        machine.extend(boundary.machine_commitments)
        worker.extend(boundary.worker_commitments)
        crossing.extend(boundary.cross_boundary_operation_keys)
        orders.update(boundary.order_completion_state)
        routing.update(boundary.routing_completion_state)
    return WindowBoundaryState(
        int(committed.factory_id), tuple(committed.committed_operation_keys),
        tuple(sorted(set(crossing))), tuple(machine), tuple(worker), orders,
        routing, f"factory-{committed.factory_id}-rolling")


def materialized_rows(prepared):
    rows = list(prepared.get("acceptance", {}).get("selected_schedule", ()))
    state = prepared["state"]
    for row in rows:
        op = state["problem"].operation_by_key[str(row["operation_key"])]
        row["order_id"] = str(op.order_id)
        cfg = row["resource_configuration"]
        row["setup_seconds"] = int(cfg["setup_seconds"])
        row["productive_seconds"] = int(cfg["productive_seconds"])
        row["productive_minutes"] = float(cfg.get("productive_minutes", 0.0))
    return rows


def exact_row_signature(row):
    cfg = row["resource_configuration"]
    return (
        str(row["operation_key"]), str(row.get("order_id", "")), int(cfg["line_id"]), int(cfg["k"]),
        tuple(sorted(int(x) for x in cfg["machine_ids"])),
        tuple(sorted(str(x) for x in cfg["worker_ids"])),
        int(row["start_seconds"]), int(row["end_seconds"]),
        int(row.get("setup_seconds", cfg.get("setup_seconds", 0))),
        int(row.get("productive_seconds", cfg.get("productive_seconds", 0))),
        tuple(tuple(int(v) for v in segment) for segment in row.get("worker_segments", ())),
    )


def _worker_domain_truncated(resource_audit):
    return any(
        bool(row.get("truncated")) and any(
            bool(item.get("more_worker_crews_available"))
            for item in row.get("domain_rows", ())
        )
        for row in (resource_audit or ())
    )


def _attempt_route_aware_crew_recovery(*, state, base, boundary, boundary_facts,
                                       cache, required_operation_keys):
    """Controlled exact recovery after a truncated normal-domain failure.

    Matching only emits legal configurations.  The original domain remains a
    strict subset and every retry uses the canonical CP/replay/validator path.
    """
    acceptance = base.get("acceptance", {}) or {}
    if acceptance.get("selected_schedule") or not _worker_domain_truncated(base.get("resource_audit", ())):
        return None
    base_configurations = base.get("resource_configurations") or {}
    if not base_configurations:
        return None
    availability = build_boundary_availability_index(state, boundary, horizon_start_seconds=0)

    def build_regimes(configurations, start_probes=None):
        regimes, summary, total, equivalence = generate_calendar_regimes(
            state, configurations, cache=cache,
            additional_breakpoints_by_operation=start_probes or {})
        return regimes, {
            "resource_configuration_count": sum(len(values) for values in configurations.values()),
            "calendar_regime_count": int(total),
            "zero_regime_operation_keys": sorted(key for key, values in regimes.items() if not values),
            "equivalence": equivalence,
            "cache_profile": cache.snapshot(),
        }

    def solve_round(round_index, configurations, regimes, metadata):
        prepared = dict(base)
        prepared.update({
            "state": state,
            "initial_keys": tuple(state["keys"]),
            "required_window_keys": tuple(required_operation_keys),
            "resource_configurations": configurations,
            "regimes": regimes,
            "regime_count": int(metadata["calendar_regime_count"]),
            "calendar_equivalence": metadata["equivalence"],
            "boundary_facts": tuple(boundary_facts),
            "adaptive_recovery_attempt": True,
        })
        candidate = run_canonical_schedule(prepared, objective_mode=False)
        candidate_acceptance = candidate.get("acceptance", {}) or {}
        rows = materialized_rows(candidate)
        scoped = {"accepted": False, "scope_status": "NOT_RUN",
                  "replay_status": "NOT_RUN", "physical_status": "NOT_RUN"}
        if rows:
            future = factory_future_operation_keys(state, state["planning_context"]["factory_id"])
            scope = ValidationScopeV1("WINDOW", tuple(required_operation_keys), future)
            phenotype, _ = build_window_phenotype(
                rows, boundary_facts=tuple(boundary_facts),
                future_outside_operation_keys=future, validation_scope=scope)
            scoped = validate_planning_window(state, phenotype)
        return {
            "round": int(round_index),
            "accepted": bool(scoped.get("accepted")) and int(
                (candidate_acceptance.get("materialization") or {}).get("mismatch_count", 0)) == 0,
            "candidate_exists": bool(rows),
            "candidate": candidate,
            "scoped_acceptance": scoped,
            "winning_cp_status": (candidate_acceptance.get("winning_stage") or {}).get("status"),
            "resource_configuration_count": metadata["resource_configuration_count"],
            "calendar_regime_count": metadata["calendar_regime_count"],
            "database_writes": 0,
        }

    controller = run_route_aware_adaptive_crew_expansion(
        state, availability, base_configurations, base.get("resource_audit", ()),
        build_regimes, solve_round, policy=RouteAwareCrewExpansionPolicyV1())
    selected = controller.get("selected_result") or {}
    candidate = selected.get("candidate")
    # ``candidate`` contains a live planning state with tuple-keyed indexes;
    # it must never be embedded in append-only evidence.  Keep it only on the
    # execution path and persist the scalar/auditable recovery trace below.
    trace_rounds = []
    for row in controller.get("rounds", ()):
        public = dict(row)
        solve = dict(public.get("solve") or {})
        solve.pop("candidate", None)
        public["solve"] = solve
        trace_rounds.append(public)
    controller_trace = {key: value for key, value in controller.items()
                        if key not in {"selected_result", "rounds", "configurations", "regimes"}}
    controller_trace["rounds"] = trace_rounds
    if candidate is not None:
        candidate["adaptive_crew_recovery"] = {
            "status": controller.get("status"), "rounds": trace_rounds,
            "domain_superset": audit_strict_superset(base_configurations, controller["configurations"]),
            "database_writes": 0,
        }
        candidate["required_window_keys"] = tuple(required_operation_keys)
        candidate["boundary_facts"] = tuple(boundary_facts)
    return {"controller": controller_trace, "candidate": candidate,
            "accepted": bool(selected.get("accepted")), "database_writes": 0}


def boundary_commitment_signature(boundary, key):
    machine = tuple(sorted((int(x["machine_id"]), int(x["start_seconds"]),
                            int(x["end_seconds"]), int(x.get("line_id", -1)),
                            int(x.get("k", -1)), int(x.get("setup_seconds", -1)),
                            int(x.get("productive_seconds", -1)))
                           for x in boundary.machine_commitments
                           if str(x["operation_key"]) == str(key)))
    worker = tuple(sorted((str(x["worker_id"]), int(x["start_seconds"]),
                           int(x["end_seconds"]))
                          for x in boundary.worker_commitments
                          if str(x["operation_key"]) == str(key)))
    route = boundary.routing_completion_state.get(str(key), {})
    route_signature = (
        int(route.get("authoritative_end_seconds", -1)),
        str(route.get("production_order_id", "")),
        int(route.get("line_id", -1)), int(route.get("k", -1)),
        int(route.get("setup_seconds", -1)), int(route.get("productive_seconds", -1)),
        tuple(sorted(int(x) for x in route.get("machine_ids", ()))),
        tuple(sorted(str(x) for x in route.get("worker_ids", ()))),
    )
    return machine, worker, route_signature


def committed_integrity(rows, boundary):
    changed = []
    for row in rows:
        key = str(row["operation_key"])
        cfg = row["resource_configuration"]
        machine, worker, route = boundary_commitment_signature(boundary, key)
        setup = int(row.get("setup_seconds", cfg.get("setup_seconds", 0)))
        productive = int(row.get("productive_seconds", cfg.get("productive_seconds", 0)))
        expected_machine = tuple(sorted((int(mid), int(row["start_seconds"]),
                                         int(row["end_seconds"]), int(cfg["line_id"]),
                                         int(cfg["k"]), setup, productive)
                                        for mid in cfg["machine_ids"]))
        expected_worker = tuple(sorted((str(wid), int(left), int(right))
                                       for wid in cfg["worker_ids"]
                                       for left, right in row.get("worker_segments", ())))
        expected_route = (
            int(row["end_seconds"]), str(row.get("order_id", "")), int(cfg["line_id"]),
            int(cfg["k"]), setup, productive,
            tuple(sorted(int(x) for x in cfg["machine_ids"])),
            tuple(sorted(str(x) for x in cfg["worker_ids"])),
        )
        if machine != expected_machine or worker != expected_worker or route != expected_route:
            changed.append(key)
    return tuple(sorted(set(changed)))


def commitment_evidence(row, boundary):
    """Persist the exact accepted row and the exact carried boundary facts."""
    key = str(row["operation_key"]); cfg = row["resource_configuration"]
    route = boundary.routing_completion_state.get(key, {})
    return {
        "operation_key": key,
        "order_id": str(row.get("order_id", "")),
        "production_order_id": str(route.get("production_order_id", row.get("order_id", ""))),
        "factory_id": int(cfg["factory_id"]), "line_id": int(cfg["line_id"]),
        "k": int(cfg["k"]), "machine_ids": list(cfg["machine_ids"]),
        "worker_ids": list(cfg["worker_ids"]),
        "start_seconds": int(row["start_seconds"]), "end_seconds": int(row["end_seconds"]),
        "setup_seconds": int(row.get("setup_seconds", cfg.get("setup_seconds", 0))),
        "productive_seconds": int(row.get("productive_seconds", cfg.get("productive_seconds", 0))),
        "worker_segments": [list(map(int, pair)) for pair in row.get("worker_segments", ())],
        "machine_occupancy": [
            {"machine_id": int(m), "start_seconds": int(row["start_seconds"]),
             "end_seconds": int(row["end_seconds"])} for m in cfg["machine_ids"]
        ],
        "routing_completion": dict(route),
        "boundary_signature_matches": key not in committed_integrity([row], boundary),
    }


def classify_factory_operations(problem, factory_id):
    return tuple(sorted(str(op.operation_key) for op in problem.operations
                        if int(problem.order_by_id[op.order_id].factory_id) == int(factory_id)))


def run():
    load_started = time.perf_counter()
    loaded = _load_run_state()
    state_load_seconds = time.perf_counter() - load_started
    problem, operator_index, rows, _durations, _details, route_index, _baseline, _checked, source = loaded
    factory_id = int(os.environ.get("CRAFT_ONE_FACTORY_ID", "3"))
    factory_keys = classify_factory_operations(problem, factory_id)
    factory_set = set(factory_keys)
    snapshot_as_of = 0
    horizon = int(problem.horizon_seconds)
    commit_seconds = 20_000
    lookahead_seconds = 86_400
    normal_cap = int(os.environ.get("CRAFT_ONE_FACTORY_NORMAL_CAP", "6"))
    max_machine_sets = int(os.environ.get("CRAFT_ONE_FACTORY_MAX_MACHINE_SETS", "1"))
    max_worker_crews = int(os.environ.get("CRAFT_ONE_FACTORY_MAX_WORKER_CREWS", "1"))
    adaptive_resource_domain = os.environ.get("CRAFT_ONE_FACTORY_RESOURCE_DOMAIN_V2", "0") == "1"
    temporal_escalation_v3 = os.environ.get("CRAFT_ONE_FACTORY_TEMPORAL_V3", "0") == "1"
    resource_domain_v3 = os.environ.get("CRAFT_ONE_FACTORY_RESOURCE_DOMAIN_V3", "0") == "1"
    rolling_horizon_v2_authoritative = os.environ.get("CRAFT_ONE_FACTORY_STEP4D", "0") == "1"
    adaptive_crew_recovery_enabled = os.environ.get("CRAFT_ONE_FACTORY_ADAPTIVE_CREW_RECOVERY", "0") == "1"
    production_v1 = os.environ.get("CRAFT_PRODUCTION_V1", "0") == "1"
    max_cap = len(factory_keys)

    source_type = ("FROZEN_SNAPSHOT_CAPTURE" if os.environ.get(
        "CRAFT_ONE_FACTORY_CAPTURE_PATH") else "FROZEN_OPERATIONS_DB_BACKED_RESOURCES")
    accounting = {
        "factory_id": factory_id,
        "source_type": source_type,
        "capture_sha256": os.environ.get("CRAFT_ONE_FACTORY_CAPTURE_SHA256"),
        "snapshot_as_of_seconds": snapshot_as_of,
        "total_factory_operations": len(factory_keys),
        "completed_operations": [],
        "in_progress_operations": [],
        "explicit_exclusions": [],
        "schedulable_operations": list(factory_keys),
        "operations_requiring_scheduling": list(factory_keys),
        "accounting_identity": {
            "universe": len(factory_keys), "completed": 0, "in_progress": 0,
            "excluded": 0, "schedulable": len(factory_keys),
        },
        "resource_configuration_policy": {
            "max_machine_sets": max_machine_sets,
            "max_worker_crews": max_worker_crews,
            "adaptive_resource_domain_v2": adaptive_resource_domain,
        },
        "database_writes": 0,
    }
    write_json("factory3_accounting.json" if factory_id == 3 else "factory_accounting.json", accounting)

    cache = CalendarRegimeCache()
    committed = CommittedScheduleState(factory_id)
    assembled = {}
    prior_provisional = {}
    progress = []
    windows = []
    commitment_records = []
    provisional_records = []
    current_start = 0
    window_number = 1
    no_progress_signatures = set()
    rolling_started = time.perf_counter()

    def make_context(window_id, mode, start, end, commit_end, selected):
        return PlanningContextV1(
            window_id, mode, factory_id, start, start, end, commit_end,
            tuple(selected), source_type,
            previous_provisional_plan_reference=("provisional-" + str(window_number - 1)
                                                 if prior_provisional else None),
            snapshot_as_of_seconds=snapshot_as_of)

    while set(assembled) != factory_set:
        remaining = tuple(sorted(factory_set - set(assembled)))
        if not remaining:
            break
        end = min(horizon, current_start + lookahead_seconds)
        if end <= current_start:
            end = horizon
        cap = normal_cap
        admission = ()
        selected = ()
        probe_boundary = merge_boundaries(committed)
        orchestration = None
        orchestration_execution = None
        while True:
            probe_context = make_context(f"window-{window_number:03d}",
                                         "INITIAL_PLAN" if window_number == 1 else "ROLLING_PLAN",
                                         current_start, end,
                                         min(end, current_start + commit_seconds), ())
            probe_state = build_planning_state(loaded, probe_context, boundary=probe_boundary)
            selected, admission = select_precedence_safe_window(
                problem, probe_state["after_index"], probe_state["classified"], probe_context,
                committed_operation_keys=committed.committed_operation_keys,
                boundary=probe_boundary,
                maximum_operations=min(cap, len(remaining)))
            selected = tuple(key for key in selected if key in factory_set and key not in assembled)
            if rolling_horizon_v2_authoritative:
                orchestration = decide_rolling_orchestration_v2(
                    window_id=f"window-{window_number:03d}", context=probe_context,
                    problem=problem, route_index=probe_state["after_index"],
                    classified=probe_state["classified"], admitted_operation_keys=selected,
                    admission_provenance=admission, remaining_operation_keys=remaining,
                    committed_operation_keys=committed.committed_operation_keys,
                    boundary=probe_boundary, maximum_operations=min(cap, len(remaining)),
                    previous_signatures=no_progress_signatures)
                orchestration_execution = apply_rolling_orchestration_decision_v2(
                    orchestration, normal_operation_cap=normal_cap,
                    maximum_operation_cap=max_cap, commit_step_seconds=commit_seconds)
                action = orchestration_execution["action"]
                progress_signature = (
                    tuple(remaining), tuple(committed.committed_operation_keys),
                    int(current_start), int(end), str(action),
                    repr(orchestration.as_dict().get("provenance", {}).get("readiness_scan", {})),
                )
                if progress_signature in no_progress_signatures:
                    failure = {"status": "GLOBAL_CRAFT_ONE_FACTORY_INCOMPLETE",
                               "first_blocker": "NO_PROGRESS_LOOP_DETECTED",
                               "window_number": window_number, "step3b": orchestration.as_dict(),
                               "database_writes": 0}
                    write_json("rolling_progress.json", {"status": failure["status"], "windows": progress,
                                                           "failure": failure})
                    return failure
                no_progress_signatures.add(progress_signature)
                if action == FAST_FORWARD_TO_EVENT:
                    record = {
                        "window_id": f"window-{window_number:03d}", "factory_id": factory_id,
                        "window_start_seconds": current_start, "window_end_seconds": end,
                        "commit_end_seconds": min(end, current_start + commit_seconds),
                        "step3b": orchestration.as_dict(), "step3b_execution": orchestration_execution,
                        "accepted": False, "optimization_fast_forward": True,
                        "next_optimization_start_seconds": orchestration_execution["next_window_start_seconds"],
                        "remaining_before": list(remaining), "remaining_after": list(remaining),
                        "database_writes": 0,
                    }
                    progress.append(record); windows.append(record)
                    write_json(f"window_{window_number:03d}.json", record)
                    current_start = int(orchestration_execution["next_window_start_seconds"])
                    window_number += 1
                    break
                if action in {NO_EXECUTABLE_WORK, UNRESOLVED_ROLLING_STATE}:
                    failure = {"status": "GLOBAL_CRAFT_ONE_FACTORY_INCOMPLETE",
                               "first_blocker": action, "window_number": window_number,
                               "step3b": orchestration.as_dict(), "database_writes": 0}
                    write_json("rolling_progress.json", {"status": failure["status"], "windows": progress,
                                                           "failure": failure})
                    return failure
                if action == HORIZON_EXTENSION_REQUIRED:
                    end = min(horizon, int(orchestration_execution["window_end_seconds"]))
                    selected = tuple(orchestration_execution.get("selected_operation_keys", selected))
                    break
                if action == NEIGHBORHOOD_EXPANSION_REQUIRED:
                    next_cap = int(orchestration_execution.get("requested_operation_cap", cap))
                    if next_cap > cap:
                        cap = next_cap
                        continue
                    failure = {"status": "GLOBAL_CRAFT_ONE_FACTORY_INCOMPLETE",
                               "first_blocker": "NEIGHBORHOOD_EXPANSION_REQUIRED",
                               "window_number": window_number, "step3b": orchestration.as_dict(),
                               "database_writes": 0}
                    write_json("rolling_progress.json", {"status": failure["status"], "windows": progress,
                                                           "failure": failure})
                    return failure
                if action == SOLVE_READY_WORK:
                    selected = tuple(orchestration_execution.get("selected_operation_keys", selected))
            if selected or cap >= max_cap:
                break
            cap = min(max_cap, max(cap + 1, cap * 2))
        if orchestration_execution and orchestration_execution.get("action") == FAST_FORWARD_TO_EVENT:
            continue
        if not selected:
            failure = {"status": "GLOBAL_CRAFT_ONE_FACTORY_INCOMPLETE",
                       "first_blocker": "PRECEDENCE_CLOSURE_REQUIRES_ESCALATION",
                       "window_number": window_number, "remaining": list(remaining),
                       "admission_provenance": admission, "database_writes": 0}
            write_json("rolling_progress.json", {"status": failure["status"], "windows": progress,
                                                   "failure": failure})
            return failure

        final_window = set(selected) == set(remaining)
        commit_end = end if final_window else min(end, current_start + commit_seconds)
        if commit_end <= current_start:
            commit_end = end
        window = RollingWindow(factory_id, current_start, end, commit_end,
                               f"window-{window_number:03d}")
        mode = "INITIAL_PLAN" if window_number == 1 else "ROLLING_PLAN"
        context = make_context(window.window_id, mode, current_start, end, commit_end, selected)
        window_started = time.perf_counter()
        window_watchdog = (OptimizationWindowWatchdog.start()
                           if fast_mode_config().enabled else None)
        stage_times = {}
        mark = time.perf_counter()
        state = build_planning_state(loaded, context, boundary=probe_boundary)
        if window_watchdog is not None:
            # The state is in-memory planning metadata only; it is not a
            # physical execution fact and is never persisted into boundaries.
            state["_craft_window_deadline"] = window_watchdog.deadline
        stage_times["build_planning_state_seconds"] = time.perf_counter() - mark
        mark = time.perf_counter()
        cache_before = cache.snapshot()
        prepared = prepare_planning_schedule(
            state, max_machine_sets=max_machine_sets, max_worker_crews=max_worker_crews, cache=cache)
        prepare_wall = time.perf_counter() - mark
        prep_timing = prepared.get("timing", {})
        stage_times["resource_configuration_seconds"] = float(
            prep_timing.get("resource_configuration_seconds", 0.0))
        stage_times["calendar_regime_generation_seconds"] = float(
            prep_timing.get("calendar_regime_generation_seconds", 0.0))
        stage_times["other_preparation_seconds"] = float(
            prep_timing.get("other_preparation_seconds", max(0.0, prepare_wall -
                                                               stage_times["resource_configuration_seconds"] -
                                                               stage_times["calendar_regime_generation_seconds"])))
        stage_times["total_prepare_seconds"] = float(
            prep_timing.get("prepare_total_seconds", prepare_wall))
        stage_times["prepare_seconds"] = prepare_wall
        cache_after = cache.snapshot()
        stage_times["cache_delta"] = {
            key: cache_after.get(key, 0) - cache_before.get(key, 0)
            for key in cache_after
            if isinstance(cache_after.get(key), (int, float))
        }
        prepared["required_window_keys"] = tuple(selected)
        prepared["boundary_facts"] = flatten_boundary(probe_boundary)
        # Keep the exact boundary facts on the canonical planning state as
        # well as on the runner envelope.  The production controller's
        # scoped acceptance seam uses the same facts that built the carried
        # reservation view; this prevents validation from silently dropping
        # committed machine/worker/routing boundaries.
        state["boundary_facts"] = prepared["boundary_facts"]
        prepared["admission_provenance"] = admission
        prepared["provisional_reference"] = {k: safe_row(v) for k, v in prior_provisional.items()}

        if window_watchdog is not None and window_watchdog.expired():
            failure = {"status": "GLOBAL_CRAFT_ONE_FACTORY_INCOMPLETE",
                       "first_blocker": "WINDOW_RUNTIME_BUDGET_EXCEEDED",
                       "window_id": window.window_id,
                       "stage": "before_canonical_solve",
                       "window_safety_ceiling_seconds": window_watchdog.ceiling_seconds,
                       "database_writes": 0}
            write_json(f"window_{window_number:03d}.json", failure)
            write_json("rolling_progress.json", {"status": failure["status"],
                                                   "windows": progress, "failure": failure})
            return failure

        mark = time.perf_counter()
        production_trace = None
        if production_v1:
            production = ProductionControllerV1(ProductionControllerConfigV1(
                normal_seconds=float(os.environ.get("CRAFT_PRODUCTION_NORMAL_SECONDS", "45")),
                temporal_retry_seconds=float(os.environ.get("CRAFT_PRODUCTION_TEMPORAL_SECONDS", "45")),
                worker_retry_seconds=float(os.environ.get("CRAFT_PRODUCTION_WORKER_SECONDS", "60")),
                window_ceiling_seconds=float(os.environ.get("CRAFT_PRODUCTION_WINDOW_CEILING_SECONDS", "120"))))
            production_result = production.run_window(
                prepared, boundary=probe_boundary, cache=cache,
                boundary_facts=prepared.get("boundary_facts", ()),
                required_operation_keys=selected)
            canonical = production_result["candidate"]
            production_trace = production_result.get("trace")
            adaptive_resource_domain = False
        elif resource_domain_v3:
            canonical = run_v3_planning_schedule(
                state, cache=cache,
                policy=ResourceDomainPolicyV3(
                    initial_machine_sets=max_machine_sets,
                    initial_worker_crews=max_worker_crews),
                temporal_policy=TemporalEscalationPolicyV3(),
                objective_mode=False)
            adaptive_resource_domain = True
        elif temporal_escalation_v3:
            canonical = run_temporal_escalated_planning_schedule(
                state, cache=cache,
                resource_domain_policy=ResourceDomainPolicyV2(
                    initial_machine_sets=max_machine_sets,
                    initial_worker_crews=max_worker_crews),
                temporal_policy=TemporalEscalationPolicyV3(),
                objective_mode=False)
            adaptive_resource_domain = True
        elif adaptive_resource_domain:
            canonical = run_adaptive_planning_schedule(
                state, cache=cache,
                policy=ResourceDomainPolicyV2(
                    initial_machine_sets=max_machine_sets,
                    initial_worker_crews=max_worker_crews),
                objective_mode=False)
            # Preserve the current runner's exact admission/boundary context
            # on the final adaptive preparation result.
            canonical["required_window_keys"] = tuple(selected)
            canonical["boundary_facts"] = prepared.get("boundary_facts", ())
            canonical["admission_provenance"] = admission
            canonical["provisional_reference"] = prepared.get("provisional_reference", {})
            prepared = canonical
        else:
            canonical = run_canonical_schedule(prepared, objective_mode=False)
        # Keep exact rolling scope on every solver result.  V3 otherwise
        # rebuilds its own preparation dictionary and omits runner metadata.
        canonical["required_window_keys"] = tuple(selected)
        canonical["boundary_facts"] = prepared.get("boundary_facts", ())
        canonical["admission_provenance"] = admission
        canonical["provisional_reference"] = prepared.get("provisional_reference", {})
        adaptive_recovery = None
        if (adaptive_crew_recovery_enabled and
                (window_watchdog is None or not window_watchdog.expired())):
            adaptive_recovery = _attempt_route_aware_crew_recovery(
                state=state, base=canonical, boundary=probe_boundary,
                boundary_facts=prepared.get("boundary_facts", ()), cache=cache,
                required_operation_keys=selected)
            if adaptive_recovery and adaptive_recovery.get("accepted"):
                canonical = adaptive_recovery["candidate"]
                prepared = canonical
        elif adaptive_crew_recovery_enabled and window_watchdog is not None and window_watchdog.expired():
            # Do not begin an expensive recovery round after the outer window
            # budget has elapsed.  A valid normal incumbent is still allowed
            # through the ordinary replay/validator path below.
            adaptive_recovery = {"status": "WINDOW_RUNTIME_BUDGET_EXCEEDED",
                                 "database_writes": 0}
        stage_times["canonical_seconds"] = time.perf_counter() - mark
        acceptance = canonical.get("acceptance", {})
        cp_stages = list(acceptance.get("stages", ()) or ())
        winning_stage = acceptance.get("winning_stage") or {}
        cp_stage_attempt_count = len(cp_stages)
        total_cp_model_build_seconds_all_stages = sum(
            float(stage.get("cp_model_build_seconds", stage.get("model_build_seconds", 0.0)))
            for stage in cp_stages)
        total_cp_solve_seconds_all_stages = sum(
            float(stage.get("cp_solve_seconds", stage.get("solve_seconds", 0.0)))
            for stage in cp_stages)
        total_cp_branches_all_stages = sum(int(stage.get("branches", 0)) for stage in cp_stages)
        total_cp_conflicts_all_stages = sum(int(stage.get("conflicts", 0)) for stage in cp_stages)
        rows_out = materialized_rows(canonical)
        if window_watchdog is not None and window_watchdog.expired() and not rows_out:
            failure = {"status": "GLOBAL_CRAFT_ONE_FACTORY_INCOMPLETE",
                       "first_blocker": "WINDOW_RUNTIME_BUDGET_EXCEEDED",
                       "window_id": window.window_id,
                       "stage": "post_canonical_no_incumbent",
                       "canonical_acceptance": acceptance,
                       "database_writes": 0}
            write_json(f"window_{window_number:03d}.json", failure)
            write_json("rolling_progress.json", {"status": failure["status"],
                                                   "windows": progress, "failure": failure})
            return failure
        stage_times["materialization_seconds"] = float(acceptance.get("timing", {}).get("materialization_seconds", 0.0))
        future = factory_future_operation_keys(state, factory_id)
        scope = ValidationScopeV1("WINDOW", tuple(selected), future)
        phenotype, _ = build_window_phenotype(
            rows_out, boundary_facts=prepared.get("boundary_facts", ()),
            future_outside_operation_keys=future, validation_scope=scope)
        mark = time.perf_counter()
        window_acceptance = validate_planning_window(state, phenotype)
        acceptance_timing = window_acceptance.get("timing", {})
        stage_times["replay_seconds"] = float(
            acceptance_timing.get("replay_seconds", 0.0))
        stage_times["physical_validation_seconds"] = float(
            acceptance_timing.get("physical_validation_seconds", 0.0))
        stage_times["replay_validation_seconds"] = float(
            acceptance_timing.get("total_seconds", time.perf_counter() - mark))
        if (not window_acceptance.get("accepted")
                or int(acceptance.get("materialization", {}).get("mismatch_count", 0)) != 0):
            failure = {"status": "GLOBAL_CRAFT_ONE_FACTORY_INCOMPLETE",
                       "first_blocker": "WINDOW_ACCEPTANCE_FAILED",
                       "window_id": window.window_id, "window_acceptance": window_acceptance,
                       "canonical_acceptance": acceptance,
                       "resource_domain_v2": canonical.get("resource_domain_v2"),
                       "temporal_escalation": canonical.get("temporal_escalation"),
                       "resource_domain_v3": canonical.get("resource_domain_v3"),
                       "adaptive_crew_recovery": adaptive_recovery,
                       "production_controller": production_trace,
                       "database_writes": 0}
            write_json(f"window_{window_number:03d}.json", failure)
            write_json("rolling_progress.json", {"status": failure["status"], "windows": progress,
                                                   "failure": failure})
            return failure

        fake = {"acceptance": {"status": "ACCEPTED", "selected_schedule": rows_out},
                "state": state}
        boundary = GlobalCRAFTPlanner.boundary_state_from_accepted_repair(
            fake, factory_id=factory_id, commit_end_seconds=commit_end,
            source_repair_id=window.window_id)
        committed_keys = set(boundary.committed_operation_keys)
        if not committed_keys:
            # A bounded escalation is required rather than a zero-progress
            # window.  Retry this accepted legal solve's commit edge at the
            # end of the lookahead; no schedule rows are modified.
            commit_end = end
            boundary = GlobalCRAFTPlanner.boundary_state_from_accepted_repair(
                fake, factory_id=factory_id, commit_end_seconds=commit_end,
                source_repair_id=window.window_id + "-escalated")
            committed_keys = set(boundary.committed_operation_keys)
        if not committed_keys:
            # A valid provisional plan may start entirely after the current
            # commit edge.  The old terminal guard treated that as a zero-
            # progress failure even though the rolling epoch can advance
            # deterministically and keep the provisional rows movable.
            zero_provisional = {
                str(row["operation_key"]): safe_row(row) for row in rows_out
            }
            transition = advance_zero_commit_rolling_epoch(
                window_start_seconds=current_start,
                commit_end_seconds=commit_end,
                provisional_operation_keys=zero_provisional,
            )
            if transition.get("action") == FAST_FORWARD_TO_EVENT:
                transition_record = {
                    "window_id": window.window_id,
                    "factory_id": factory_id,
                    "snapshot_as_of_seconds": snapshot_as_of,
                    "window_start_seconds": current_start,
                    "window_end_seconds": end,
                    "commit_end_seconds": commit_end,
                    "selected_keys": [str(row["operation_key"]) for row in rows_out],
                    "committed_keys": [],
                    "provisional_keys": sorted(zero_provisional),
                    "step3b": orchestration.as_dict() if orchestration else None,
                    "step3b_execution": transition,
                    "accepted": False,
                    "zero_commit_epoch_advance": True,
                    "database_writes": 0,
                    "timing": stage_times,
                    "window_total_seconds": time.perf_counter() - window_started,
                }
                persisted_transition = persist_window_record(transition_record)
                write_json(f"window_{window_number:03d}.json", persisted_transition)
                progress.append(persisted_transition)
                write_json("rolling_progress.json", {
                    "status": "RUNNING", "windows": progress,
                    "cumulative_committed": sorted(assembled),
                    "remaining": sorted(factory_set - set(assembled)),
                    "database_writes": 0,
                })
                prior_provisional = zero_provisional
                current_start = int(transition["next_window_start_seconds"])
                window_number += 1
                continue
            failure = {"status": "GLOBAL_CRAFT_ONE_FACTORY_INCOMPLETE",
                       "first_blocker": "ZERO_PROGRESS_WINDOW",
                       "window_id": window.window_id, "remaining": list(remaining),
                       "transition": transition, "database_writes": 0}
            write_json(f"window_{window_number:03d}.json", failure)
            write_json("rolling_progress.json", {"status": failure["status"], "windows": progress,
                                                   "failure": failure})
            return failure
        if committed_keys & set(assembled):
            failure = {"status": "GLOBAL_CRAFT_ONE_FACTORY_INCOMPLETE",
                       "first_blocker": "DUPLICATE_COMMIT",
                       "duplicate_keys": sorted(committed_keys & set(assembled)),
                       "database_writes": 0}
            write_json(f"window_{window_number:03d}.json", failure)
            return failure

        committed_rows = [row for row in rows_out if str(row["operation_key"]) in committed_keys]
        # Verify all already-committed exact fields against the boundary that
        # is injected into this window before accepting the next solve.
        changes = (committed_integrity(list(assembled.values()), probe_boundary)
                   if probe_boundary else ())
        for row in committed_rows:
            assembled[str(row["operation_key"])] = safe_row(row)
            commitment_records.append(commitment_evidence(row, boundary))
        next_provisional = {str(row["operation_key"]): safe_row(row)
                            for row in rows_out if str(row["operation_key"]) not in committed_keys}
        carried = set(prior_provisional) & set(next_provisional)
        changed = sorted(k for k in carried
                         if exact_row_signature(prior_provisional[k]) != exact_row_signature(next_provisional[k]))
        unchanged = sorted(carried - set(changed))
        # Detect hard freezing from the *incoming* boundary captured before
        # this solve.  A provisional row becoming a new commitment in the
        # outgoing boundary is legitimate; it cannot prove that the row was
        # hard before this solve.
        incoming_hard = set(probe_boundary.committed_operation_keys) if probe_boundary else set()
        if probe_boundary:
            incoming_hard.update(str(row.get("operation_key"))
                                 for row in probe_boundary.machine_commitments)
            incoming_hard.update(str(row.get("operation_key"))
                                 for row in probe_boundary.worker_commitments)
            incoming_hard.update(str(key) for key in probe_boundary.routing_completion_state)
        hard_frozen = list(detect_provisional_hard_freeze(
            prior_provisional_keys=prior_provisional,
            incoming_hard_operation_keys=incoming_hard,
            previously_committed_keys=committed.committed_operation_keys,
            current_new_commitment_keys=committed_keys))
        provisional_records.append({
            "window_id": window.window_id,
            "carried": sorted(carried),
            "changed": changed,
            "unchanged": unchanged,
            "hard_frozen": hard_frozen,
            "provisional_hard_frozen": len(hard_frozen),
        })
        record = {
            "window_id": window.window_id, "factory_id": factory_id,
            "snapshot_as_of_seconds": snapshot_as_of,
            "window_start_seconds": current_start, "window_end_seconds": end,
            "commit_end_seconds": commit_end,
            "pre_solve_required_keys": list(selected),
            "selected_keys": [str(row["operation_key"]) for row in rows_out],
            "committed_keys": sorted(committed_keys),
            "provisional_keys": sorted(next_provisional),
            "future_outside_keys": list(future),
            "boundary_keys": list(probe_boundary.committed_operation_keys) if probe_boundary else [],
            "admission_provenance": list(admission),
            "step3b": orchestration.as_dict() if orchestration else None,
            "step3b_execution": orchestration_execution,
            "provisional_reference": sorted(prior_provisional),
            "provisional_operations_carried": len(carried),
            "provisional_operations_changed": len(changed),
            "provisional_operations_unchanged": len(unchanged),
            "provisional_hard_frozen": len(hard_frozen),
            "committed_operations_changed": len(changes),
            "cp_solver_status": (acceptance.get("winning_stage") or {}).get("status", "UNKNOWN"),
            "canonical_legacy_acceptance_status": acceptance.get("status"),
            "cp_status": (acceptance.get("winning_stage") or {}).get("status", "UNKNOWN"),
            "resource_domain_policy": {
                "initial_machine_sets": max_machine_sets,
                "initial_worker_crews": max_worker_crews,
                "adaptive_resource_domain_v2": adaptive_resource_domain,
                "temporal_escalation_v3": temporal_escalation_v3,
                "resource_domain_v3": resource_domain_v3,
            },
            "resource_domain_v2": canonical.get("resource_domain_v2")
                if adaptive_resource_domain else None,
            "temporal_escalation": canonical.get("temporal_escalation"),
            "resource_domain_v3": canonical.get("resource_domain_v3"),
            "adaptive_crew_recovery": adaptive_recovery.get("controller") if adaptive_recovery else None,
            "production_controller": production_trace,
            "canonical_cp_stage": acceptance.get("winning_stage"),
            "cp_stage_attempt_count": cp_stage_attempt_count,
            "cp_stages": cp_stages,
            "total_cp_model_build_seconds_all_stages": total_cp_model_build_seconds_all_stages,
            "total_cp_solve_seconds_all_stages": total_cp_solve_seconds_all_stages,
            "total_cp_branches_all_stages": total_cp_branches_all_stages,
            "total_cp_conflicts_all_stages": total_cp_conflicts_all_stages,
            "winning_stage": winning_stage.get("stage"),
            "winning_stage_variables": winning_stage.get("variables"),
            "winning_stage_constraints": winning_stage.get("constraints"),
            "winning_stage_optional_intervals": winning_stage.get("optional_intervals"),
            "craft_complexity": {
                "required_operation_count": len(selected),
                "selected_operation_count": len(rows_out),
                "committed_count": len(committed_keys),
                "provisional_count": len(next_provisional),
                "future_outside_count": len(future),
                "resource_configuration_count": sum(len(values) for values in prepared["resource_configurations"].values()),
                "configurations_per_operation": numeric_summary(
                    [len(values) for values in prepared["resource_configurations"].values()]),
                "calendar_regime_count": int(prepared.get("regime_count", 0)),
                "regimes_per_operation": numeric_summary(
                    [len(values) for values in prepared["regimes"].values()]),
                "unique_machines": sorted({int(machine) for values in prepared["resource_configurations"].values() for cfg in values for machine in cfg.machine_ids}),
                "unique_workers": sorted({str(worker) for values in prepared["resource_configurations"].values() for cfg in values for worker in cfg.worker_ids}),
                "unique_lines": sorted({int(cfg.line_id) for values in prepared["resource_configurations"].values() for cfg in values}),
                "carried_machine_boundary_facts": len(probe_boundary.machine_commitments) if probe_boundary else 0,
                "carried_worker_boundary_facts": len(probe_boundary.worker_commitments) if probe_boundary else 0,
                "carried_routing_boundary_facts": len(probe_boundary.routing_completion_state) if probe_boundary else 0,
            },
            "materialization_mismatches": acceptance.get("materialization", {}).get("mismatch_count", 0),
            "scope_status": window_acceptance.get("scope_status"),
            "replay_status": window_acceptance.get("replay_status"),
            "physical_status": window_acceptance.get("physical_status"),
            "planning_scope_status": window_acceptance.get("scope_status"),
            "planning_replay_status": window_acceptance.get("replay_status"),
            "planning_physical_status": window_acceptance.get("physical_status"),
            "planning_accepted": bool(window_acceptance.get("accepted")),
            "accepted": True, "historical_future_assignments_backfilled": 0,
            "database_writes": 0,
            "timing": stage_times,
            "window_total_seconds": time.perf_counter() - window_started,
            "total_seconds": time.perf_counter() - STARTED,
        }
        persisted_record = persist_window_record(record)
        write_json(f"window_{window_number:03d}.json", persisted_record)
        windows.append(persisted_record)
        progress.append({**persisted_record,
                         "cumulative_committed_count": len(assembled),
                         "remaining_unscheduled_count": len(factory_set - set(assembled)),
                         "required_count": len(selected), "selected_count": len(rows_out),
                         "committed_count": len(committed_keys),
                         "provisional_count": len(next_provisional)})
        write_json("rolling_progress.json", {"status": "RUNNING", "windows": progress,
                                               "cumulative_committed": sorted(assembled),
                                               "remaining": sorted(factory_set - set(assembled)),
                                               "database_writes": 0})
        committed = committed.append(boundary)
        prior_provisional = next_provisional
        current_start = commit_end
        window_number += 1
        max_windows = int(os.environ.get("CRAFT_ONE_FACTORY_MAX_WINDOWS", "0"))
        if max_windows > 0 and len(windows) >= max_windows:
            limited = {
                "status": "CRAFT_WINDOW_LIMIT_REACHED",
                "windows": len(windows),
                "scheduled": len(assembled),
                "remaining_unscheduled": sorted(factory_set - set(assembled)),
                "factory_id": factory_id,
                "database_writes": 0,
            }
            write_json("rolling_progress.json", {
                "status": limited["status"], "windows": progress,
                "cumulative_committed": sorted(assembled),
                "remaining": limited["remaining_unscheduled"],
                "database_writes": 0,
            })
            return limited
        if window_number > len(factory_keys) + 2:
            raise RuntimeError("rolling planner exceeded bounded progress guard")

    rolling_execution_seconds = time.perf_counter() - rolling_started
    assembled_validation_started = time.perf_counter()
    # Assemble and validate every schedulable Factory-3 operation from a fresh
    # planning state.  No historical decoder assignment is added.
    full_context = PlanningContextV1(
        f"factory-{factory_id}-assembled", "INITIAL_PLAN", factory_id, 0, 0, horizon,
        horizon, factory_keys, source_type,
        snapshot_as_of_seconds=snapshot_as_of)
    full_state = build_planning_state(loaded, full_context, boundary=None)
    assembled_rows = [assembled[key] for key in sorted(assembled)]
    final_validation = validate_assembled_factory(
        full_state, assembled_rows, required_operation_keys=factory_keys)
    assembled_validation_seconds = time.perf_counter() - assembled_validation_started
    full_report = final_validation.get("physical", {}).get("report") or {}
    assembled_payload = {
        "factory_id": factory_id, "snapshot_as_of_seconds": snapshot_as_of,
        "required_operation_keys": list(factory_keys),
        "assembled_operation_keys": sorted(assembled),
        "assembled_schedule": assembled_rows,
        "validation": final_validation,
        "factory_complete_claimed": bool(final_validation.get("accepted")),
        "post_cp_modifications": 0, "historical_future_assignments_backfilled": 0,
        "database_writes": 0,
        "validator_counts": full_report.get("counts", {}),
    }
    write_json("assembled_factory_schedule.json", assembled_payload)
    write_json("assembled_factory_validation.json", final_validation)
    zero_final_violations = all(int(value) == 0 for value in
                                final_validation.get("violation_counts", {}).values())
    all_windows_accepted = all(bool(item.get("accepted"))
                               and int(item.get("materialization_mismatches", 0)) == 0
                               and int(item.get("historical_future_assignments_backfilled", 0)) == 0
                               and int(item.get("database_writes", 0)) == 0
                               and int(item.get("committed_operations_changed", 0)) == 0
                               and int(item.get("provisional_hard_frozen", 0)) == 0
                               for item in progress)
    success_status = ("GLOBAL_CRAFT_FACTORY5_45_VALIDATED" if factory_id == 5
                      else "GLOBAL_CRAFT_ONE_FACTORY_VALIDATED")
    status = success_status if (
        set(assembled) == factory_set and final_validation.get("accepted")
        and final_validation.get("scope", {}).get("missing_required_operations") == []
        and final_validation.get("scope", {}).get("unexpected_operations") == []
        and final_validation.get("scope", {}).get("duplicate_operations") == []
        and final_validation.get("replay", {}).get("historical_future_assignments_backfilled") == 0
        and zero_final_violations and all_windows_accepted
    ) else "GLOBAL_CRAFT_ONE_FACTORY_INCOMPLETE"
    result = {"status": status, "factory_id": factory_id,
              "windows": len(windows), "accounting": accounting,
              "scheduled": len(assembled), "remaining_unscheduled": sorted(factory_set - set(assembled)),
              "final_validation": final_validation, "database_writes": 0,
              "total_seconds": time.perf_counter() - STARTED}
    write_json("runtime_profile.json", {
        "database_state_load_seconds": state_load_seconds,
        "rolling_execution_seconds_excluding_load": rolling_execution_seconds,
        "assembled_validation_seconds": assembled_validation_seconds,
        "total_seconds": result["total_seconds"],
        "windows": progress, "database_writes": 0,
    })
    cp_stage_rows = [item for item in progress if item.get("cp_stage_attempt_count") is not None]
    cp_stage_counts = [int(item.get("cp_stage_attempt_count", 0)) for item in cp_stage_rows]
    write_json("cp_stage_accounting.json", {
        "factory_id": factory_id,
        "windows": len(cp_stage_rows),
        "cp_stage_attempt_count_distribution": {
            str(count): cp_stage_counts.count(count) for count in sorted(set(cp_stage_counts))
        },
        "maximum_stages_attempted": max(cp_stage_counts, default=0),
        "total_cp_model_build_seconds_all_stages": sum(
            float(item.get("total_cp_model_build_seconds_all_stages", 0.0)) for item in cp_stage_rows),
        "total_cp_solve_seconds_all_stages": sum(
            float(item.get("total_cp_solve_seconds_all_stages", 0.0)) for item in cp_stage_rows),
        "total_cp_branches_all_stages": sum(
            int(item.get("total_cp_branches_all_stages", 0)) for item in cp_stage_rows),
        "total_cp_conflicts_all_stages": sum(
            int(item.get("total_cp_conflicts_all_stages", 0)) for item in cp_stage_rows),
        "winning_stage_variables_max": max(
            (int(item["winning_stage_variables"]) for item in cp_stage_rows
             if item.get("winning_stage_variables") is not None), default=0),
        "winning_stage_constraints_max": max(
            (int(item["winning_stage_constraints"]) for item in cp_stage_rows
             if item.get("winning_stage_constraints") is not None), default=0),
        "winning_stage_optional_intervals_max": max(
            (int(item["winning_stage_optional_intervals"]) for item in cp_stage_rows
             if item.get("winning_stage_optional_intervals") is not None), default=0),
        "database_writes": 0,
    })
    write_json("complexity_profile.json", {
        "factory_id": factory_id,
        "windows": [{
            "window_id": item.get("window_id"),
            "required_operation_count": item.get("craft_complexity", {}).get("required_operation_count", 0),
            "selected_operation_count": item.get("craft_complexity", {}).get("selected_operation_count", 0),
            "committed_count": item.get("craft_complexity", {}).get("committed_count", 0),
            "provisional_count": item.get("craft_complexity", {}).get("provisional_count", 0),
            "future_outside_count": item.get("craft_complexity", {}).get("future_outside_count", 0),
            "resource_configuration_count": item.get("craft_complexity", {}).get("resource_configuration_count", 0),
            "calendar_regime_count": item.get("craft_complexity", {}).get("calendar_regime_count", 0),
            "unique_machine_count": len(item.get("craft_complexity", {}).get("unique_machines", [])),
            "unique_worker_count": len(item.get("craft_complexity", {}).get("unique_workers", [])),
            "unique_line_count": len(item.get("craft_complexity", {}).get("unique_lines", [])),
            "cp_stage_attempt_count": item.get("cp_stage_attempt_count", 0),
            "winning_stage_variables": item.get("winning_stage_variables"),
            "winning_stage_constraints": item.get("winning_stage_constraints"),
            "winning_stage_optional_intervals": item.get("winning_stage_optional_intervals"),
        } for item in progress],
        "database_writes": 0,
    })
    write_json("commit_integrity.json", {
        "factory_id": factory_id,
        "committed_operations_checked": len(commitment_records),
        "committed_operations_changed": sum(
            1 for item in commitment_records
            if not item.get("boundary_signature_matches", False)),
        "operations": commitment_records,
        "database_writes": 0,
    })
    write_json("provisional_behavior.json", {
        "factory_id": factory_id,
        "windows": provisional_records,
        "provisional_operations_carried": sum(len(x["carried"]) for x in provisional_records),
        "provisional_operations_changed": sum(len(x["changed"]) for x in provisional_records),
        "provisional_operations_unchanged": sum(len(x["unchanged"]) for x in provisional_records),
        "provisional_hard_frozen": sum(int(x["provisional_hard_frozen"]) for x in provisional_records),
        "database_writes": 0,
    })
    write_json("rolling_progress.json", {"status": status, "windows": progress,
                                           "cumulative_committed": sorted(assembled),
                                           "remaining": sorted(factory_set - set(assembled)),
                                           "database_writes": 0})
    write_json("final_report.md", "")
    return result


if __name__ == "__main__":
    try:
        result = run()
        report = (
            f"# Factory-{result.get('factory_id')} rolling validation\n\n"
            f"Status: `{result['status']}`\n\n"
            f"Windows: {result.get('windows', 0)}; scheduled: {result.get('scheduled', 0)}; "
            f"remaining: {len(result.get('remaining_unscheduled', ())) }; DB writes: 0\n"
        )
        (OUT / "final_report.md").write_text(report, encoding="utf-8")
        print(json.dumps({"status": result["status"], "windows": result.get("windows", 0),
                          "scheduled": result.get("scheduled", 0),
                          "remaining": result.get("remaining_unscheduled", [])}, default=str), flush=True)
    except Exception as exc:
        failure = {"status": "GLOBAL_CRAFT_ONE_FACTORY_INCOMPLETE",
                   "first_blocker": f"{type(exc).__name__}: {exc}",
                   "traceback": traceback.format_exc(), "database_writes": 0,
                   "total_seconds": time.perf_counter() - STARTED}
        write_json("final_report.md", json.dumps(failure, indent=2))
        write_json("rolling_progress.json", failure)
        print(json.dumps(failure), flush=True)
        raise
