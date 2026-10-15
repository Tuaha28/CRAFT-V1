"""Adaptive, auditable resource-domain expansion for planning windows.

This module is deliberately a planning adapter.  It never changes the CP
physical model or relaxes a constraint.  A compact legal candidate domain is
solved first; expansion is permitted only when the same canonical model is
infeasible, unused legal alternatives are proven to remain, and a resource
overlap probe implicates machines and/or workers.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import time

from ortools.sat.python import cp_model

from .calendar_regimes import CalendarRegimeCache
from .cp_repair import build_repair_model
from .planning import prepare_planning_schedule
from .solve import run_canonical_schedule


def _window_deadline_expired(state):
    deadline = state.get("_craft_window_deadline") if isinstance(state, dict) else None
    return deadline is not None and time.perf_counter() >= float(deadline)


@dataclass(frozen=True)
class ResourceDomainPolicyV2:
    """Deterministic progressive-domain policy.

    The policy uses a geometric value only as an upper bound, while exposing
    one deterministic candidate tier at a time.  Each stage is capped at the
    exact finite legal combination count reported by the domain builder.  No
    factory, operation, or known successful cap is embedded here.
    """

    initial_machine_sets: int = 1
    initial_worker_crews: int = 1
    growth_factor: int = 2
    max_expansion_rounds: int = 6
    machine_pool_limit: int = 30
    diagnostic_time_limit_seconds: float = 15.0

    def __post_init__(self):
        if self.initial_machine_sets < 1 or self.initial_worker_crews < 1:
            raise ValueError("initial resource-domain caps must be positive")
        if self.growth_factor < 2:
            raise ValueError("resource-domain growth factor must be at least two")
        if self.max_expansion_rounds < 0:
            raise ValueError("resource-domain expansion rounds cannot be negative")


def _status_name(code):
    return cp_model.CpSolver().StatusName(code)


def _resource_audit_rows(audit):
    rows = []
    for item in audit or ():
        domain_rows = item.get("domain_rows", ())
        rows.append({
            "operation_key": str(item.get("operation_key")),
            "resource_configuration_count": int(item.get("total_resource_configurations", 0)),
            "truncated": bool(item.get("truncated", False)),
            "machine_alternatives_remaining": any(
                bool(row.get("more_machine_sets_available")) for row in domain_rows),
            "worker_alternatives_remaining": any(
                bool(row.get("more_worker_crews_available")) for row in domain_rows),
            # A resource cap applies per legal line/k row, while the complete
            # operation domain is the disjoint union of those rows.  Sum the
            # finite capacities; using max would hide cross-line/k expansion.
            "maximum_legal_machine_sets": sum(
                int(row.get("total_legal_machine_sets", 0)) for row in domain_rows),
            "maximum_legal_worker_crews": sum(
                int(row.get("total_legal_worker_crews", 0)) for row in domain_rows),
            "machine_pool_complete": all(
                bool(row.get("machine_pool_complete", True)) for row in domain_rows),
            "worker_pool_complete": all(
                bool(row.get("worker_pool_complete", True)) for row in domain_rows),
            "machine_ids": sorted({int(mid) for row in domain_rows
                                    for mid in row.get("machine_pool", ())}),
            "worker_ids": sorted({str(wid) for row in domain_rows
                                   for wid in row.get("worker_pool", ())}),
        })
    return rows


def summarize_resource_domain(audit):
    """Expose exact finite capacities and current truncation without guesses."""
    rows = _resource_audit_rows(audit)
    return {
        "operations": rows,
        "truncated_operation_keys": sorted(row["operation_key"] for row in rows
                                            if row["truncated"]),
        "unused_machine_alternatives": sorted(row["operation_key"] for row in rows
                                               if row["machine_alternatives_remaining"]),
        "unused_worker_alternatives": sorted(row["operation_key"] for row in rows
                                              if row["worker_alternatives_remaining"]),
        "resource_domain_truncated": any(row["truncated"] for row in rows),
        "machine_pool_incomplete": any(not row["machine_pool_complete"] for row in rows),
        "worker_pool_incomplete": any(not row["worker_pool_complete"] for row in rows),
    }


def _complete_configuration_domain(domain):
    return (not domain['resource_domain_truncated'] and
            not domain['machine_pool_incomplete'] and
            not domain['worker_pool_incomplete'])


def _solve_probe(state, regimes, *, disable_machine=False, disable_worker=False,
                 disable_routing=False,
                 seconds=15.0):
    """Solve a diagnostic variant using the unchanged canonical builder."""
    started = time.perf_counter()
    try:
        artifact = build_repair_model(
            state, regimes,
            include_internal_machine_no_overlap=not disable_machine,
            include_internal_worker_no_overlap=not disable_worker,
            include_internal_routing=not disable_routing,
            include_outside_routing=not disable_routing)
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 8
        solver.parameters.random_seed = 42
        solver.parameters.max_time_in_seconds = float(seconds)
        code = solver.Solve(artifact["model"])
        return {
            "status": solver.StatusName(code),
            "feasible": code in (cp_model.FEASIBLE, cp_model.OPTIMAL),
            "wall_time_seconds": solver.WallTime(),
            "elapsed_seconds": time.perf_counter() - started,
            "branches": solver.NumBranches(),
            "conflicts": solver.NumConflicts(),
            "variables": artifact.get("size", {}).get("variables"),
            "constraints": artifact.get("size", {}).get("constraints"),
            "optional_intervals": artifact.get("size", {}).get("optional_intervals"),
            "disabled_machine_overlap": bool(disable_machine),
            "disabled_worker_overlap": bool(disable_worker),
            "disabled_routing": bool(disable_routing),
        }
    except Exception as exc:  # diagnostics must never change production outcome
        return {"status": "DIAGNOSTIC_ERROR", "feasible": False,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_seconds": time.perf_counter() - started,
                "disabled_machine_overlap": bool(disable_machine),
                "disabled_worker_overlap": bool(disable_worker),
                "disabled_routing": bool(disable_routing)}


def diagnose_resource_domain_pressure(state, prepared, *, seconds=15.0):
    """Classify whether a failed compact domain is resource-pressure related.

    First the complete *currently exposed* domain is tested.  If it is feasible,
    the canonical failure came from a bounded search/window stage and expanding
    resources would be misleading.  Only an infeasible exposed domain proceeds
    to machine/worker overlap relaxation probes.
    """
    regimes = prepared.get("regimes", {})
    full = _solve_probe(state, regimes, seconds=seconds)
    if full.get("feasible"):
        return {"status": "CANONICAL_STAGE_BOUND_OR_SEARCH_LIMIT",
                "implicated_resources": (), "full_domain_probe": full}
    machine_relaxed = _solve_probe(state, regimes, disable_machine=True, seconds=seconds)
    worker_relaxed = _solve_probe(state, regimes, disable_worker=True, seconds=seconds)
    both_relaxed = None
    if not machine_relaxed.get("feasible") and not worker_relaxed.get("feasible"):
        both_relaxed = _solve_probe(state, regimes, disable_machine=True,
                                     disable_worker=True, seconds=seconds)
    routing_relaxed = _solve_probe(state, regimes, disable_routing=True, seconds=seconds)
    implicated = []
    if machine_relaxed.get("feasible"):
        implicated.append("MACHINE")
    if worker_relaxed.get("feasible"):
        implicated.append("WORKER")
    if both_relaxed and both_relaxed.get("feasible") and not implicated:
        implicated = ["MACHINE", "WORKER"]
    if not implicated and routing_relaxed.get("feasible"):
        # Routing and a truncated resource domain can interact: a compact
        # domain may leave no legal sequence, while a broader exact domain can
        # restore one.  This is an expansion trial, not a physical-infeasibility
        # claim.  Expand only resource types with unused legal alternatives.
        rows = _resource_audit_rows(prepared.get("resource_audit", ()))
        if any(row["machine_alternatives_remaining"] for row in rows):
            implicated.append("MACHINE")
        if any(row["worker_alternatives_remaining"] for row in rows):
            implicated.append("WORKER")
    return {
        "status": ("RESOURCE_OVERLAP_PRESSURE" if implicated
                   else "RESOURCE_DOMAIN_INTERACTION" if routing_relaxed.get("feasible")
                   else "NON_RESOURCE_INFEASIBILITY"),
        "implicated_resources": tuple(implicated),
        "full_domain_probe": full,
        "machine_relaxed_probe": machine_relaxed,
        "worker_relaxed_probe": worker_relaxed,
        "both_relaxed_probe": both_relaxed,
        "routing_relaxed_probe": routing_relaxed,
    }


def _next_cap(current, rows, capacity_key, remaining_key, growth):
    remaining = [int(row.get(capacity_key, 0)) for row in rows
                 if bool(row.get(remaining_key))]
    if not remaining:
        return int(current)
    # Expose one deterministic candidate tier at a time.  The geometric value
    # is only an upper bound, so a successful tier such as 3 is discovered
    # rather than skipped by a guessed ``1 -> 4`` jump.
    target = min(int(current) + 1, int(current) * int(growth))
    finite = max(remaining)
    return min(target, finite) if finite > 0 else target


def _expansion_targets(audit, implicated):
    rows = _resource_audit_rows(audit)
    kinds = set(implicated)
    selected = []
    for row in rows:
        machine = row["machine_alternatives_remaining"] and "MACHINE" in kinds
        worker = row["worker_alternatives_remaining"] and "WORKER" in kinds
        if machine or worker:
            # Transparent pressure ordering: more exposed alternatives first,
            # then operation key.  No opaque scalar score is used.
            selected.append((-(row["maximum_legal_machine_sets"] +
                               row["maximum_legal_worker_crews"]),
                             row["operation_key"]))
    return tuple(key for _score, key in sorted(selected))


def run_adaptive_planning_schedule(state, *, cache=None,
                                   policy: ResourceDomainPolicyV2 | None = None,
                                   objective_mode=False, manager_overrides=(),
                                   objective_time_limit_seconds=30,
                                   canonical_kwargs=None, regime_probe=None,
                                   canonical_plan_factory=None):
    """Run canonical planning with progressive legal resource domains.

    The returned object has the same prepared/acceptance shape as
    ``run_canonical_schedule`` plus ``resource_domain_v2`` trace data, so
    existing planning acceptance and boundary code can consume it unchanged.
    """
    policy = policy or ResourceDomainPolicyV2()
    canonical_kwargs = dict(canonical_kwargs or {})
    # Expansion changes the exposed configurations, not the authoritative
    # calendar state. Reuse exact, state-signature-keyed calculations across
    # retries; a changed reservation/calendar signature remains a cache miss.
    cache = cache if cache is not None else CalendarRegimeCache()
    machine_cap = int(policy.initial_machine_sets)
    worker_cap = int(policy.initial_worker_crews)
    history = []
    current = None
    for expansion_round in range(int(policy.max_expansion_rounds) + 1):
        if current is not None and _window_deadline_expired(state):
            break
        prepared = prepare_planning_schedule(
            state, max_machine_sets=machine_cap, max_worker_crews=worker_cap,
            machine_pool_limit=policy.machine_pool_limit, cache=cache,
            allow_zero_regimes=True, regime_probe=regime_probe)
        zero_keys = tuple(sorted(key for key, rows in prepared["regimes"].items()
                                 if not rows))
        if zero_keys:
            # A compact resource sample can contain no calendar-executable
            # alternative. CP-SAT cannot represent an empty operation domain;
            # expand legal alternatives without calling this physical
            # infeasibility or pretending that a solver ran.
            result = {**prepared, "acceptance": {
                "status": "REJECTED", "stages": [], "winning_stage": None,
                "selected_schedule": [],
                "reason": "ZERO_CALENDAR_REGIME_DOMAIN"}}
        else:
            round_kwargs = dict(canonical_kwargs)
            if canonical_plan_factory is not None:
                round_kwargs['plan'] = canonical_plan_factory(machine_cap, worker_cap)
            result = run_canonical_schedule(
                prepared, objective_mode=objective_mode,
                manager_overrides=manager_overrides,
                objective_time_limit_seconds=objective_time_limit_seconds,
                **round_kwargs)
        current = result
        acceptance = result.get("acceptance", {})
        statuses = [str(row.get("status")) for row in acceptance.get("stages", ())]
        domain = summarize_resource_domain(result.get("resource_audit", ()))
        record = {
            "round": expansion_round,
            "machine_cap": machine_cap,
            "worker_cap": worker_cap,
            "candidate_domain": domain,
            "cp_stage_statuses": statuses,
            "cp_stage_attempt_count": len(statuses),
            "canonical_status": acceptance.get("status"),
            "winning_stage": acceptance.get("winning_stage", {}).get("stage")
                if acceptance.get("winning_stage") else None,
            "cp_stage_plan": list(round_kwargs.get('plan', ())) if not zero_keys else [],
            "resource_domain_status": "INITIAL_COMPACT" if expansion_round == 0 else "EXPANDED",
            "zero_calendar_domain_keys": list(zero_keys),
        }
        if zero_keys:
            target_rows = [row for row in _resource_audit_rows(result.get("resource_audit", ()))
                           if row["operation_key"] in zero_keys]
            next_machine = _next_cap(
                machine_cap, target_rows, "maximum_legal_machine_sets",
                "machine_alternatives_remaining", policy.growth_factor)
            next_worker = _next_cap(
                worker_cap, target_rows, "maximum_legal_worker_crews",
                "worker_alternatives_remaining", policy.growth_factor)
            can_retry = next_machine > machine_cap or next_worker > worker_cap
            if can_retry and expansion_round < policy.max_expansion_rounds \
                    and not _window_deadline_expired(state):
                record["resource_domain_status"] = "ZERO_CALENDAR_DOMAIN_EXPANDING"
                record["expansion"] = {
                    "target_operation_keys": list(zero_keys),
                    "next_machine_cap": next_machine,
                    "next_worker_cap": next_worker}
                history.append(record)
                current = result
                machine_cap, worker_cap = next_machine, next_worker
                continue
            record["resource_domain_status"] = (
                "WINDOW_RUNTIME_BUDGET_EXCEEDED" if _window_deadline_expired(state)
                else "RESOURCE_DOMAIN_EXPANSION_LIMIT" if can_retry
                else "UNRESOLVED_ZERO_CALENDAR_DOMAIN")
            history.append(record)
            trace = {"policy": asdict(policy), "rounds": history,
                     "final_machine_cap": machine_cap, "final_worker_cap": worker_cap,
                     "resource_domain_complete": _complete_configuration_domain(domain),
                     "physical_infeasibility_proven": False,
                     "status": record["resource_domain_status"]}
            result["resource_domain_v2"] = trace
            result["acceptance"]["resource_domain_v2"] = trace
            return result
        winning_status = str((acceptance.get("winning_stage") or {}).get("status", ""))
        materialization_exact = int(
            (acceptance.get("materialization") or {}).get("mismatch_count", 0)) == 0
        cp_candidate = winning_status in {"FEASIBLE", "OPTIMAL"} \
            and bool(acceptance.get("selected_schedule")) and materialization_exact
        if acceptance.get("status") == "ACCEPTED" or cp_candidate:
            # A bounded planning phenotype is validated by the scoped planning
            # adapter after this function.  The legacy full-repair acceptance
            # may legitimately be REJECTED even when CP produced an exact
            # materialized candidate (the established planning/repair seam).
            record["resource_domain_status"] = (
                "ACCEPTED" if acceptance.get("status") == "ACCEPTED"
                else "CANDIDATE_READY_FOR_SCOPED_ACCEPTANCE")
            history.append(record)
            trace = {"policy": asdict(policy), "rounds": history,
                     "final_machine_cap": machine_cap, "final_worker_cap": worker_cap,
                     "resource_domain_complete": _complete_configuration_domain(domain),
                     "solution_domain_status": (
                         "FEASIBLE_ON_RESTRICTED_DOMAIN" if not _complete_configuration_domain(domain)
                         else "FEASIBLE_ON_COMPLETE_CONFIGURATION_DOMAIN"),
                     "physical_infeasibility_proven": False}
            result["resource_domain_v2"] = trace
            result.setdefault("acceptance", {})["resource_domain_v2"] = trace
            return result
        if _window_deadline_expired(state):
            record["resource_domain_status"] = "WINDOW_RUNTIME_BUDGET_EXCEEDED"
            history.append(record)
            trace = {"policy": asdict(policy), "rounds": history,
                     "final_machine_cap": machine_cap, "final_worker_cap": worker_cap,
                     "resource_domain_complete": _complete_configuration_domain(domain),
                     "status": "WINDOW_RUNTIME_BUDGET_EXCEEDED"}
            result["resource_domain_v2"] = trace
            result.setdefault("acceptance", {})["resource_domain_v2"] = trace
            return result
        # A narrow temporal stage can have no configurations inside its
        # window even though later, wider stages build real CP models. If the
        # terminal exposed-domain stage is INFEASIBLE, that earlier
        # ZERO_DOMAIN_AFTER_WINDOW must not suppress legal resource expansion.
        # No UNKNOWN/search-limit stage qualifies, and this never proves
        # physical infeasibility of the complete domain.
        all_infeasible = (bool(statuses) and statuses[-1] == "INFEASIBLE"
                          and all(status in {"INFEASIBLE", "ZERO_DOMAIN_AFTER_WINDOW"}
                                  for status in statuses))
        if not all_infeasible or not domain["resource_domain_truncated"]:
            record["resource_domain_status"] = (
                "RESOURCE_DOMAIN_EXHAUSTED" if not domain["resource_domain_truncated"]
                else "NO_EXPANSION_NON_INFEASIBILITY")
            history.append(record)
            trace = {"policy": asdict(policy), "rounds": history,
                     "final_machine_cap": machine_cap, "final_worker_cap": worker_cap,
                     "resource_domain_complete": _complete_configuration_domain(domain)}
            result["resource_domain_v2"] = trace
            result.setdefault("acceptance", {})["resource_domain_v2"] = trace
            return result
        diagnosis = diagnose_resource_domain_pressure(
            state, result, seconds=policy.diagnostic_time_limit_seconds)
        record["diagnosis"] = diagnosis
        implicated = diagnosis.get("implicated_resources", ())
        if (not implicated and state.get('source_resource_domain') is not None
                and diagnosis.get('full_domain_probe', {}).get('status') == 'INFEASIBLE'):
            # In source-derived factory work, processing duration varies with
            # worker efficiency. Relaxing NoOverlap does not diagnose a
            # missing faster worker (or a machine/calendar alternative) that
            # could make a route fit. A still-truncated domain therefore gets
            # a *bounded trial*, never a claim of physical infeasibility.
            rows = _resource_audit_rows(result.get('resource_audit', ()))
            implicated = tuple(kind for kind, field in (
                ('MACHINE', 'machine_alternatives_remaining'),
                ('WORKER', 'worker_alternatives_remaining'))
                if any(row[field] for row in rows))
            if implicated:
                record['diagnosis_limitation'] = 'WORKER_DEPENDENT_DURATION_OR_CALENDAR_ALTERNATIVE'
        if ((diagnosis.get("status") not in {
                "RESOURCE_OVERLAP_PRESSURE", "RESOURCE_DOMAIN_INTERACTION"}
                and 'diagnosis_limitation' not in record) or not implicated):
            record["resource_domain_status"] = diagnosis.get("status", "NO_EXPANSION")
            history.append(record)
            trace = {"policy": asdict(policy), "rounds": history,
                     "final_machine_cap": machine_cap, "final_worker_cap": worker_cap,
                     "resource_domain_complete": _complete_configuration_domain(domain)}
            result["resource_domain_v2"] = trace
            result.setdefault("acceptance", {})["resource_domain_v2"] = trace
            return result
        targets = _expansion_targets(result.get("resource_audit", ()), implicated)
        rows = _resource_audit_rows(result.get("resource_audit", ()))
        target_rows = [row for row in rows if row["operation_key"] in set(targets)]
        next_machine = _next_cap(
            machine_cap, target_rows, "maximum_legal_machine_sets",
            "machine_alternatives_remaining", policy.growth_factor) \
            if "MACHINE" in implicated else machine_cap
        next_worker = _next_cap(
            worker_cap, target_rows, "maximum_legal_worker_crews",
            "worker_alternatives_remaining", policy.growth_factor) \
            if "WORKER" in implicated else worker_cap
        if next_machine == machine_cap and next_worker == worker_cap:
            record["resource_domain_status"] = "RESOURCE_DOMAIN_EXHAUSTED"
            history.append(record)
            trace = {"policy": asdict(policy), "rounds": history,
                     "final_machine_cap": machine_cap, "final_worker_cap": worker_cap,
                     "resource_domain_complete": _complete_configuration_domain(domain)}
            result["resource_domain_v2"] = trace
            result.setdefault("acceptance", {})["resource_domain_v2"] = trace
            return result
        record["expansion"] = {"implicated_resources": list(implicated),
                                "target_operation_keys": list(targets),
                                "next_machine_cap": next_machine,
                                "next_worker_cap": next_worker}
        history.append(record)
        machine_cap, worker_cap = next_machine, next_worker
        current = result
    if current is None:
        raise RuntimeError("adaptive resource-domain controller produced no result")
    last_attempt = history[-1] if history else {}
    trace = {"policy": asdict(policy), "rounds": history,
             "final_machine_cap": last_attempt.get("machine_cap", machine_cap),
             "final_worker_cap": last_attempt.get("worker_cap", worker_cap),
             "resource_domain_complete": False,
             "physical_infeasibility_proven": False,
             "status": ("WINDOW_RUNTIME_BUDGET_EXCEEDED" if _window_deadline_expired(state)
                        else "RESOURCE_DOMAIN_EXPANSION_LIMIT")}
    current["resource_domain_v2"] = trace
    current.setdefault("acceptance", {})["resource_domain_v2"] = trace
    return current
