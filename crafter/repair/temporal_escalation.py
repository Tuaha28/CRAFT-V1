"""Deterministic temporal-stage escalation for bounded planning.

Resource-Domain V2 answers a resource question: whether a compact legal
resource domain is enough for the canonical stage sequence.  It deliberately
does not widen time.  This adapter addresses the separate case observed in
the 172-operation stress run where the exact currently exposed domain is
feasible but every bounded canonical stage is infeasible.

No physical constraint is disabled here.  Each retry rebuilds the unchanged
canonical model with a wider legal start-time region and must still produce a
materialization that is accepted by replay and the independent validator.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .resource_domain_v2 import (ResourceDomainPolicyV2,
                                 _window_deadline_expired,
                                 run_adaptive_planning_schedule)


DEFAULT_CANONICAL_PLAN = (
    (1, None, 30),
    (2, 7, 180),
    (3, 7, 240),
    (999, 14, 300),
)


@dataclass(frozen=True)
class TemporalEscalationPolicyV3:
    """Finite, auditable temporal retries.

    The first plan is the validated historical CRAFT plan.  Later plans only
    widen the legal time region; they never change the resource domain or
    remove routing/calendar/no-overlap constraints.
    """

    plans: tuple[tuple[tuple[int, int | None, float], ...], ...] = (
        DEFAULT_CANONICAL_PLAN,
        (
            (1, None, 30),
            (2, 14, 180),
            (3, 28, 240),
            (999, None, 300),
        ),
        (
            (1, None, 30),
            (2, 28, 180),
            (3, 56, 240),
            (999, None, 300),
        ),
    )
    unrestricted_final_stage_after_attempt: int = 2

    def __post_init__(self):
        if not self.plans:
            raise ValueError("temporal escalation requires at least one plan")
        for plan in self.plans:
            if not plan:
                raise ValueError("temporal escalation plans cannot be empty")
            for limit, days, seconds in plan:
                if int(limit) < 1 or float(seconds) <= 0:
                    raise ValueError("temporal stage limits and budgets must be positive")
                if days is not None and int(days) <= 0:
                    raise ValueError("temporal window days must be positive or None")
        if int(self.unrestricted_final_stage_after_attempt) < 1:
            raise ValueError("unrestricted stage attempt must be positive")


def _candidate_ready(result: Mapping[str, Any]) -> bool:
    acceptance = result.get("acceptance", {}) or {}
    winning = acceptance.get("winning_stage") or {}
    return bool(
        acceptance.get("status") == "ACCEPTED"
        or (
            str(winning.get("status", "")) in {"FEASIBLE", "OPTIMAL"}
            and bool(acceptance.get("selected_schedule"))
            and int((acceptance.get("materialization") or {}).get("mismatch_count", 0)) == 0
        )
    )


def _retryable_temporal_failure(result: Mapping[str, Any]) -> bool:
    """Return true only for the known bounded-stage/horizon seam.

    A truncated resource-domain failure, a routing failure, or a model error is
    not silently reclassified as temporal.  V2's diagnosis is the authority
    for this narrow hand-off.
    """
    trace = result.get("resource_domain_v2", {}) or {}
    rounds = trace.get("rounds", ()) or ()
    if not rounds:
        return False
    statuses = [str(x) for x in rounds[-1].get("cp_stage_statuses", ())]
    diagnosis = rounds[-1].get("diagnosis", {}) or {}
    full_feasible = diagnosis.get("full_domain_probe", {}).get("feasible") is True
    status = str(diagnosis.get("status", ""))
    round_status = str(rounds[-1].get("resource_domain_status", ""))
    return bool(
        full_feasible
        and (
            round_status == "CANONICAL_STAGE_BOUND_OR_SEARCH_LIMIT"
            or status == "CANONICAL_STAGE_BOUND_OR_SEARCH_LIMIT"
            or (not status and statuses and all(x == "INFEASIBLE" for x in statuses))
        )
    )


def run_temporal_escalated_planning_schedule(
    state,
    *,
    cache=None,
    resource_domain_policy: ResourceDomainPolicyV2 | None = None,
    temporal_policy: TemporalEscalationPolicyV3 | None = None,
    objective_mode=False,
    manager_overrides=(),
    objective_time_limit_seconds=30,
    canonical_kwargs=None,
):
    """Run V2 at finite widening temporal stages.

    The first validated candidate is returned immediately.  A later candidate
    is never accepted merely because CP-SAT found it: the existing planning
    scoped replay/validator remains the caller's acceptance gate.
    """
    policy = temporal_policy or TemporalEscalationPolicyV3()
    canonical_kwargs = dict(canonical_kwargs or {})
    attempts = []
    last = None
    for attempt, plan in enumerate(policy.plans, 1):
        if last is not None and _window_deadline_expired(state):
            break
        kwargs = dict(canonical_kwargs)
        kwargs["plan"] = tuple(plan)
        kwargs["unrestricted_final_stage"] = bool(
            attempt >= int(policy.unrestricted_final_stage_after_attempt))
        result = run_adaptive_planning_schedule(
            state,
            cache=cache,
            policy=resource_domain_policy or ResourceDomainPolicyV2(),
            objective_mode=objective_mode,
            manager_overrides=manager_overrides,
            objective_time_limit_seconds=objective_time_limit_seconds,
            canonical_kwargs=kwargs,
        )
        last = result
        trace = result.get("resource_domain_v2", {}) or {}
        rounds = trace.get("rounds", ()) or ()
        attempts.append({
            "attempt": attempt,
            "plan": [list(row) for row in plan],
            "resource_domain_status": trace.get("status") or (
                rounds[-1].get("resource_domain_status") if rounds else None),
            "resource_domain_round_count": len(rounds),
            "canonical_stage_statuses": list(
                (result.get("acceptance", {}) or {}).get("stages", ()) or ()
            ),
            "candidate_ready": _candidate_ready(result),
            "retryable_temporal_failure": _retryable_temporal_failure(result),
        })
        if _window_deadline_expired(state):
            trace = result.get("temporal_escalation", {}) or {}
            result["temporal_escalation"] = {
                "policy": asdict(policy), "status": "WINDOW_RUNTIME_BUDGET_EXCEEDED",
                "attempts": attempts, "winning_attempt": None,
            }
            return result
        if _candidate_ready(result):
            result["temporal_escalation"] = {
                "policy": asdict(policy),
                "status": "CANDIDATE_READY",
                "attempts": attempts,
                "winning_attempt": attempt,
            }
            return result
        if not _retryable_temporal_failure(result):
            break
    if last is None:
        raise RuntimeError("temporal controller produced no canonical attempt")
    last["temporal_escalation"] = {
        "policy": asdict(policy),
        "status": "TEMPORAL_STAGE_ESCALATION_LIMIT",
        "attempts": attempts,
        "winning_attempt": None,
    }
    return last
