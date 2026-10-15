"""Truthful INITIAL_PLAN / ROLLING_PLAN adapters for the canonical kernel."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta
import time
from collections.abc import Mapping

from crafter.disruptions.runtime.execution_state import FreezeParameters, classify_operations
from crafter.lns_probe.analysis import build_incumbent_index
from crafter.lns_probe.anchored_model import outside_machine_reservations, outside_worker_reservations

from .calendar_regimes import CalendarRegimeCache, generate_calendar_regimes_selected
from .resource_configs import generate_resource_configurations


@dataclass(frozen=True)
class PlanningContextV1:
    planning_context_id: str
    mode: str  # INITIAL_PLAN or ROLLING_PLAN
    factory_id: int
    planning_time_seconds: int
    window_start_seconds: int
    window_end_seconds: int
    commit_end_seconds: int
    selected_operation_keys: tuple[str, ...]
    snapshot_provenance: str
    manager_overrides: tuple = ()
    objective_contract_version: str = "CRAFT_OBJECTIVE_CONTRACT_V1"
    boundary_state_version: str | None = None
    calendar_resource_state_version: str | None = None
    previous_provisional_plan_reference: str | None = None
    # The authoritative execution snapshot is intentionally separate from the
    # rolling optimization origin.  ``planning_time_seconds`` remains the
    # backward-compatible decision epoch used by older callers; new planning
    # adapters must use this explicit snapshot value when classifying actual
    # execution state.
    snapshot_as_of_seconds: int | None = None

    def __post_init__(self):
        if self.mode not in {"INITIAL_PLAN", "ROLLING_PLAN"}:
            raise ValueError("planning context mode must be INITIAL_PLAN or ROLLING_PLAN")
        if not (self.window_start_seconds <= self.planning_time_seconds <= self.window_end_seconds):
            raise ValueError("planning time must be inside its rolling window")
        if not (self.window_start_seconds < self.commit_end_seconds <= self.window_end_seconds):
            raise ValueError("planning commit edge must be inside window")
        if self.snapshot_as_of_seconds is None:
            object.__setattr__(self, "snapshot_as_of_seconds", int(self.planning_time_seconds))
        if int(self.snapshot_as_of_seconds) < 0:
            raise ValueError("snapshot-as-of time cannot be negative")


@dataclass(frozen=True)
class BoundaryReadinessV1:
    """Auditable, authoritative admission result for one operation."""

    operation_key: str
    window_start_seconds: int
    window_end_seconds: int
    earliest_ready_seconds: int | None
    admissible_now: bool
    decision: str
    blockers: tuple[dict, ...] = ()
    provenance: dict = field(default_factory=dict)
    future_schedulable: bool = True
    is_hard_boundary: bool = False

    def as_dict(self):
        return {
            "contract_version": "BOUNDARY_READINESS_V1",
            "operation_key": self.operation_key,
            "window_start_seconds": self.window_start_seconds,
            "window_end_seconds": self.window_end_seconds,
            "earliest_ready_seconds": self.earliest_ready_seconds,
            "admissible_now": self.admissible_now,
            "decision": self.decision,
            "blockers": [dict(row) for row in self.blockers],
            "provenance": dict(self.provenance or {}),
            "future_schedulable": self.future_schedulable,
            "is_hard_boundary": self.is_hard_boundary,
        }


_MOVABLE_PREDECESSOR_STATES = {
    "FUTURE_RELEASED", "FUTURE_PLANNED", "FUTURE_OUTSIDE_WINDOW",
    "MOVABLE_WINDOW", "PROVISIONAL", "STABLE", "BOUNDARY_DEFERRED",
}
_IMMUTABLE_PREDECESSOR_STATES = {
    "COMPLETED", "IN_PROGRESS", "COMMITTED", "COMMITTED_FUTURE",
    "IMMUTABLE_FROZEN", "FROZEN", "FROZEN_NEAR_TERM",
}
_UNKNOWN_PREDECESSOR_STATES = {
    "UNKNOWN", "UNKNOWN_EXECUTION_STATE", "UNRESOLVED_EXECUTION_STATE",
}


def _mapping_value(value, *names):
    if value is None:
        return None
    for name in names:
        if isinstance(value, Mapping):
            candidate = value.get(name)
        else:
            candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return None


def _boundary_completion_seconds(boundary, operation_key):
    """Return only carried authoritative completion, never planned timing."""
    if boundary is None:
        return None, None
    routing = _mapping_value(boundary, "routing_completion_state") or {}
    fact = routing.get(str(operation_key)) if isinstance(routing, Mapping) else None
    value = _mapping_value(fact, "authoritative_end_seconds", "actual_end_seconds",
                           "completion_seconds", "authoritative_completion_seconds")
    if value is not None:
        return int(value), "boundary.routing_completion_state"
    machine_rows = _mapping_value(boundary, "machine_commitments") or ()
    ends = []
    for row in machine_rows:
        if str(_mapping_value(row, "operation_key")) == str(operation_key):
            end = _mapping_value(row, "end_seconds", "authoritative_end_seconds")
            if end is not None:
                ends.append(int(end))
    if ends:
        return max(ends), "boundary.machine_commitments"
    return None, None


def _classified_completion_seconds(item):
    """Use explicitly authoritative fields only; planned end_seconds is excluded."""
    value = _mapping_value(
        item,
        "authoritative_end_seconds", "actual_end_seconds",
        "execution_end_seconds", "authoritative_completion_seconds",
        "actual_completion_seconds", "completion_seconds",
    )
    if value is None:
        # Keep the tuple contract so callers can distinguish missing
        # authoritative execution truth from a stale planned timestamp.
        return None, None
    return int(value), "classified.authoritative_execution"


def _predecessor_keys(route_index, operation_key):
    raw = route_index.route_predecessor.get(str(operation_key))
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (str(raw),)
    return tuple(sorted(str(value) for value in raw if value is not None))


def boundary_readiness_for_operation(problem, route_index, classified, context,
                                     operation_key, *, committed_operation_keys=(),
                                     boundary=None):
    """Compute a safe lower-bound readiness result without resource heuristics.

    Only release and explicit authoritative execution/boundary completion are
    used as hard lower bounds.  Resource availability and detailed calendar
    feasibility remain exact CP/replay concerns.
    """
    key = str(operation_key)
    committed = {str(value) for value in committed_operation_keys}
    order = problem.order_by_id[problem.operation_by_key[key].order_id]
    earliest = int(order.release_seconds)
    blockers = []
    if earliest >= int(context.window_end_seconds):
        blockers.append({
            "type": "ORDER_RELEASE", "operation_key": key,
            "authoritative_end_seconds": earliest,
            "source": "problem.order_by_id.release_seconds",
        })
    visited = set()

    def walk(node):
        nonlocal earliest
        if node in visited:
            blockers.append({"type": "ROUTING_CYCLE_OR_DUPLICATE",
                             "operation_key": node,
                             "source": "route_index.route_predecessor"})
            return
        visited.add(node)
        for predecessor in _predecessor_keys(route_index, node):
            item = classified.get(predecessor)
            state = str(_mapping_value(item, "state") or "").upper()
            hard = predecessor in committed or state in _IMMUTABLE_PREDECESSOR_STATES
            if predecessor not in committed and (state in _UNKNOWN_PREDECESSOR_STATES or item is None):
                blockers.append({
                    "type": "UNKNOWN_EXECUTION_STATE",
                    "operation_key": predecessor,
                    "authoritative_end_seconds": None,
                    "source": "classified.execution_state",
                })
                continue
            if hard:
                end, source = _boundary_completion_seconds(boundary, predecessor)
                if end is None:
                    end, source = _classified_completion_seconds(item)
                if end is None:
                    blockers.append({
                        "type": "UNKNOWN_EXECUTION_STATE",
                        "operation_key": predecessor,
                        "authoritative_end_seconds": None,
                        "source": source or "missing_authoritative_completion",
                    })
                    continue
                earliest = max(earliest, int(end))
                if int(end) >= int(context.window_end_seconds):
                    if predecessor in committed or state in {"COMMITTED", "COMMITTED_FUTURE", "IMMUTABLE_FROZEN", "FROZEN"}:
                        blocker_type = "COMMITTED_PREDECESSOR_COMPLETION"
                    elif state == "COMPLETED":
                        blocker_type = "COMPLETED_PREDECESSOR_COMPLETION"
                    elif state == "IN_PROGRESS":
                        blocker_type = "IN_PROGRESS_PREDECESSOR_COMPLETION"
                    else:
                        blocker_type = "AUTHORITATIVE_PREDECESSOR_COMPLETION"
                    blockers.append({
                        "type": blocker_type,
                        "operation_key": predecessor,
                        "authoritative_end_seconds": int(end),
                        "source": source,
                    })
                # An authoritative completion already summarizes its own route.
                continue
            if state not in _MOVABLE_PREDECESSOR_STATES:
                blockers.append({
                    "type": "UNKNOWN_EXECUTION_STATE",
                    "operation_key": predecessor,
                    "authoritative_end_seconds": None,
                    "source": "classified.execution_state",
                })
                continue
            walk(predecessor)

    walk(key)
    unresolved = any(row["type"] == "UNKNOWN_EXECUTION_STATE" for row in blockers)
    deferred = any(row["type"] in {
        "ORDER_RELEASE", "COMMITTED_PREDECESSOR_COMPLETION",
        "COMPLETED_PREDECESSOR_COMPLETION",
        "IN_PROGRESS_PREDECESSOR_COMPLETION",
        "AUTHORITATIVE_PREDECESSOR_COMPLETION",
    } for row in blockers)
    if unresolved:
        decision = "UNRESOLVED_EXECUTION_STATE"
    elif deferred or earliest >= int(context.window_end_seconds):
        decision = "BOUNDARY_DEFERRED"
    else:
        decision = "ADMIT"
    return BoundaryReadinessV1(
        operation_key=key,
        window_start_seconds=int(context.window_start_seconds),
        window_end_seconds=int(context.window_end_seconds),
        earliest_ready_seconds=int(earliest) if earliest is not None else None,
        admissible_now=decision == "ADMIT",
        decision=decision,
        blockers=tuple(blockers),
        provenance={
            "contract_version": "BOUNDARY_READINESS_V1",
            "safe_lower_bounds": ["order_release", "authoritative_predecessor_completion"],
            "resource_availability_used_as_hard_bound": False,
            "window_interval": "[window_start_seconds, window_end_seconds)",
        },
        future_schedulable=True,
        is_hard_boundary=False,
    )


def select_precedence_safe_window(problem, route_index, classified, context: PlanningContextV1,
                                  *, committed_operation_keys=(), maximum_operations: int,
                                  boundary=None):
    """Select a deterministic factory-local window without ignoring routes.

    Candidates are ranked by release, effective customer deadline, and key.
    A candidate is admitted only with every uncommitted predecessor required
    to make its in-window placement meaningful.  This is deliberately a
    selection policy, not a scheduler and never freezes future work.
    """
    committed = {str(x) for x in committed_operation_keys}
    factory = int(context.factory_id)
    candidates = []
    for op in problem.operations:
        key = str(op.operation_key)
        if int(problem.order_by_id[op.order_id].factory_id) != factory or key in committed:
            continue
        life = classified.get(key, {}).get('state')
        if life in {'COMPLETED', 'IN_PROGRESS', 'COMMITTED_FUTURE', 'CANCELLED', 'EXCLUDED'}:
            continue
        order = problem.order_by_id[op.order_id]
        if int(order.release_seconds) > int(context.window_end_seconds):
            continue
        due = order.deadline_seconds if order.deadline_seconds is not None else problem.horizon_seconds
        candidates.append((int(order.release_seconds), int(due), key))
    selected, provenance = set(), []
    for _release, _due, seed in sorted(candidates):
        readiness = boundary_readiness_for_operation(
            problem, route_index, classified, context, seed,
            committed_operation_keys=committed, boundary=boundary)
        if readiness.decision != "ADMIT":
            provenance.append({
                'operation_key': seed, 'selected': False,
                'reason': readiness.decision,
                'required_predecessors': [],
                'readiness': readiness.as_dict(),
            })
            continue
        closure, stack = {seed}, [seed]
        while stack:
            key = stack.pop()
            pred = route_index.route_predecessor.get(key)
            if pred is None or str(pred) in committed:
                continue
            pred = str(pred)
            if pred not in closure:
                closure.add(pred); stack.append(pred)
        if len(selected | closure) > int(maximum_operations):
            provenance.append({'operation_key': seed, 'selected': False,
                               'reason': 'OPERATION_CAP_WITH_REQUIRED_PREDECESSORS',
                               'required_predecessors': sorted(closure - {seed}),
                               'readiness': readiness.as_dict()})
            continue
        added = sorted(closure - selected)
        selected.update(closure)
        provenance.append({'operation_key': seed, 'selected': True,
                           'reason': 'RELEASE_DUE_WITH_PRECEDENCE_CLOSURE',
                           'required_predecessors': sorted(closure - {seed}),
                           'newly_added': added,
                           'readiness': readiness.as_dict()})
        if len(selected) >= int(maximum_operations):
            break
    return tuple(sorted(selected)), tuple(provenance)


def _inject_boundary(state, boundary):
    """Inject exact carried facts into canonical reservation structures."""
    if boundary is None:
        return state
    if int(boundary.factory_id) != int(state["planning_context"]["factory_id"]):
        raise ValueError("cross-factory planning boundary is prohibited")
    machine = state["_machine_res"]; worker = state["_worker_res"]
    for row in boundary.machine_commitments:
        value = (int(row["start_seconds"]), int(row["end_seconds"]), "__COMMITTED__", str(row["operation_key"]))
        rows = machine.setdefault(int(row["machine_id"]), [])
        if value not in rows:
            rows.append(value)
    origin = state["problem"].origin
    for row in boundary.worker_commitments:
        value = (origin + timedelta(seconds=int(row["start_seconds"])),
                 origin + timedelta(seconds=int(row["end_seconds"])))
        rows = worker.setdefault(str(row["worker_id"]), [])
        if value not in rows:
            rows.append(value)
    for rows in machine.values(): rows.sort(key=lambda x: (int(x[0]), int(x[1]), str(x[3])))
    for rows in worker.values(): rows.sort()
    state["committed_boundary"] = boundary
    state["committed_routing_completion"] = dict(boundary.routing_completion_state)
    return state


def build_planning_state(loaded, context: PlanningContextV1, *, boundary=None):
    """Build an event-free canonical bounded state from one factory snapshot.

    Existing schedule rows provide authoritative completed/committed history and
    metadata only; they are not a synthetic stability reference for the first
    INITIAL_PLAN window.  The objective profile enforces that distinction.
    """
    problem, operator_index, rows, _durations, op_details, _index, baseline, checked, source = loaded
    selected = tuple(sorted(str(key) for key in context.selected_operation_keys))
    for key in selected:
        operation = problem.operation_by_key[key]
        if int(problem.order_by_id[operation.order_id].factory_id) != int(context.factory_id):
            raise ValueError(f"selected operation {key} is outside planning factory")
    after_index = build_incumbent_index(problem, rows, op_details)
    # Execution truth is classified at the authoritative snapshot epoch, not
    # at the start of the current rolling optimization window.  In particular
    # frozen benchmark planned timestamps are not evidence of completed work
    # when a later window advances.
    raw = {x["operation_key"]: x for x in classify_operations(rows, context.snapshot_as_of_seconds,
                                                                  parameters=FreezeParameters())}
    classified = {}
    committed = set(getattr(boundary, "committed_operation_keys", ()))
    for key, item in raw.items():
        state = item["state"]
        if key in selected:
            state = "MOVABLE_WINDOW"
        elif key in committed:
            state = "COMMITTED_FUTURE"
        elif state not in {"COMPLETED", "IN_PROGRESS"}:
            state = "FUTURE_OUTSIDE_WINDOW"
        classified[key] = {**item, "state": state}
    # A first initial plan must not freeze future work merely because the
    # frozen benchmark contains an old decoded schedule.  Future-outside rows
    # remain planning context (due dates / routing graph), not reservations or
    # fixed start/end boundaries.  Carried commitments replace their old
    # historical rows and are injected exactly below.
    future_outside = {key for key, item in classified.items()
                      if item['state'] == 'FUTURE_OUTSIDE_WINDOW'}
    mutable = set(selected) | committed | future_outside
    state = {
        "problem": problem, "operator_index": operator_index,
        "validation_operator_index": operator_index, "rows": rows,
        "op_details": op_details, "after_index": after_index, "baseline": baseline,
        "checked": checked, "event": None, "keys": selected,
        "classified": classified, "source": source,
        "planning_context": asdict(context),
        "snapshot_as_of_seconds": int(context.snapshot_as_of_seconds),
        "objective_profile": "INITIAL_PLAN_NO_STABILITY" if context.mode == "INITIAL_PLAN" and not context.previous_provisional_plan_reference else "ROLLING_PLAN_STABILITY",
        "_machine_res": outside_machine_reservations(problem, after_index, mutable),
        "_worker_res": outside_worker_reservations(problem, after_index, mutable),
    }
    return _inject_boundary(state, boundary)


def prepare_planning_schedule(state, *, max_machine_sets=3, max_worker_crews=3,
                              machine_pool_limit=30, cache=None,
                              allow_zero_regimes=False, regime_probe=None):
    """Prepare a bounded planning state for ``run_canonical_schedule``.

    Window selection/precedence closure must occur before this adapter.  This
    function deliberately does not invent disruption causal closure.
    """
    started = time.perf_counter(); cache = cache or CalendarRegimeCache()
    resource_started = time.perf_counter()
    configs, audit, summary = generate_resource_configurations(
        state, max_machine_sets=max_machine_sets, max_worker_crews=max_worker_crews,
        machine_pool_limit=machine_pool_limit)
    resource_seconds = time.perf_counter() - resource_started
    calendar_started = time.perf_counter()
    regimes, regime_summary, total, equivalence = generate_calendar_regimes_selected(state, configs, cache=cache)
    probes = {}
    if regime_probe is not None:
        # The probe may only ADD exact calendar breakpoints. The regime builder
        # remains authoritative and re-evaluates every proposed start exactly.
        probe_state = {**state, 'resource_configurations_for_probes': configs}
        for _ in range(len(state['keys'])):
            proposed, _ = regime_probe(
                probe_state, regimes, state.get('route_ready_committed', {}),
                state['keys'])
            combined = {key: tuple(sorted(set(probes.get(key, ())) | set(proposed.get(key, ()))))
                        for key in state['keys']}
            if combined == probes:
                break
            probes = combined
            regimes, regime_summary, total, equivalence = generate_calendar_regimes_selected(
                state, configs, cache=cache,
                additional_breakpoints_by_operation=probes)
    calendar_seconds = time.perf_counter() - calendar_started
    if not allow_zero_regimes and any(not values for values in regimes.values()):
        raise ValueError("planning window has a zero authoritative regime domain")
    total_seconds = time.perf_counter() - started
    return {"state": state, "initial_keys": tuple(state["keys"]),
            "required_window_keys": tuple(state["keys"]),
            "closure": {"iterations": [], "added_operations": [], "hard_boundaries": []},
            "resource_configurations": configs, "resource_audit": audit,
            "resource_summaries": [summary], "regimes": regimes,
            "route_ready_breakpoint_probes": probes,
            "regime_summary": regime_summary, "regime_count": total,
            "calendar_equivalence": equivalence,
            "calendar_cache_profile": cache.snapshot(), "model": None,
            "timing": {
                "resource_configuration_seconds": resource_seconds,
                "calendar_regime_generation_seconds": calendar_seconds,
                "other_preparation_seconds": max(0.0, total_seconds - resource_seconds - calendar_seconds),
                "prepare_total_seconds": total_seconds,
            }}
