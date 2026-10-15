"""Optimization-aware, strict-superset resource-domain expansion.

V2 remains the feasibility safety foundation.  V3 only adds alternatives
after a validated incumbent exists and never replaces a valid incumbent with a
failed, unknown, timeout, replay-failing, or non-improving solve.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import time

from .calendar_regimes import generate_calendar_regimes
from .planning import prepare_planning_schedule
from .resource_configs import expand_resource_configurations
from .resource_domain_v2 import (ResourceDomainPolicyV2,
                                 _window_deadline_expired,
                                 summarize_resource_domain)
from .solve import run_canonical_schedule
from .temporal_escalation import (
    TemporalEscalationPolicyV3,
    run_temporal_escalated_planning_schedule,
)


@dataclass(frozen=True)
class ResourceDomainPolicyV3:
    """Deterministic quality-search budget layered over V2."""

    initial_machine_sets: int = 1
    initial_worker_crews: int = 1
    growth_factor: int = 2
    max_expansion_rounds: int = 2
    max_expansion_operations: int = 8
    machine_pool_limit: int = 30
    diagnostic_time_limit_seconds: float = 15.0
    max_resource_configurations: int = 2000
    max_calendar_regimes: int = 20000

    def __post_init__(self):
        if self.initial_machine_sets < 1 or self.initial_worker_crews < 1:
            raise ValueError("initial resource-domain caps must be positive")
        if self.growth_factor < 2:
            raise ValueError("growth_factor must be at least two")
        if self.max_expansion_rounds < 0 or self.max_expansion_operations < 1:
            raise ValueError("V3 expansion budgets must be non-negative/positive")


def _objective_vector(result):
    stages = ((result.get("acceptance", {}) or {}).get("objective", {}) or {}).get("stages", ())
    values=[]
    for row in stages or ():
        if row.get("value") is None:
            return None
        values.append(int(row["value"]))
    return tuple(values) if values else None


def _incumbent_quality_diagnostics(result):
    """Derive auditable schedule-aware signals for candidate targeting.

    These signals only prioritize which hidden legal alternatives are exposed;
    they never replace the canonical lexicographic objective.  Missing due or
    graph data is represented explicitly rather than guessed.
    """
    state = result.get("state") or {}
    problem = state.get("problem")
    after = state.get("after_index")
    selected = {str(row.get("operation_key")): row
                for row in ((result.get("acceptance", {}) or {}).get("selected_schedule", ()) or ())}
    if problem is None:
        return ()
    resource_users = {}
    for key, row in selected.items():
        cfg = row.get("resource_configuration") or {}
        for machine in cfg.get("machine_ids", ()):
            resource_users.setdefault(("machine", str(machine)), set()).add(key)
        for worker in cfg.get("worker_ids", ()):
            resource_users.setdefault(("worker", str(worker)), set()).add(key)
    diagnostics = []
    for key, row in selected.items():
        op = problem.operation_by_key.get(key)
        if op is None:
            continue
        order = problem.order_by_id.get(op.order_id)
        release = int(getattr(order, "release_seconds", 0) or 0)
        due_value = getattr(order, "deadline_seconds", None)
        due = int(due_value) if due_value is not None else None
        predecessor_end = 0
        predecessor = after.route_predecessor.get(key) if after is not None else None
        if predecessor:
            predecessor_row = selected.get(str(predecessor))
            if predecessor_row:
                predecessor_end = int(predecessor_row.get("end_seconds", 0))
        ready = max(release, predecessor_end)
        start = int(row.get("start_seconds", 0))
        end = int(row.get("end_seconds", start))
        waiting = max(0, start - ready)
        tardiness = max(0, end - due) if due is not None else 0
        cfg = row.get("resource_configuration") or {}
        contention = sum(max(0, len(resource_users.get((kind, str(resource)), ())) - 1)
                         for kind, values in (("machine", cfg.get("machine_ids", ())),
                                              ("worker", cfg.get("worker_ids", ())))
                         for resource in values)
        downstream = 0
        cursor = key
        seen = set()
        while after is not None and cursor not in seen:
            seen.add(cursor)
            successor = after.route_successor.get(cursor)
            if not successor:
                break
            successors = (successor,) if isinstance(successor, str) else tuple(successor)
            downstream += len(successors)
            cursor = str(successors[0]) if successors else cursor
        diagnostics.append({
            "operation_key": key,
            "release_ready_seconds": release,
            "predecessor_ready_seconds": predecessor_end,
            "calendar_ready_seconds": ready,
            "actual_start_seconds": start,
            "actual_end_seconds": end,
            "waiting_seconds": waiting,
            "due_seconds": due,
            "due_slack_seconds": None if due is None else due - end,
            "tardiness_seconds": tardiness,
            "manager_urgency": "UNSPECIFIED",
            "resource_contention": contention,
            "downstream_criticality": downstream,
            "resource_signal_provenance": "selected_schedule_and_route_graph",
        })
    return tuple(sorted(diagnostics, key=lambda row: row["operation_key"]))


def _candidate_quality_targets(result, limit):
    """Transparent schedule-aware target ordering; no opaque weighted score."""
    acceptance = result.get("acceptance", {}) or {}
    by_key = {str(row.get("operation_key")): row
              for row in acceptance.get("selected_schedule", ()) or ()}
    diagnostic_rows = {str(row["operation_key"]): row
                       for row in _incumbent_quality_diagnostics(result)}
    rows=[]
    for item in (result.get("resource_audit", ()) or ()):
        key=str(item.get("operation_key"))
        if not item.get("truncated"):
            continue
        selected=by_key.get(key, {})
        diagnostic = diagnostic_rows.get(key, {})
        rows.append((
            -int(diagnostic.get("tardiness_seconds", 0) or 0),
            -int(diagnostic.get("waiting_seconds", 0) or 0),
            -int(diagnostic.get("resource_contention", 0) or 0),
            -int(diagnostic.get("downstream_criticality", 0) or 0),
            0 if selected else 1,
            -int(item.get("total_resource_configurations", 0)),
            int(selected.get("start_seconds", 0)),
            key,
        ))
    return tuple(row[-1] for row in sorted(rows)[:int(limit)])


def _merge_audit(old, new):
    by_key={str(row.get("operation_key")): row for row in (old or ())}
    by_key.update({str(row.get("operation_key")): row for row in (new or ())})
    return tuple(by_key[key] for key in sorted(by_key))


def run_v3_planning_schedule(
    state,
    *,
    cache=None,
    policy: ResourceDomainPolicyV3 | None = None,
    temporal_policy: TemporalEscalationPolicyV3 | None = None,
    objective_mode=False,
    manager_overrides=(),
    objective_time_limit_seconds=30,
    incumbent_validator=None,
):
    """Obtain a compact incumbent, then search strict supersets.

    ``incumbent_validator`` is deliberately explicit.  In production it must
    run the same planning scoped replay/physical validator used by the window
    acceptance adapter.  Omitting it is safe but yields a feasibility-only
    result and performs no quality expansion.
    """
    policy = policy or ResourceDomainPolicyV3()
    v2_policy = ResourceDomainPolicyV2(
        initial_machine_sets=policy.initial_machine_sets,
        initial_worker_crews=policy.initial_worker_crews,
        growth_factor=policy.growth_factor,
        max_expansion_rounds=policy.max_expansion_rounds,
        machine_pool_limit=policy.machine_pool_limit,
        diagnostic_time_limit_seconds=policy.diagnostic_time_limit_seconds,
    )
    started=time.perf_counter()
    incumbent = run_temporal_escalated_planning_schedule(
        state, cache=cache, resource_domain_policy=v2_policy,
        temporal_policy=temporal_policy, objective_mode=objective_mode,
        manager_overrides=manager_overrides,
        objective_time_limit_seconds=objective_time_limit_seconds)
    history=[]
    if _window_deadline_expired(state):
        incumbent["resource_domain_v3"] = {
            "status": "WINDOW_RUNTIME_BUDGET_EXCEEDED",
            "rounds": history, "policy": asdict(policy),
            "domain_truncated_not_infeasible": True,
        }
        return incumbent
    if not ((incumbent.get("acceptance", {}) or {}).get("selected_schedule")):
        # A compact domain can fail before an incumbent exists.  If V2 could
        # not implicate a dimension, this is still not proof of infeasibility:
        # expose one deterministic worker/machine tier at a time, recording
        # the result as unresolved truncated-domain recovery.
        current=incumbent
        for recovery_round in range(1, int(policy.max_expansion_rounds)+1):
            if _window_deadline_expired(state):
                break
            trace = current.get("resource_domain_v2", {}) or {}
            rounds = trace.get("rounds", ()) or ()
            if not rounds:
                break
            domain = rounds[-1].get("candidate_domain", {}) or {}
            worker_remaining = bool(domain.get("unused_worker_alternatives"))
            machine_remaining = bool(domain.get("unused_machine_alternatives"))
            if not worker_remaining and not machine_remaining:
                break
            # Prefer the dimension with the larger number of remaining
            # operation targets; ties go to workers because worker calendars
            # are the common source of compact-domain contention in this data.
            if worker_remaining:
                next_worker = policy.initial_worker_crews + recovery_round
                next_machine = policy.initial_machine_sets
                reason="TRUNCATED_DOMAIN_UNRESOLVED_WORKER_PROBE"
            else:
                next_worker = policy.initial_worker_crews
                next_machine = policy.initial_machine_sets + recovery_round
                reason="TRUNCATED_DOMAIN_UNRESOLVED_MACHINE_PROBE"
            from .resource_domain_v2 import run_adaptive_planning_schedule
            retry_policy=ResourceDomainPolicyV2(
                initial_machine_sets=next_machine,
                initial_worker_crews=next_worker,
                growth_factor=policy.growth_factor,
                max_expansion_rounds=0,
                machine_pool_limit=policy.machine_pool_limit,
                diagnostic_time_limit_seconds=policy.diagnostic_time_limit_seconds)
            current=run_temporal_escalated_planning_schedule(
                state, cache=cache, resource_domain_policy=retry_policy,
                temporal_policy=temporal_policy, objective_mode=objective_mode,
                manager_overrides=manager_overrides,
                objective_time_limit_seconds=objective_time_limit_seconds)
            history.append({"round": recovery_round, "reason": reason,
                            "initial_machine_sets": next_machine,
                            "initial_worker_crews": next_worker,
                            "candidate_ready": bool((current.get("acceptance", {}) or {}).get("selected_schedule")),
                            "resource_domain_v2": current.get("resource_domain_v2"),
                            "temporal_escalation": current.get("temporal_escalation")})
            if (current.get("acceptance", {}) or {}).get("selected_schedule"):
                incumbent=current
                break
        if not ((incumbent.get("acceptance", {}) or {}).get("selected_schedule")):
            incumbent["resource_domain_v3"]={"status":"NO_FEASIBLE_INCUMBENT", "rounds":history,
                                              "policy":asdict(policy),
                                              "domain_truncated_not_infeasible":True}
            return incumbent
    incumbent_valid = bool(incumbent_validator(incumbent)) if incumbent_validator else False
    if not incumbent_valid:
        incumbent["resource_domain_v3"]={
            "status":"INCUMBENT_REQUIRES_SCOPED_ACCEPTANCE",
            "rounds":history, "policy":asdict(policy),
            "elapsed_seconds":time.perf_counter()-started,
        }
        return incumbent
    incumbent_vector=_objective_vector(incumbent)
    current=incumbent
    quality_diagnostics = _incumbent_quality_diagnostics(current)
    machine_cap=policy.initial_machine_sets
    worker_cap=policy.initial_worker_crews
    for expansion_round in range(1, int(policy.max_expansion_rounds)+1):
        if _window_deadline_expired(state):
            break
        targets=_candidate_quality_targets(current, policy.max_expansion_operations)
        if not targets:
            break
        audit_rows={str(x.get("operation_key")): x for x in current.get("resource_audit", ())}
        machine_more=any(any(bool(r.get("more_machine_sets_available"))
                           for r in audit_rows[key].get("domain_rows", ())) for key in targets)
        worker_more=any(any(bool(r.get("more_worker_crews_available"))
                          for r in audit_rows[key].get("domain_rows", ())) for key in targets)
        next_machine=machine_cap + (1 if machine_more else 0)
        next_worker=worker_cap + (1 if worker_more else 0)
        if next_machine == machine_cap and next_worker == worker_cap:
            break
        state_obj=current["state"]
        current_configs=current["resource_configurations"]
        expanded, new_audit, _summary=expand_resource_configurations(
            state_obj, current_configs, targets,
            max_machine_sets=next_machine, max_worker_crews=next_worker,
            machine_pool_limit=policy.machine_pool_limit)
        strict_superset=any(
            len(expanded[key]) > len(current_configs.get(key, ()))
            for key in expanded)
        prepared=dict(current)
        prepared["resource_configurations"]=expanded
        prepared["resource_audit"]=_merge_audit(current.get("resource_audit", ()), new_audit)
        prep_started=time.perf_counter()
        regimes, summary, total, equivalence=generate_calendar_regimes(
            state_obj, expanded, cache=cache)
        prep_seconds=time.perf_counter()-prep_started
        config_count=sum(len(v) for v in expanded.values())
        trace={"round":expansion_round, "target_operation_keys":list(targets),
               "next_machine_cap":next_machine, "next_worker_cap":next_worker,
               "strict_superset":strict_superset, "resource_configuration_count":config_count,
               "calendar_regime_count":total, "calendar_generation_seconds":prep_seconds,
               "resource_domain_complete":not summarize_resource_domain(prepared["resource_audit"])["resource_domain_truncated"],
               "target_signals": [row for row in quality_diagnostics
                                  if row.get("operation_key") in set(targets)]}
        if not strict_superset or config_count > policy.max_resource_configurations or total > policy.max_calendar_regimes:
            trace["status"]="COMPLEXITY_BUDGET_REACHED"
            history.append(trace)
            break
        prepared.update({"regimes":regimes, "regime_summary":summary, "regime_count":total,
                         "calendar_equivalence":equivalence,
                         "timing":{**(current.get("timing") or {}),
                                   "v3_expansion_calendar_seconds":prep_seconds}})
        plan_attempts=(current.get("temporal_escalation", {}) or {}).get("attempts", ())
        if plan_attempts:
            plan=[tuple(row) for row in plan_attempts[-1].get("plan", ())]
        else:
            plan=TemporalEscalationPolicyV3().plans[0]
        candidate=run_canonical_schedule(
            prepared, plan=tuple(plan), objective_mode=objective_mode,
            manager_overrides=manager_overrides,
            objective_time_limit_seconds=objective_time_limit_seconds,
            incumbent_selected_schedule=(current.get("acceptance", {}) or {}).get("selected_schedule", ()))
        candidate_valid=bool(incumbent_validator(candidate)) if incumbent_validator else False
        new_vector=_objective_vector(candidate)
        improved=(candidate_valid and bool(new_vector is not None and incumbent_vector is not None
                                            and new_vector < incumbent_vector))
        trace.update({"candidate_valid":candidate_valid, "objective_vector":new_vector,
                      "incumbent_objective_vector":incumbent_vector,
                      "strict_improvement":improved,
                      "canonical_status":(candidate.get("acceptance", {}) or {}).get("status"),
                      "winning_stage":(candidate.get("acceptance", {}) or {}).get("winning_stage")})
        history.append(trace)
        if improved:
            current=candidate; incumbent_vector=new_vector
            machine_cap,worker_cap=next_machine,next_worker
        else:
            break
    current["resource_domain_v3"]={
        "status":"BEST_WITHIN_BUDGET" if history else "FEASIBLE_ONLY",
        "policy":asdict(policy), "rounds":history,
        "incumbent_objective_vector":incumbent_vector,
        "incumbent_quality_diagnostics": quality_diagnostics,
        "elapsed_seconds":time.perf_counter()-started,
    }
    return current
