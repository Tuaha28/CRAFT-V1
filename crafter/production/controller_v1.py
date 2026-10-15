"""Shallow CRAFT V1 production controller.

This module intentionally does not call the nested V2/V3 research
controllers.  It performs one compact solve, then selects at most one exact
recovery branch (temporal widening *or* route-aware worker-crew expansion).
The canonical CP/replay/validator implementations remain authoritative.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Mapping

from crafter.repair.boundary_aware_modes import build_boundary_availability_index
from crafter.repair.balanced_resource_rescue import (
    BalancedResourceRescuePolicyV1,
    run_balanced_resource_feasibility_rescue,
)
from crafter.repair.calendar_regimes import (CalendarRegimeCache,
                                             IncrementalCalendarRegimeBuilder,
                                             generate_calendar_regimes_selected)
from crafter.repair.resource_configs import generate_resource_configurations
from crafter.repair.route_aware_crew_expansion import (
    RouteAwareCrewExpansionPolicyV1,
    audit_strict_superset,
    diagnose_worker_route_pressure,
    run_route_aware_adaptive_crew_expansion,
)
from crafter.repair.solve import run_canonical_schedule


TEMPORAL_SCOPE_INSUFFICIENT = "TEMPORAL_SCOPE_INSUFFICIENT"
WORKER_CREW_DOMAIN_TRUNCATED = "WORKER_CREW_DOMAIN_TRUNCATED"
OTHER_UNRESOLVED = "OTHER_UNRESOLVED"


@dataclass(frozen=True)
class ProductionControllerConfigV1:
    normal_seconds: float = 45.0
    temporal_retry_seconds: float = 45.0
    worker_retry_seconds: float = 60.0
    # A finite, configurable safety bound prevents an accidental recovery
    # loop from retaining an unbounded sequence of domains.  It is an
    # operational guard, never a physical infeasibility claim.
    max_worker_rounds: int | None = 4
    new_crews_per_operation: int = 8
    # Retained as a compatibility field for older callers.  It is not a
    # preparation deadline; worker-recovery preparation is structurally
    # guarded, and ``worker_retry_seconds`` is the CP-SAT solve allowance.
    worker_recovery_seconds: float = 120.0
    window_ceiling_seconds: float = 120.0
    normal_worker_caps: tuple[int, ...] = (1, 2, 3)
    # Production starts from the empirically sufficient compact K3 matched
    # domain and performs one evidence-selected crew expansion.  Research
    # callers can disable this to retain the historical K1/K2/K3 ladder.
    direct_matching_domain: bool = True
    # Selective middle-ground recovery: three highest-evidence operations in
    # the first round, then at most four unresolved bottlenecks.  The hard
    # target cap remains independently configurable.
    max_pressure_targets_per_round: int | None = 4
    pressure_targets_by_round: tuple[int, ...] = (3, 4, 4, 4)
    new_machine_alternatives_per_operation_by_round: tuple[int, ...] = (1, 2, 3, 4)
    max_recovery_resource_configurations: int = 600
    max_recovery_calendar_regimes: int = 5500
    max_recovery_peak_rss_mb: float = 1536.0
    max_recovery_runtime_seconds: float = 300.0
    # One final breadth pass is permitted only after the normal bounded
    # adaptive recovery reaches its round guard with legal alternatives still
    # unexposed.  These are operational ceilings, never infeasibility claims.
    balanced_rescue_enabled: bool = True
    balanced_rescue_min_configurations_low: int = 8
    balanced_rescue_min_configurations_high: int = 12
    balanced_rescue_max_resource_configurations: int = 800
    balanced_rescue_max_calendar_regimes: int = 7500
    balanced_rescue_max_peak_rss_mb: float = 1536.0
    balanced_rescue_max_runtime_seconds: float = 300.0


def _candidate_valid(result: Mapping[str, Any]) -> bool:
    acceptance = result.get("acceptance", {}) or {}
    selected = acceptance.get("selected_schedule") or ()
    mismatch = int((acceptance.get("materialization") or {}).get("mismatch_count", 0))
    replay = (acceptance.get("replay") or {}).get("status")
    validator = (acceptance.get("validator") or {}).get("status")
    return bool(selected) and mismatch == 0 and replay == "PASS" and validator == "VALID"


def _worker_truncation_evidence(resource_audit) -> dict[str, dict]:
    """Extract explicit worker-domain evidence, independently of route pressure."""
    evidence = {}
    for row in resource_audit or ():
        if not isinstance(row, Mapping):
            continue
        key = str(row.get("operation_key", ""))
        domain_rows = row.get("domain_rows") or ()
        worker_flag = bool(row.get("worker_domain_truncated", False))
        reasons = list(row.get("worker_pruning_reasons") or ())
        more = bool(row.get("more_worker_crews_available", False))
        for domain in domain_rows:
            if not isinstance(domain, Mapping):
                continue
            worker_flag = worker_flag or bool(domain.get("worker_domain_truncated", False))
            reasons.extend(domain.get("worker_pruning_reasons") or ())
            more = more or bool(domain.get("more_worker_crews_available", False))
            exposed = domain.get("worker_crews_after_pruning") or ()
            legal = domain.get("total_legal_worker_crews")
            if legal is not None:
                try:
                    more = more or int(legal) > len(exposed)
                except (TypeError, ValueError):
                    pass
        if worker_flag or more or reasons:
            evidence[key] = {
                "operation_key": key,
                "worker_domain_truncated": True,
                "more_worker_crews_available": bool(more),
                "worker_pruning_reasons": sorted({str(x) for x in reasons}),
            }
    return evidence


def _failure_classification(prepared: Mapping[str, Any]) -> tuple[str, dict]:
    acceptance = prepared.get("acceptance", {}) or {}
    statuses = [str(row.get("status")) for row in acceptance.get("stages", ()) or ()]
    state = prepared.get("state", {})
    pressure = diagnose_worker_route_pressure(
        state, prepared.get("resource_configurations", {}),
        prepared.get("regimes", {}), prepared.get("resource_audit", ()))
    pressured = [row for row in pressure if row.get("route_pressure")]
    truncation = _worker_truncation_evidence(prepared.get("resource_audit", ()))
    pressured_truncated = [
        row for row in pressured
        if str(row.get("operation_key", "")) in truncation
    ]
    if pressured_truncated:
        return WORKER_CREW_DOMAIN_TRUNCATED, {
            "worker_route_pressure": pressured_truncated,
            "worker_route_pressure_all": pressure,
            "route_pressure_without_truncation_proof": [
                row for row in pressured if row not in pressured_truncated
            ],
            "worker_truncation_evidence": truncation,
            "resource_domain_truncated": True,
        }
    if statuses and all(status in {"INFEASIBLE", "UNKNOWN", "SEARCH_LIMIT_NO_INCUMBENT"}
                        for status in statuses):
        return TEMPORAL_SCOPE_INSUFFICIENT, {
            "cp_stage_statuses": statuses,
            "reason": "worker truncation was not proven; one exact wider temporal retry is permitted",
            "route_pressure_without_truncation_proof": pressured,
            "worker_truncation_evidence": truncation,
        }
    return OTHER_UNRESOLVED, {
        "cp_stage_statuses": statuses,
        "route_pressure_without_truncation_proof": pressured,
        "worker_truncation_evidence": truncation,
    }


class ProductionControllerV1:
    """One normal attempt plus one evidence-selected recovery attempt."""

    def __init__(self, config: ProductionControllerConfigV1 | None = None):
        self.config = config or ProductionControllerConfigV1()

    def run_window(self, prepared: dict, *, boundary=None, cache=None,
                   boundary_facts=(), required_operation_keys=()) -> dict:
        if (self.config.direct_matching_domain
                and prepared.get("resource_audit") is not None
                and prepared.get("resource_configurations") is not None
                and prepared.get("regimes") is not None
                and (prepared.get("state") or {}).get("problem") is not None):
            return self._run_direct_matching(
                prepared, boundary=boundary, cache=cache,
                boundary_facts=boundary_facts,
                required_operation_keys=required_operation_keys)
        normal, normal_trace = self._normal_ladder(prepared, cache=cache)
        if normal_trace.get("accepted"):
            return {"candidate": normal, "recovery": "NONE",
                    "failure_classification": None,
                    "trace": {"normal": normal_trace, "recovery": "NONE"}}

        classification, diagnosis = _failure_classification(normal)
        if classification == TEMPORAL_SCOPE_INSUFFICIENT:
            retry = dict(prepared)
            retry["state"] = dict(prepared["state"])
            retry = run_canonical_schedule(
                retry, objective_mode=False,
                plan=((999, None, float(self.config.temporal_retry_seconds)),),
                unrestricted_final_stage=True)
            return {"candidate": retry, "recovery": "TEMPORAL",
                    "failure_classification": classification,
                    "trace": {"normal": normal_trace,
                              "diagnosis": diagnosis,
                              "recovery": self._summary(retry)}}

        # An empty carried boundary is still a valid boundary state on the
        # first rolling window.  The worker-domain recovery must be able to
        # diagnose/expand against that empty state; requiring a non-None
        # boundary would incorrectly suppress the only V1 recovery branch.
        if classification == WORKER_CREW_DOMAIN_TRUNCATED:
            return self._worker_recovery(
                normal, boundary=boundary, cache=cache,
                boundary_facts=boundary_facts,
                required_operation_keys=required_operation_keys,
                diagnosis=diagnosis, normal_trace=normal_trace)

        return {"candidate": normal, "recovery": "NONE",
                "failure_classification": classification,
                "trace": {"normal": normal_trace, "diagnosis": diagnosis,
                           "recovery": "NONE"}}

    def _run_direct_matching(self, prepared, *, boundary, cache,
                             boundary_facts, required_operation_keys):
        """Skip known-redundant K1/K2 solves while preserving the K3 domain."""
        target_cap = max(int(value) for value in self.config.normal_worker_caps)
        existing_cap = min(
            (len({tuple(config.worker_ids) for config in rows})
             for rows in prepared.get('resource_configurations', {}).values() if rows),
            default=0)
        base = (prepared if existing_cap >= target_cap
                else self._normal_variant(prepared, target_cap, cache=cache))
        trace = {
            "accepted": False,
            "mode": "DIRECT_COMPACT_MATCHING_DOMAIN",
            "worker_caps_attempted": [],
            "skipped_redundant_caps": list(self.config.normal_worker_caps),
            "base_worker_cap": target_cap,
            "attempts": [],
            "total_budget_seconds": float(self.config.normal_seconds),
            "deadline_exhausted": False,
        }
        # Always give the compact domain a real CP attempt before any worker
        # recovery.  This both avoids needless expansion and makes the
        # recovery decision evidence-driven by the actual solve result.
        initial = run_canonical_schedule(
            base, objective_mode=False,
            plan=((999, None, float(self.config.normal_seconds)),),
            unrestricted_final_stage=True)
        trace["attempts"].append({"worker_cap": target_cap,
                                  "phase": "INITIAL_COMPACT_DOMAIN",
                                  "result": self._summary(initial)})
        trace["accepted"] = _candidate_valid(initial)
        if trace["accepted"]:
            return {"candidate": initial, "recovery": "NONE",
                    "failure_classification": None,
                    "trace": {"normal": trace, "recovery": "NONE"}}

        classification, diagnosis = _failure_classification(initial)
        if classification == WORKER_CREW_DOMAIN_TRUNCATED:
            return self._worker_recovery(
                initial, boundary=boundary, cache=cache,
                boundary_facts=boundary_facts,
                required_operation_keys=required_operation_keys,
                diagnosis=diagnosis, normal_trace=trace)

        # Reuse the exact initial result; do not rebuild the model merely to
        # classify a temporal/other failure.
        if classification == TEMPORAL_SCOPE_INSUFFICIENT:
            retry = dict(base)
            retry["state"] = dict(base["state"])
            retry = run_canonical_schedule(
                retry, objective_mode=False,
                plan=((999, None, float(self.config.temporal_retry_seconds)),),
                unrestricted_final_stage=True)
            return {"candidate": retry, "recovery": "TEMPORAL",
                    "failure_classification": classification,
                    "trace": {"normal": trace, "diagnosis": diagnosis,
                              "recovery": self._summary(retry)}}
        return {"candidate": initial, "recovery": "NONE",
                "failure_classification": classification,
                "trace": {"normal": trace, "diagnosis": diagnosis,
                          "recovery": "NONE"}}

    def _normal_ladder(self, prepared, *, cache=None):
        """Run K1/K2/K3 under one monotonic normal-attempt deadline."""
        deadline = time.perf_counter() + float(self.config.normal_seconds)
        attempts = []
        last = prepared
        caps = tuple(int(x) for x in self.config.normal_worker_caps)
        for index, k in enumerate(caps):
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            variant = self._normal_variant(prepared, k, cache=cache)
            if variant is None:
                if index > 0:
                    break
                variant = prepared
            last = run_canonical_schedule(
                variant, objective_mode=False,
                plan=((1, 7, max(0.001, remaining)),))
            attempts.append({
                "worker_cap": k,
                "remaining_budget_before_seconds": float(remaining),
                "result": self._summary(last),
            })
            if _candidate_valid(last):
                return last, {
                    "accepted": True,
                    "worker_caps_attempted": [x["worker_cap"] for x in attempts],
                    "attempts": attempts,
                    "total_budget_seconds": float(self.config.normal_seconds),
                    "deadline_exhausted": time.perf_counter() >= deadline,
                }
        return last, {
            "accepted": False,
            "worker_caps_attempted": [x["worker_cap"] for x in attempts],
            "attempts": attempts,
            "total_budget_seconds": float(self.config.normal_seconds),
            "deadline_exhausted": time.perf_counter() >= deadline,
        }

    @staticmethod
    def _normal_variant(prepared, worker_cap, *, cache=None):
        if int(worker_cap) <= 1:
            return prepared
        state = prepared.get("state") or {}
        if "problem" not in state:
            return None
        configs, audit, summary = generate_resource_configurations(
            state, max_machine_sets=1, max_worker_crews=int(worker_cap),
            machine_pool_limit=30)
        regimes, regime_summary, total, equivalence = generate_calendar_regimes_selected(
            state, configs, cache=cache)
        variant = dict(prepared)
        variant.update({
            "resource_configurations": configs,
            "resource_audit": audit,
            "resource_summaries": [summary],
            "regimes": regimes,
            "regime_summary": regime_summary,
            "regime_count": int(total),
            "calendar_equivalence": equivalence,
        })
        return variant

    def _worker_recovery(self, normal, *, boundary, cache,
                         boundary_facts, required_operation_keys, diagnosis,
                         normal_trace):
        state = normal["state"]
        # Always give recovery a scoped cache so pruning is applied even when
        # a caller did not provide a cross-window cache object.
        cache = cache or CalendarRegimeCache()
        base_configs = normal.get("resource_configurations") or {}
        availability = build_boundary_availability_index(
            state, boundary, horizon_start_seconds=0)

        incremental = IncrementalCalendarRegimeBuilder(state, cache=cache)
        incremental.seed(
            base_configs, normal.get("regimes") or {},
            normal.get("regime_summary") or (),
            normal.get("calendar_equivalence") or {},
            additional_breakpoints_by_operation={})

        def build_regimes(configurations, start_probes=None):
            started = time.perf_counter()
            if cache is not None and hasattr(cache, "prune_to_configurations"):
                # Cache eviction only drops memoized calculations that cannot
                # be used by this replacement domain; it never removes a
                # legal configuration or changes an exact regime.
                cache.prune_to_configurations(configurations)
            regimes, summary, total, equivalence = incremental.build(
                configurations,
                additional_breakpoints_by_operation=start_probes or {})
            return regimes, {
                "resource_configuration_count": sum(len(v) for v in configurations.values()),
                "calendar_regime_count": int(total),
                "zero_regime_operation_keys": sorted(k for k, v in regimes.items() if not v),
                "equivalence": equivalence,
                "calendar_generation_seconds": time.perf_counter() - started,
                "regime_cache": dict(equivalence.get("incremental_profile") or {}),
            }

        # Production V1 diagnostic timing starts the CP-SAT budget only when
        # the solver is entered.  Preparation (matching, domain and regime
        # construction) is structurally guarded but not wall-clock aborted.
        recovery_deadline = None

        def solve_round(round_index, configurations, regimes, metadata):
            return solve_domain(
                round_index, configurations, regimes, metadata,
                objective_mode=False, incumbent_selected_schedule=None)

        def solve_domain(round_index, configurations, regimes, metadata, *,
                         objective_mode, incumbent_selected_schedule):
            retry = dict(normal)
            retry.update({
                "state": state,
                "resource_configurations": configurations,
                "regimes": regimes,
                "regime_count": metadata["calendar_regime_count"],
                "calendar_equivalence": metadata["equivalence"],
                "adaptive_recovery_attempt": True,
            })
            solve_started = time.perf_counter()
            result = run_canonical_schedule(
                retry, objective_mode=bool(objective_mode),
                plan=((999, None, float(self.config.worker_retry_seconds)),),
                unrestricted_final_stage=True,
                incumbent_selected_schedule=incumbent_selected_schedule)
            return {"round": round_index, "accepted": _candidate_valid(result),
                    "candidate_exists": bool((result.get("acceptance", {}) or {}).get("selected_schedule")),
                    "candidate": result,
                    "resource_configuration_count": metadata["resource_configuration_count"],
                    "calendar_regime_count": metadata["calendar_regime_count"],
                    "calendar_generation_seconds": float(metadata.get("calendar_generation_seconds", 0.0)),
                    "regime_cache": dict(metadata.get("regime_cache") or {}),
                    "cp_seconds": time.perf_counter() - solve_started,
                    "database_writes": 0}

        max_rounds = self.config.max_worker_rounds
        env_rounds = os.environ.get("CRAFT_PRODUCTION_MAX_WORKER_ROUNDS")
        if env_rounds is not None and str(env_rounds).strip():
            max_rounds = int(env_rounds)
        max_targets = self.config.max_pressure_targets_per_round
        env_targets = os.environ.get("CRAFT_PRODUCTION_MAX_PRESSURE_TARGETS")
        if env_targets is not None and str(env_targets).strip():
            max_targets = int(env_targets)
        budgets = [int(self.config.new_crews_per_operation),
                   int(self.config.new_crews_per_operation) * 2]
        if max_rounds is not None:
            while len(budgets) < int(max_rounds):
                budgets.append(budgets[-1] * 2)
        policy = RouteAwareCrewExpansionPolicyV1(
            max_rounds=max_rounds,
            total_new_crews_per_operation_by_round=tuple(budgets),
            max_expansion_operations=None,
            max_successor_depth=None,
            max_pressure_targets_per_round=max_targets,
            pressure_targets_by_round=tuple(
                int(value) for value in self.config.pressure_targets_by_round),
            total_new_machine_alternatives_per_operation_by_round=tuple(
                int(value) for value in
                self.config.new_machine_alternatives_per_operation_by_round),
            max_resource_configurations=int(
                self.config.max_recovery_resource_configurations),
            max_calendar_regimes=int(self.config.max_recovery_calendar_regimes),
            max_peak_rss_mb=float(self.config.max_recovery_peak_rss_mb),
            max_recovery_runtime_seconds=float(
                self.config.max_recovery_runtime_seconds))
        controller = run_route_aware_adaptive_crew_expansion(
            state, availability, base_configs, normal.get("resource_audit", ()),
            build_regimes, solve_round, policy=policy,
            baseline_result={"accepted": False, "candidate": normal,
                             "candidate_exists": bool((normal.get("acceptance", {}) or {}).get("selected_schedule")),
                             "round": 0, "database_writes": 0},
            recovery_deadline=None,
            initial_pressure_diagnosis=tuple(
                diagnosis.get("worker_route_pressure_all") or ()))
        balanced_rescue = None
        if (self.config.balanced_rescue_enabled and
                controller.get("status") == "COMPUTATIONAL_LIMIT" and
                controller.get("reason") == "MAX_WORKER_RECOVERY_ROUNDS" and
                controller.get("remaining_truncated_operations")):
            rescue_policy = BalancedResourceRescuePolicyV1(
                minimum_configurations_low=int(
                    self.config.balanced_rescue_min_configurations_low),
                minimum_configurations_high=int(
                    self.config.balanced_rescue_min_configurations_high),
                max_resource_configurations=int(
                    self.config.balanced_rescue_max_resource_configurations),
                max_calendar_regimes=int(
                    self.config.balanced_rescue_max_calendar_regimes),
                max_peak_rss_mb=float(
                    self.config.balanced_rescue_max_peak_rss_mb),
                max_runtime_seconds=float(
                    self.config.balanced_rescue_max_runtime_seconds))

            def solve_rescue_feasibility(configurations, regimes, metadata):
                return solve_domain(
                    "BALANCED_FEASIBILITY", configurations, regimes, metadata,
                    objective_mode=False, incumbent_selected_schedule=None)

            def solve_rescue_objective(configurations, regimes, metadata, incumbent):
                return solve_domain(
                    "BALANCED_OBJECTIVE", configurations, regimes, metadata,
                    objective_mode=True,
                    incumbent_selected_schedule=incumbent)

            balanced_rescue = run_balanced_resource_feasibility_rescue(
                state, availability,
                controller.get("configurations") or base_configs,
                controller.get("regimes") or normal.get("regimes") or {},
                normal.get("resource_audit", ()), build_regimes,
                solve_rescue_feasibility, solve_rescue_objective,
                existing_start_probes=controller.get(
                    "start_probes_by_operation") or {},
                policy=rescue_policy)
            controller["balanced_rescue"] = balanced_rescue
            controller["configurations"] = balanced_rescue.get(
                "configurations", controller.get("configurations"))
            controller["regimes"] = balanced_rescue.get(
                "regimes", controller.get("regimes"))
            if balanced_rescue.get("selected_result"):
                controller["selected_result"] = balanced_rescue["selected_result"]
            controller["status"] = balanced_rescue.get(
                "status", controller.get("status"))
            controller["reason"] = balanced_rescue.get(
                "reason", controller.get("reason"))
        if controller.get("status") == "WORKER_CREW_DOMAIN_STILL_TRUNCATED":
            controller["status"] = "MATCHING_RECOVERY_ATTEMPTS_EXHAUSTED"
        selected = controller.get("selected_result") or {}
        candidate = selected.get("candidate") if selected else normal
        def compact_round_solve(payload):
            if not isinstance(payload, Mapping):
                return None
            # The expansion kernel stores only a compact diagnostic.  Legacy
            # callers may still provide a full result, so retain compatibility.
            if "acceptance" in payload:
                return self._summary(payload)
            return dict(payload)
        return {
            "candidate": candidate,
            "recovery": "CREW",
            "failure_classification": WORKER_CREW_DOMAIN_TRUNCATED,
            "trace": {
                "normal": normal_trace, "diagnosis": diagnosis,
                "controller_status": controller.get("status"),
                "controller_terminal": {
                    key: controller.get(key)
                    for key in (
                        "status", "reason", "safety_bound_rounds",
                        "attempted_resource_configuration_count",
                        "resource_configuration_limit",
                        "attempted_calendar_regime_count",
                        "calendar_regime_limit", "recovery_runtime_seconds",
                        "peak_rss_mb", "peak_rss_limit_mb",
                        "remaining_truncated_operations",
                    )
                    if controller.get(key) is not None
                },
                "balanced_rescue": ({
                    key: value for key, value in balanced_rescue.items()
                    if key not in {"configurations", "regimes", "selected_result"}
                } if isinstance(balanced_rescue, Mapping) else None),
                "rounds": [
                    {**{k: v for k, v in row.items() if k != "solve"},
                    "solve_summary": {
                         **(compact_round_solve(row["solve"]) or {}),
                         "round_accepted": bool(row["solve"].get("accepted", False)),
                         "round_candidate_exists": bool(row["solve"].get(
                             "candidate_exists", False)),
                         "cp_seconds": float(row["solve"].get("cp_seconds", 0.0) or 0.0),
                         "calendar_generation_seconds": float(row["solve"].get(
                             "calendar_generation_seconds", 0.0) or 0.0),
                         "regime_cache": dict(row["solve"].get("regime_cache") or {}),
                         "closure_seconds": float(row.get("closure_seconds", 0.0) or 0.0),
                         "resource_configuration_seconds": float(
                             row.get("resource_configuration_seconds", 0.0) or 0.0),
                     }
                     if isinstance(row.get("solve"), Mapping) else None}
                    for row in controller.get("rounds", ())],
                "domain_superset": audit_strict_superset(
                    base_configs, controller.get("configurations", base_configs)),
                "recovery": self._summary(candidate),
            },
        }

    @staticmethod
    def _summary(result):
        acceptance = result.get("acceptance", {}) or {}
        winning = acceptance.get("winning_stage") or {}
        stages = acceptance.get("stages") or ()
        last_stage = stages[-1] if stages and isinstance(stages[-1], Mapping) else {}
        replay = acceptance.get("replay") or {}
        materialization = acceptance.get("materialization") or {}
        cp_build_seconds = sum(float(row.get("model_build_seconds", 0.0) or 0.0)
                               for row in stages if isinstance(row, Mapping))
        cp_solve_seconds = sum(float(row.get("solve_seconds", row.get("runtime_seconds", 0.0)) or 0.0)
                               for row in stages if isinstance(row, Mapping))
        return {
            "status": acceptance.get("status"),
            "solver_status": winning.get("status") or last_stage.get("status"),
            "cp_time_limit_seconds": last_stage.get("deadline_budget_seconds"),
            "search_limit_hit": last_stage.get("search_limit_hit"),
            "candidate_exists": bool(acceptance.get("selected_schedule")),
            "resource_configurations": sum(len(v) for v in result.get("resource_configurations", {}).values()),
            "regimes": int(result.get("regime_count", 0)),
            "replay": replay.get("status"),
            "replay_mismatch_count": int(replay.get("mismatch_count", 0) or 0),
            "first_replay_mismatch": (replay.get("mismatches") or [None])[0],
            "validator": (acceptance.get("validator") or {}).get("status"),
            "validator_phenotype_status": (acceptance.get("validator") or {}).get("phenotype_status"),
            "validator_report": (acceptance.get("validator") or {}).get("validation_report"),
            "event_violations": acceptance.get("event_violations") or [],
            "factory_isolation_violations": (acceptance.get("factory_isolation") or {}).get("violations") or [],
            "materialization_mismatch_count": int(materialization.get("mismatch_count", 0) or 0),
            "cp_model_build_seconds": cp_build_seconds,
            "cp_solve_seconds": cp_solve_seconds,
            "cp_attempt_started": bool(stages),
            "database_writes": 0,
        }
