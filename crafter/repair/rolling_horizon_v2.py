"""Deterministic post-admission orchestration for Rolling Horizon V2.

Step 3B deliberately sits *after* the frozen Step-3A readiness gate.  It
does not schedule, widen a CP model, or alter commitments.  It classifies the
admission result and returns an auditable request for the next orchestration
action.  The canonical solver remains the only source of schedule decisions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .objectives import active_manager_overrides
from .planning import (
    BoundaryReadinessV1,
    PlanningContextV1,
    _MOVABLE_PREDECESSOR_STATES,
    _predecessor_keys,
    boundary_readiness_for_operation,
)


ROLLING_ORCHESTRATION_CONTRACT_VERSION = "ROLLING_ORCHESTRATION_DECISION_V2"

SOLVE_READY_WORK = "SOLVE_READY_WORK"
HORIZON_EXTENSION_REQUIRED = "HORIZON_EXTENSION_REQUIRED"
NEIGHBORHOOD_EXPANSION_REQUIRED = "NEIGHBORHOOD_EXPANSION_REQUIRED"
FAST_FORWARD_TO_EVENT = "FAST_FORWARD_TO_EVENT"
NO_EXECUTABLE_WORK = "NO_EXECUTABLE_WORK"
UNRESOLVED_ROLLING_STATE = "UNRESOLVED_ROLLING_STATE"

_AUTHORITATIVE_EVENT_TYPES = {
    "COMMITTED_PREDECESSOR_COMPLETION",
    "COMPLETED_PREDECESSOR_COMPLETION",
    "IN_PROGRESS_PREDECESSOR_COMPLETION",
    "AUTHORITATIVE_PREDECESSOR_COMPLETION",
    "ORDER_RELEASE",
    "HARD_RESERVATION_END",
    "DISRUPTION_RECOVERY",
}


def apply_rolling_orchestration_decision_v2(
        decision: "RollingOrchestrationDecisionV2", *,
        normal_operation_cap: int, maximum_operation_cap: int,
        commit_step_seconds: int) -> dict[str, Any]:
    """Translate a validated Step-3B decision into bounded controller input.

    This deliberately does *not* schedule anything.  It only gives the
    rolling controller the exact next action and preserves the authoritative
    decision as provenance.  CP-SAT, replay, and validation stay downstream.
    """
    action = str(decision.decision)
    if action == SOLVE_READY_WORK:
        return {
            "action": action,
            "selected_operation_keys": tuple(decision.admitted_operation_keys),
            "window_start_seconds": int(decision.window_start_seconds),
            "window_end_seconds": int(decision.window_end_seconds),
            "commit_end_seconds": int(decision.commit_end_seconds),
        }
    if action == HORIZON_EXTENSION_REQUIRED:
        extended = decision.extended_window_end_seconds
        if extended is None or int(extended) <= int(decision.window_end_seconds):
            return {"action": UNRESOLVED_ROLLING_STATE,
                    "reason": "INVALID_HORIZON_EXTENSION_REQUEST"}
        return {
            "action": action,
            "window_start_seconds": int(decision.window_start_seconds),
            "window_end_seconds": int(extended),
            "commit_end_seconds": min(
                int(extended), int(decision.window_start_seconds) + int(commit_step_seconds)),
            "selected_operation_keys": tuple(decision.admitted_operation_keys),
        }
    if action == NEIGHBORHOOD_EXPANSION_REQUIRED:
        requested = max(int(normal_operation_cap), len(decision.trigger_operation_keys))
        for blocker in decision.trigger_blockers:
            requested = max(requested, int(blocker.get("required_minimum_closure_size", 0) or 0))
        return {
            "action": action,
            "requested_operation_cap": min(int(maximum_operation_cap), requested),
            "trigger_operation_keys": tuple(decision.trigger_operation_keys),
        }
    if action == FAST_FORWARD_TO_EVENT:
        next_start = decision.next_window_start_seconds
        if next_start is None or int(next_start) <= int(decision.window_start_seconds):
            return {"action": UNRESOLVED_ROLLING_STATE,
                    "reason": "INVALID_FAST_FORWARD_REQUEST"}
        return {"action": action, "next_window_start_seconds": int(next_start)}
    return {"action": action, "reason": decision.decision_reason}


@dataclass(frozen=True)
class RollingOrchestrationDecisionV2:
    """Complete, serialisable result of one post-admission decision."""

    window_id: str
    window_start_seconds: int
    window_end_seconds: int
    commit_end_seconds: int
    admitted_operation_keys: tuple[str, ...] = ()
    deferred_operation_keys: tuple[str, ...] = ()
    decision: str = UNRESOLVED_ROLLING_STATE
    decision_reason: str = ""
    next_window_start_seconds: int | None = None
    extended_window_end_seconds: int | None = None
    trigger_operation_keys: tuple[str, ...] = ()
    trigger_blockers: tuple[Mapping[str, Any], ...] = ()
    candidate_pool_count: int = 0
    ready_candidate_count: int = 0
    deferred_candidate_count: int = 0
    unresolved_candidate_count: int = 0
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.decision not in {
            SOLVE_READY_WORK,
            HORIZON_EXTENSION_REQUIRED,
            NEIGHBORHOOD_EXPANSION_REQUIRED,
            FAST_FORWARD_TO_EVENT,
            NO_EXECUTABLE_WORK,
            UNRESOLVED_ROLLING_STATE,
        }:
            raise ValueError(f"unknown Rolling Horizon V2 decision: {self.decision}")

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["contract_version"] = ROLLING_ORCHESTRATION_CONTRACT_VERSION
        value["admitted_operation_keys"] = list(self.admitted_operation_keys)
        value["deferred_operation_keys"] = list(self.deferred_operation_keys)
        value["trigger_operation_keys"] = list(self.trigger_operation_keys)
        value["trigger_blockers"] = [dict(row) for row in self.trigger_blockers]
        value["provenance"] = dict(self.provenance or {})
        return value


@dataclass(frozen=True)
class RollingReadinessScanV2:
    """Factory-local readiness scan used before any fast-forward decision."""

    candidate_results: tuple[BoundaryReadinessV1, ...]
    factory_id: int
    scan_scope: str = "FACTORY_LOCAL_REMAINING"

    @property
    def ready(self) -> tuple[BoundaryReadinessV1, ...]:
        return tuple(row for row in self.candidate_results if row.decision == "ADMIT")

    @property
    def deferred(self) -> tuple[BoundaryReadinessV1, ...]:
        return tuple(row for row in self.candidate_results if row.decision == "BOUNDARY_DEFERRED")

    @property
    def unresolved(self) -> tuple[BoundaryReadinessV1, ...]:
        return tuple(row for row in self.candidate_results
                     if row.decision == "UNRESOLVED_EXECUTION_STATE")

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": "ROLLING_READINESS_SCAN_V2",
            "scan_scope": self.scan_scope,
            "factory_id": self.factory_id,
            "candidate_pool_count": len(self.candidate_results),
            "ready_candidate_count": len(self.ready),
            "deferred_candidate_count": len(self.deferred),
            "unresolved_candidate_count": len(self.unresolved),
            "results": [row.as_dict() for row in self.candidate_results],
        }


@dataclass(frozen=True)
class RollingWindowInitializationV2:
    """Explicit first-window contract; there is no hidden first-window step."""

    window_start_seconds: int
    window_end_seconds: int
    commit_end_seconds: int
    lookahead_seconds: int
    commit_step_seconds: int
    factory_horizon_seconds: int

    def as_dict(self):
        return {
            "contract_version": "ROLLING_WINDOW_INITIALIZATION_V2",
            **asdict(self),
        }


def initialize_rolling_window_v2(*, epoch_start_seconds: int,
                                 factory_horizon_seconds: int,
                                 lookahead_seconds: int = 86_400,
                                 commit_step_seconds: int = 20_000) -> RollingWindowInitializationV2:
    """Create a window using the configured policy for *all* windows."""
    start = int(epoch_start_seconds)
    horizon = int(factory_horizon_seconds)
    lookahead = int(lookahead_seconds)
    step = int(commit_step_seconds)
    if lookahead <= 0 or step <= 0 or step > lookahead:
        raise ValueError("rolling initialization requires 0 < commit_step <= lookahead")
    if horizon < start:
        raise ValueError("factory horizon precedes rolling epoch")
    end = min(horizon, start + lookahead)
    commit = min(end, start + step)
    if not (start < commit <= end):
        raise ValueError("factory horizon leaves no executable rolling window")
    return RollingWindowInitializationV2(start, end, commit, lookahead, step, horizon)


def scan_factory_local_readiness_v2(problem, route_index, classified, context: PlanningContextV1,
                                    remaining_operation_keys: Iterable[str], *,
                                    committed_operation_keys=(), boundary=None) -> RollingReadinessScanV2:
    """Scan every factory-local remaining candidate before fast-forwarding.

    This is deliberately a lightweight hard-readiness scan.  It does not use
    resource availability or guessed calendar openings as rejection bounds.
    """
    committed = {str(key) for key in committed_operation_keys}
    keys = []
    for raw in remaining_operation_keys:
        key = str(raw)
        if key in committed or key not in problem.operation_by_key:
            continue
        item = classified.get(key)
        life = (item.get("state") if isinstance(item, Mapping)
                else getattr(item, "state", None))
        if str(life or "").upper() in {
            "COMPLETED", "IN_PROGRESS", "COMMITTED", "COMMITTED_FUTURE",
            "CANCELLED", "EXCLUDED",
        }:
            continue
        operation = problem.operation_by_key[key]
        order = problem.order_by_id[operation.order_id]
        if int(order.factory_id) != int(context.factory_id):
            continue
        keys.append(key)
    results = tuple(
        boundary_readiness_for_operation(
            problem, route_index, classified, context, key,
            committed_operation_keys=committed, boundary=boundary,
        )
        for key in sorted(set(keys))
    )
    return RollingReadinessScanV2(results, int(context.factory_id))


def _closure_for_operation(route_index, classified, operation_key: str,
                           committed: set[str]) -> tuple[str, ...]:
    """Return the movable predecessor closure, deterministically."""
    closure: set[str] = set()
    stack = [str(operation_key)]
    while stack:
        key = stack.pop()
        for predecessor in _predecessor_keys(route_index, key):
            if predecessor in committed or predecessor in closure:
                continue
            item = classified.get(predecessor)
            state = str((item or {}).get("state", "")).upper()
            if state in _MOVABLE_PREDECESSOR_STATES:
                closure.add(predecessor)
                stack.append(predecessor)
    return tuple(sorted(closure))


def _blocker_events(scan: RollingReadinessScanV2, *, window_start_seconds: int):
    events = {}
    for result in scan.deferred:
        for blocker in result.blockers:
            kind = str(blocker.get("type", ""))
            value = blocker.get("authoritative_end_seconds")
            if kind not in _AUTHORITATIVE_EVENT_TYPES or value is None:
                continue
            event_time = int(value)
            if event_time <= int(window_start_seconds):
                continue
            event = {
                "event_type": kind,
                "event_time_seconds": event_time,
                "operation_key": str(blocker.get("operation_key", result.operation_key)),
                "source": blocker.get("source"),
            }
            signature = (event_time, kind, event["operation_key"], str(event["source"]))
            events[signature] = event
    return tuple(sorted(events.values(), key=lambda row: (
        int(row["event_time_seconds"]), str(row["event_type"]), str(row["operation_key"]),
        str(row.get("source")),
    )))


def next_relevant_event_v2(scan: RollingReadinessScanV2, *, window_start_seconds: int,
                           authoritative_future_events: Iterable[Mapping[str, Any]] = ()):
    """Return the earliest authoritative event strictly after the epoch.

    Optional event rows are accepted only when their type is one of the
    explicitly authoritative event kinds.  In particular, this seam does not
    infer machine/worker calendar openings or resource availability.
    """
    events = list(_blocker_events(scan, window_start_seconds=window_start_seconds))
    for row in authoritative_future_events or ():
        kind = str(row.get("event_type", row.get("type", "")))
        value = row.get("event_time_seconds", row.get("authoritative_end_seconds"))
        if kind not in _AUTHORITATIVE_EVENT_TYPES or value is None:
            continue
        event_time = int(value)
        if event_time <= int(window_start_seconds):
            continue
        events.append({
            "event_type": kind,
            "event_time_seconds": event_time,
            "operation_key": str(row.get("operation_key", row.get("event_id", ""))),
            "source": row.get("source", "authoritative_future_event"),
        })
    deduped = {(int(row["event_time_seconds"]), str(row["event_type"]),
               str(row["operation_key"]), str(row.get("source"))): row for row in events}
    events = list(deduped.values())
    events.sort(key=lambda row: (int(row["event_time_seconds"]), str(row["event_type"]),
                                 str(row["operation_key"]), str(row.get("source"))))
    return events[0] if events else None


def advance_zero_commit_rolling_epoch(*, window_start_seconds: int,
                                      commit_end_seconds: int,
                                      provisional_operation_keys: Iterable[str] = ()) -> dict:
    """Advance a valid rolling epoch when no operation crosses its commit edge.

    A valid provisional plan can contain only starts at or after the current
    commit edge.  That is not physical infeasibility and must not terminate a
    run: the rolling epoch may advance to its deterministic commit edge while
    the provisional plan remains movable.  The guard is structural (strictly
    increasing epoch and non-empty provisional plan), so it cannot spin on an
    unchanged state.
    """
    start = int(window_start_seconds)
    edge = int(commit_end_seconds)
    provisional = tuple(sorted({str(key) for key in provisional_operation_keys}))
    if edge <= start:
        return {
            "action": UNRESOLVED_ROLLING_STATE,
            "reason": "ZERO_COMMIT_EDGE_DID_NOT_ADVANCE",
            "provisional_operation_keys": list(provisional),
        }
    if not provisional:
        return {
            "action": UNRESOLVED_ROLLING_STATE,
            "reason": "ZERO_COMMIT_WITHOUT_PROVISIONAL_PLAN",
            "provisional_operation_keys": [],
        }
    return {
        "action": FAST_FORWARD_TO_EVENT,
        "reason": "ZERO_COMMIT_ADVANCE_TO_ROLLING_COMMIT_EDGE",
        "next_window_start_seconds": edge,
        "provisional_operation_keys": list(provisional),
    }


def _default_extension_stages(window_start: int, window_end: int) -> tuple[int, ...]:
    width = max(1, int(window_end) - int(window_start))
    return tuple(int(window_end) + width * factor for factor in (1, 2, 4))


def _evaluate_manager_priority(problem, context, manager_overrides):
    """Evaluate overrides without ever treating malformed data as absence."""
    try:
        active = active_manager_overrides(
            manager_overrides or (),
            event_time_seconds=int(context.planning_time_seconds),
            origin=problem.origin,
        )
    except Exception as exc:  # surfaced as an unresolved orchestration state
        return {}, {
            "type": "MANAGER_PRIORITY_EVALUATION_ERROR",
            "source": "active_manager_overrides",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
    return active, None


def _urgent_deferred_keys(problem, deferred, active_manager, required_operation_keys):
    required = {str(key) for key in (required_operation_keys or ())}
    urgent = set(required)
    for result in deferred:
        operation = problem.operation_by_key.get(result.operation_key)
        if operation is None:
            continue
        override = active_manager.get(str(operation.order_id))
        if override is not None and str(override.tier).upper() in {"URGENT", "HIGH"}:
            urgent.add(result.operation_key)
    return tuple(sorted(urgent & {row.operation_key for row in deferred}))


def rolling_state_signature_v2(*, factory_id: int, remaining_operation_keys,
                              committed_operation_keys, context: PlanningContextV1,
                              scan: RollingReadinessScanV2, decision: str | None = None):
    """Canonical signature for deterministic no-progress protection."""
    blockers = []
    for row in scan.candidate_results:
        blockers.append((row.operation_key, row.decision, row.earliest_ready_seconds,
                         tuple((b.get("type"), b.get("operation_key"),
                                b.get("authoritative_end_seconds")) for b in row.blockers)))
    return (
        int(factory_id), tuple(sorted(str(key) for key in remaining_operation_keys)),
        tuple(sorted(str(key) for key in committed_operation_keys)),
        int(context.window_start_seconds), int(context.window_end_seconds),
        int(context.commit_end_seconds), tuple(blockers), decision,
    )


def no_progress_repeat_v2(previous_signatures, current_signature, *, state_changed=False) -> bool:
    """Return true only when an unchanged no-progress state would repeat."""
    if state_changed:
        return False
    return current_signature in set(previous_signatures or ())


def decide_rolling_orchestration_v2(*, window_id: str, context: PlanningContextV1,
                                    problem, route_index, classified,
                                    admitted_operation_keys=(), admission_provenance=(),
                                    remaining_operation_keys=(), committed_operation_keys=(),
                                    boundary=None, maximum_operations: int = 22,
                                    manager_overrides=(), required_operation_keys=(),
                                    temporal_extension_stages_seconds: Sequence[int] | None = None,
                                    previous_signatures=(), state_changed=False,
                                    authoritative_future_events: Iterable[Mapping[str, Any]] = ()) -> RollingOrchestrationDecisionV2:
    """Classify what should happen after Step-3A admission.

    Ready work always wins.  In particular, deferred work never triggers a
    horizon extension or fast-forward while the admission result contains
    executable operations.
    """
    scan = scan_factory_local_readiness_v2(
        problem, route_index, classified, context, remaining_operation_keys,
        committed_operation_keys=committed_operation_keys, boundary=boundary,
    )
    ready_keys = {row.operation_key for row in scan.ready}
    admitted = tuple(sorted(set(str(key) for key in admitted_operation_keys) & ready_keys))
    deferred = tuple(sorted(row.operation_key for row in scan.deferred))
    triggers = []
    blockers = []
    provenance = {
        "contract_version": ROLLING_ORCHESTRATION_CONTRACT_VERSION,
        "readiness_scan": scan.as_dict(),
        "admission_provenance": [dict(row) for row in admission_provenance],
        "fast_forward_factory_local_scan": True,
        "snapshot_as_of_seconds": context.snapshot_as_of_seconds,
    }
    active_manager, manager_error = _evaluate_manager_priority(
        problem, context, manager_overrides)
    if manager_error is not None:
        provenance["manager_priority_status"] = "ERROR"
        provenance["manager_priority_error"] = dict(manager_error)
        return RollingOrchestrationDecisionV2(
            window_id=str(window_id), window_start_seconds=int(context.window_start_seconds),
            window_end_seconds=int(context.window_end_seconds), commit_end_seconds=int(context.commit_end_seconds),
            admitted_operation_keys=admitted, deferred_operation_keys=deferred,
            decision=UNRESOLVED_ROLLING_STATE,
            decision_reason="MANAGER_PRIORITY_EVALUATION_ERROR",
            trigger_operation_keys=admitted,
            trigger_blockers=(manager_error,), candidate_pool_count=len(scan.candidate_results),
            ready_candidate_count=len(scan.ready), deferred_candidate_count=len(scan.deferred),
            unresolved_candidate_count=len(scan.unresolved), provenance=provenance,
        )

    # A selector may choose a subset of the full ready pool.  If it selected
    # anything, the normal canonical solver must proceed regardless of other
    # deferred work.
    if admitted:
        return RollingOrchestrationDecisionV2(
            window_id=str(window_id), window_start_seconds=int(context.window_start_seconds),
            window_end_seconds=int(context.window_end_seconds), commit_end_seconds=int(context.commit_end_seconds),
            admitted_operation_keys=admitted, deferred_operation_keys=deferred,
            decision=SOLVE_READY_WORK,
            decision_reason="READY_WORK_EXISTS_DEFERRED_WORK_RETAINED_IN_ACCOUNTING",
            trigger_operation_keys=tuple(sorted(row.operation_key for row in scan.ready)),
            trigger_blockers=(), candidate_pool_count=len(scan.candidate_results),
            ready_candidate_count=len(scan.ready), deferred_candidate_count=len(scan.deferred),
            unresolved_candidate_count=len(scan.unresolved), provenance=provenance,
        )

    if not scan.candidate_results:
        return RollingOrchestrationDecisionV2(
            window_id=str(window_id), window_start_seconds=int(context.window_start_seconds),
            window_end_seconds=int(context.window_end_seconds), commit_end_seconds=int(context.commit_end_seconds),
            decision=NO_EXECUTABLE_WORK, decision_reason="FACTORY_LOCAL_REMAINING_POOL_EMPTY",
            candidate_pool_count=0, provenance=provenance,
        )

    if scan.unresolved:
        for row in scan.unresolved:
            triggers.append(row.operation_key)
            blockers.extend(dict(item) for item in row.blockers)
        signature = rolling_state_signature_v2(
            factory_id=context.factory_id, remaining_operation_keys=remaining_operation_keys,
            committed_operation_keys=committed_operation_keys, context=context, scan=scan,
            decision=UNRESOLVED_ROLLING_STATE)
        repeated = no_progress_repeat_v2(previous_signatures, signature, state_changed=state_changed)
        provenance["state_signature"] = repr(signature)
        provenance["no_progress_repeat"] = repeated
        return RollingOrchestrationDecisionV2(
            window_id=str(window_id), window_start_seconds=int(context.window_start_seconds),
            window_end_seconds=int(context.window_end_seconds), commit_end_seconds=int(context.commit_end_seconds),
            deferred_operation_keys=deferred,
            decision=UNRESOLVED_ROLLING_STATE,
            decision_reason="UNKNOWN_EXECUTION_STATE_BLOCKS_SAFE_PROGRESS" if not repeated else "REPEATED_NO_PROGRESS_STATE",
            trigger_operation_keys=tuple(sorted(set(triggers))),
            trigger_blockers=tuple(sorted(blockers, key=lambda x: (str(x.get("type")), str(x.get("operation_key"))))),
            candidate_pool_count=len(scan.candidate_results), ready_candidate_count=len(scan.ready),
            deferred_candidate_count=len(scan.deferred), unresolved_candidate_count=len(scan.unresolved),
            provenance=provenance,
        )

    # No ready operation was admitted, but ready work exists in the factory
    # local scan.  The only safe response is a bounded-neighborhood request.
    if scan.ready:
        committed = {str(key) for key in committed_operation_keys}
        selected_ready: set[str] = set()
        for row in scan.ready:
            closure = _closure_for_operation(route_index, classified, row.operation_key, committed)
            candidate_closure = set(closure) | {row.operation_key}
            if len(selected_ready | candidate_closure) <= int(maximum_operations):
                selected_ready.update(candidate_closure)
            else:
                triggers.append(row.operation_key)
                blockers.append({
                    "type": "MOVABLE_PREDECESSOR_CLOSURE",
                    "operation_key": row.operation_key,
                    "movable_predecessors": list(closure),
                    "required_minimum_closure_size": len(selected_ready | candidate_closure),
                    "current_operation_cap": int(maximum_operations),
                    "source": "route_index.route_predecessor",
                })
        # If a deterministic closure fits, the orchestration layer can keep
        # ready work moving even when a caller supplied no selected subset.
        if selected_ready:
            return RollingOrchestrationDecisionV2(
                window_id=str(window_id), window_start_seconds=int(context.window_start_seconds),
                window_end_seconds=int(context.window_end_seconds), commit_end_seconds=int(context.commit_end_seconds),
                admitted_operation_keys=tuple(sorted(selected_ready)), deferred_operation_keys=deferred,
                decision=SOLVE_READY_WORK,
                decision_reason="FACTORY_LOCAL_READY_CLOSURE_FITS_CURRENT_NEIGHBORHOOD",
                trigger_operation_keys=tuple(sorted(selected_ready)),
                candidate_pool_count=len(scan.candidate_results), ready_candidate_count=len(scan.ready),
                deferred_candidate_count=len(scan.deferred), unresolved_candidate_count=len(scan.unresolved),
                provenance=provenance,
            )
        if not blockers:
            blockers.append({
                "type": "READY_POOL_NOT_ADMITTED",
                "ready_operation_keys": [row.operation_key for row in scan.ready],
                "current_operation_cap": int(maximum_operations),
                "source": "factory_local_readiness_scan",
            })
        return RollingOrchestrationDecisionV2(
            window_id=str(window_id), window_start_seconds=int(context.window_start_seconds),
            window_end_seconds=int(context.window_end_seconds), commit_end_seconds=int(context.commit_end_seconds),
            deferred_operation_keys=deferred, decision=NEIGHBORHOOD_EXPANSION_REQUIRED,
            decision_reason="READY_FACTORY_LOCAL_WORK_REQUIRES_UNSELECTED_NEIGHBORHOOD",
            trigger_operation_keys=tuple(sorted(set(triggers) or {row.operation_key for row in scan.ready})),
            trigger_blockers=tuple(sorted(blockers, key=lambda x: (str(x.get("type")), str(x.get("operation_key"))))),
            candidate_pool_count=len(scan.candidate_results), ready_candidate_count=len(scan.ready),
            deferred_candidate_count=len(scan.deferred), unresolved_candidate_count=len(scan.unresolved),
            provenance=provenance,
        )

    urgent = _urgent_deferred_keys(problem, scan.deferred, active_manager, required_operation_keys)
    if urgent:
        required_ready = max(
            int(row.earliest_ready_seconds)
            for row in scan.deferred if row.operation_key in set(urgent)
            if row.earliest_ready_seconds is not None
        )
        stages = tuple(sorted(set(int(value) for value in (
            temporal_extension_stages_seconds
            if temporal_extension_stages_seconds is not None
            else _default_extension_stages(context.window_start_seconds, context.window_end_seconds)
        ) if int(value) > int(context.window_end_seconds))))
        chosen = next((value for value in stages if value >= required_ready), None)
        blockers = [dict(item) for row in scan.deferred if row.operation_key in set(urgent)
                     for item in row.blockers]
        provenance["temporal_extension_stages_seconds"] = list(stages)
        provenance["minimum_required_extension_seconds"] = max(0, required_ready - int(context.window_end_seconds))
        if chosen is not None:
            return RollingOrchestrationDecisionV2(
                window_id=str(window_id), window_start_seconds=int(context.window_start_seconds),
                window_end_seconds=int(context.window_end_seconds), commit_end_seconds=int(context.commit_end_seconds),
                deferred_operation_keys=deferred, decision=HORIZON_EXTENSION_REQUIRED,
                decision_reason="URGENT_OR_REQUIRED_HARD_BOUNDARY_EXCEEDS_WINDOW",
                extended_window_end_seconds=int(chosen), trigger_operation_keys=urgent,
                trigger_blockers=tuple(sorted(blockers, key=lambda x: (str(x.get("type")), str(x.get("operation_key"))))),
                candidate_pool_count=len(scan.candidate_results), ready_candidate_count=0,
                deferred_candidate_count=len(scan.deferred), unresolved_candidate_count=0,
                provenance=provenance,
            )
        return RollingOrchestrationDecisionV2(
            window_id=str(window_id), window_start_seconds=int(context.window_start_seconds),
            window_end_seconds=int(context.window_end_seconds), commit_end_seconds=int(context.commit_end_seconds),
            deferred_operation_keys=deferred, decision=UNRESOLVED_ROLLING_STATE,
            decision_reason="URGENT_HARD_BOUNDARY_EXCEEDS_CONFIGURED_TEMPORAL_STAGES",
            trigger_operation_keys=urgent, trigger_blockers=tuple(blockers),
            candidate_pool_count=len(scan.candidate_results), deferred_candidate_count=len(scan.deferred),
            provenance=provenance,
        )

    event = next_relevant_event_v2(
        scan, window_start_seconds=context.window_start_seconds,
        authoritative_future_events=authoritative_future_events)
    if event is not None:
        provenance["next_relevant_event"] = dict(event)
        return RollingOrchestrationDecisionV2(
            window_id=str(window_id), window_start_seconds=int(context.window_start_seconds),
            window_end_seconds=int(context.window_end_seconds), commit_end_seconds=int(context.commit_end_seconds),
            deferred_operation_keys=deferred, decision=FAST_FORWARD_TO_EVENT,
            decision_reason="NO_FACTORY_LOCAL_READY_WORK_AUTHORITATIVE_EVENT_EXISTS",
            next_window_start_seconds=int(event["event_time_seconds"]),
            trigger_operation_keys=tuple(sorted(row.operation_key for row in scan.deferred)),
            trigger_blockers=tuple(dict(row) for row in scan.deferred for row in row.blockers),
            candidate_pool_count=len(scan.candidate_results), ready_candidate_count=0,
            deferred_candidate_count=len(scan.deferred), unresolved_candidate_count=0,
            provenance=provenance,
        )

    return RollingOrchestrationDecisionV2(
        window_id=str(window_id), window_start_seconds=int(context.window_start_seconds),
        window_end_seconds=int(context.window_end_seconds), commit_end_seconds=int(context.commit_end_seconds),
        deferred_operation_keys=deferred, decision=UNRESOLVED_ROLLING_STATE,
        decision_reason="NO_READY_WORK_WITHOUT_FINITE_AUTHORITATIVE_EVENT",
        trigger_operation_keys=tuple(sorted(row.operation_key for row in scan.deferred)),
        trigger_blockers=tuple(dict(row) for row in scan.deferred for row in row.blockers),
        candidate_pool_count=len(scan.candidate_results), ready_candidate_count=0,
        deferred_candidate_count=len(scan.deferred), unresolved_candidate_count=0,
        provenance=provenance,
    )
