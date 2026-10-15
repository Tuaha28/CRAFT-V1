"""Factory-scoped rolling orchestration over the canonical CRAFT kernel.

This module owns window sequencing and exact boundary extraction only. Every
window delegates its already-prepared bounded state to the shared canonical
kernel and must pass canonical materialization, replay, and validation before
it can be carried forward.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .solve import run_canonical_schedule
from .window_phenotype import build_window_phenotype
from .scoped_validation import validate_planning_window


def factory_future_operation_keys(state, factory_id=None):
    """Return only FUTURE_OUTSIDE keys belonging to the planning factory."""
    problem = state.get('problem')
    context = state.get('planning_context', {})
    target_factory = int(factory_id if factory_id is not None else context.get('factory_id'))
    if problem is None:
        return ()
    keys = []
    for key, value in state.get('classified', {}).items():
        if value.get('state') != 'FUTURE_OUTSIDE_WINDOW':
            continue
        operation = problem.operation_by_key.get(str(key))
        if operation is None:
            continue
        order = problem.order_by_id[operation.order_id]
        if int(order.factory_id) == target_factory:
            keys.append(str(key))
    return tuple(sorted(keys))


@dataclass(frozen=True)
class RollingWindowPolicy:
    lookahead_seconds: int
    commit_seconds: int
    maximum_operations: int
    minimum_operations: int = 1
    cp_time_budget_seconds: int = 300
    objective_time_budget_seconds: int = 30
    seed: int = 42

    def __post_init__(self):
        if not (self.lookahead_seconds > 0 and 0 < self.commit_seconds <= self.lookahead_seconds):
            raise ValueError("rolling policy requires 0 < commit_seconds <= lookahead_seconds")
        if not (self.maximum_operations >= self.minimum_operations >= 1):
            raise ValueError("rolling policy operation bounds are invalid")


@dataclass(frozen=True)
class RollingWindow:
    factory_id: int
    start_seconds: int
    end_seconds: int
    commit_end_seconds: int
    window_id: str

    def __post_init__(self):
        if not (self.start_seconds < self.commit_end_seconds <= self.end_seconds):
            raise ValueError("rolling window requires start < commit_end <= end")


@dataclass(frozen=True)
class WindowBoundaryState:
    """Exact carried state required by a later window, never a free-time proxy."""
    factory_id: int
    committed_operation_keys: tuple[str, ...]
    cross_boundary_operation_keys: tuple[str, ...]
    machine_commitments: tuple[Mapping[str, Any], ...]
    worker_commitments: tuple[Mapping[str, Any], ...]
    order_completion_state: Mapping[str, Any]
    routing_completion_state: Mapping[str, Any]
    source_repair_id: str | None = None


@dataclass(frozen=True)
class CommittedScheduleState:
    factory_id: int
    boundaries: tuple[WindowBoundaryState, ...] = ()
    committed_operation_keys: tuple[str, ...] = ()

    def append(self, boundary: WindowBoundaryState) -> "CommittedScheduleState":
        if int(boundary.factory_id) != int(self.factory_id):
            raise ValueError("cannot carry a boundary across factories")
        prior, current = set(self.committed_operation_keys), set(boundary.committed_operation_keys)
        if prior & current:
            raise ValueError(f"rolling commit would duplicate operations: {sorted(prior & current)}")
        return CommittedScheduleState(self.factory_id, self.boundaries + (boundary,),
                                      tuple(sorted(prior | current)))


def detect_provisional_hard_freeze(*, prior_provisional_keys=(),
                                  incoming_hard_operation_keys=(),
                                  previously_committed_keys=(),
                                  current_new_commitment_keys=()):
    """Detect provisional work that was hard before the current solve.

    The incoming boundary is sampled *before* the current planning solve.
    A provisional operation may become a new commitment after an accepted
    solve; that transition is legitimate and is therefore not evidence of a
    hard freeze.  Conversely, a provisional key already present in the
    incoming committed/reservation/routing context is illegal unless it was
    already an explicitly legitimate commitment (which also protects callers
    carrying richer boundary representations).

    ``current_new_commitment_keys`` is accepted deliberately as provenance for
    callers and diagnostics; it is never used to turn an illegal incoming
    hard fact into a legal one.
    """
    prior = {str(key) for key in prior_provisional_keys}
    incoming = {str(key) for key in incoming_hard_operation_keys}
    previously = {str(key) for key in previously_committed_keys}
    # Keep the transition set explicit so callers cannot accidentally revive
    # the old ``(A & B) - B`` outgoing-boundary tautology.
    _new_commitments = {str(key) for key in current_new_commitment_keys}
    del _new_commitments
    return tuple(sorted((prior & incoming) - previously))


@dataclass(frozen=True)
class RollingPlanResult:
    factory_id: int
    status: str
    windows: tuple[Mapping[str, Any], ...]
    committed_state: CommittedScheduleState
    unscheduled_operation_keys: tuple[str, ...] = ()
    database_writes: int = 0


class GlobalCRAFTPlanner:
    """Factory-by-factory rolling planner that cannot bypass CRAFT."""

    def __init__(self, *, schedule_kernel: Callable[..., dict] = run_canonical_schedule,
                 resource_domain_policy=None, temporal_policy=None,
                 resource_domain_v3_policy=None):
        self._schedule_kernel = schedule_kernel
        # Explicit opt-in keeps existing repair/planning callers unchanged,
        # while exposing the production V2 controller through the canonical
        # planner API rather than only through a scratch harness.
        self._resource_domain_policy = resource_domain_policy
        # Optional V3 temporal adapter.  Leaving this unset preserves the
        # validated V2/repair behavior for existing callers.
        self._temporal_policy = temporal_policy
        self._resource_domain_v3_policy = resource_domain_v3_policy

    def plan_factory_window(self, *, window: RollingWindow, prepared,
                            manager_overrides=(), objective_mode=True, **kwargs):
        # Planning V3 must validate its incumbent through the same bounded
        # scope/replay/physical path used for final window acceptance.  This
        # callback is planning-only; repair callers retain their full
        # incumbent phenotype semantics.
        kwargs = dict(kwargs)
        if self._resource_domain_v3_policy is not None and isinstance(prepared, dict):
            from .scoped_validation import validate_canonical_planning_candidate
            planning_state = prepared.get("state", {})
            required_keys = tuple(prepared.get(
                "required_window_keys",
                prepared.get("initial_keys", planning_state.get("keys", ()))))
            boundary_facts = tuple(prepared.get("boundary_facts", ()))
            future_keys = factory_future_operation_keys(planning_state, window.factory_id)

            def _scoped_incumbent_validator(candidate):
                checked = validate_canonical_planning_candidate(
                    candidate, candidate.get("state", planning_state),
                    required_operation_keys=required_keys,
                    boundary_facts=boundary_facts,
                    future_outside_operation_keys=future_keys)
                candidate.setdefault("planning_acceptance", checked)
                return bool(checked.get("accepted"))

            kwargs.setdefault("incumbent_validator", _scoped_incumbent_validator)
        if (self._resource_domain_policy is not None or self._resource_domain_v3_policy is not None) and isinstance(prepared, dict) \
                and isinstance(prepared.get("state"), dict):
            if self._resource_domain_v3_policy is not None:
                from .resource_domain_v3 import run_v3_planning_schedule
                result = run_v3_planning_schedule(
                    prepared["state"],
                    policy=self._resource_domain_v3_policy,
                    temporal_policy=self._temporal_policy,
                    manager_overrides=manager_overrides,
                    objective_mode=objective_mode,
                    **kwargs)
            elif self._temporal_policy is not None:
                from .temporal_escalation import run_temporal_escalated_planning_schedule
                result = run_temporal_escalated_planning_schedule(
                    prepared["state"],
                    resource_domain_policy=self._resource_domain_policy,
                    temporal_policy=self._temporal_policy,
                    manager_overrides=manager_overrides,
                    objective_mode=objective_mode,
                    **kwargs)
            else:
                from .resource_domain_v2 import run_adaptive_planning_schedule
                result = run_adaptive_planning_schedule(
                    prepared["state"], policy=self._resource_domain_policy,
                    manager_overrides=manager_overrides,
                    objective_mode=objective_mode,
                    **kwargs)
        else:
            result = self._schedule_kernel(prepared,
                                           manager_overrides=manager_overrides,
                                           objective_mode=objective_mode, **kwargs)
        selected = result.get('acceptance', {}).get('selected_schedule', ())
        state = prepared.get('state', {})
        if 'problem' not in state:
            return {"window": window, "kernel": "crafter.repair.solve.run_canonical_schedule",
                    "result": result, "window_acceptance": {'accepted': False, 'scope_status': 'NOT_RUN',
                    'replay_status': 'NOT_RUN', 'physical_status': 'NOT_RUN'},
                    "commit_status": "REJECTED_VALIDATION_CONTEXT_INCOMPLETE", "database_writes": 0}
        for row in selected:
            op = state['problem'].operation_by_key[str(row['operation_key'])]
            row['order_id'] = str(op.order_id)
            row['setup_seconds'] = int(row['resource_configuration']['setup_seconds'])
            row['productive_seconds'] = int(row['resource_configuration']['productive_seconds'])
            row['productive_minutes'] = float(row['resource_configuration'].get('productive_minutes', 0.0))
        required = tuple(sorted(str(x) for x in prepared.get('required_window_keys',
                              prepared.get('initial_keys', state.get('keys', ())))))
        future = factory_future_operation_keys(state, window.factory_id)
        from .validation_scope import ValidationScopeV1
        scope = ValidationScopeV1('WINDOW', required, future)
        boundary_facts = tuple(prepared.get('boundary_facts', ()))
        phenotype, _ = build_window_phenotype(selected, boundary_facts=boundary_facts,
                                               future_outside_operation_keys=future,
                                               validation_scope=scope)
        acceptance = result.get('acceptance', {})
        winning_status = str((acceptance.get('winning_stage') or {}).get('status', ''))
        candidate_ready = (acceptance.get('status') == 'ACCEPTED' or
                           winning_status in {'FEASIBLE', 'OPTIMAL'} and
                           int((acceptance.get('materialization') or {}).get(
                               'mismatch_count', 0)) == 0)
        scoped = validate_planning_window(state, phenotype) if candidate_ready else {
            'accepted': False, 'scope_status': 'NOT_RUN',
            'replay_status': 'NOT_RUN', 'physical_status': 'NOT_RUN'}
        return {"window": window, "kernel": "crafter.repair.solve.run_canonical_schedule",
                "result": result, "window_acceptance": scoped,
                "commit_status": "PENDING_ACCEPTANCE" if scoped.get('accepted') else "REJECTED_NOT_COMMITTED", "database_writes": 0}

    @staticmethod
    def boundary_state_from_accepted_repair(result, *, factory_id: int,
                                            commit_end_seconds: int,
                                            source_repair_id: str | None = None):
        """Extract exact committed occupancy and routing facts from a window.

        Operations that start before the commit edge are committed whole.  A
        rolling boundary never cuts a setup, machine envelope, or fixed-crew
        productive segment.
        """
        acceptance = result.get("acceptance", {})
        if acceptance.get("status") != "ACCEPTED":
            raise ValueError("cannot create rolling boundary from rejected repair")
        selected = acceptance.get("selected_schedule", ())
        chosen = [row for row in selected if int(row["start_seconds"]) < int(commit_end_seconds)]
        committed = tuple(sorted(str(row["operation_key"]) for row in chosen))
        crossing = tuple(sorted(str(row["operation_key"]) for row in chosen
                                if int(row["end_seconds"]) > int(commit_end_seconds)))
        machines, workers, routing, orders = [], [], {}, {}
        state = result.get("state")
        for row in chosen:
            key = str(row["operation_key"]); cfg = row["resource_configuration"]
            start, end = int(row["start_seconds"]), int(row["end_seconds"])
            order_id = str(state["problem"].operation_by_key[key].order_id) if state is not None else str(row.get("order_id"))
            for machine in cfg["machine_ids"]:
                machines.append({"machine_id": int(machine), "operation_key": key,
                                 "production_order_id": order_id,
                                 "start_seconds": start, "end_seconds": end,
                                 "factory_id": int(cfg["factory_id"]), "line_id": int(cfg["line_id"]),
                                 "k": int(cfg.get("k", len(cfg["machine_ids"]))),
                                 "setup_seconds": int(row.get("setup_seconds", cfg.get("setup_seconds", 0))),
                                 "productive_seconds": int(row.get("productive_seconds", cfg.get("productive_seconds", 0)))})
            for worker in cfg["worker_ids"]:
                for left, right in row["worker_segments"]:
                    workers.append({"worker_id": str(worker), "operation_key": key,
                                    "start_seconds": int(left), "end_seconds": int(right),
                                    "factory_id": int(cfg["factory_id"])})
            successor = None
            if state is not None:
                successor = state.get("after_index").route_successor.get(key)
            routing[key] = {"predecessor_operation_key": state.get("after_index").route_predecessor.get(key) if state is not None else None,
                            "successor_operation_key": successor,
                            "authoritative_end_seconds": end,
                            "production_order_id": order_id,
                            "line_id": int(cfg["line_id"]),
                            "k": int(cfg.get("k", len(cfg["machine_ids"]))),
                            "setup_seconds": int(row.get("setup_seconds", cfg.get("setup_seconds", 0))),
                            "productive_seconds": int(row.get("productive_seconds", cfg.get("productive_seconds", 0))),
                            "machine_ids": tuple(cfg["machine_ids"]), "worker_ids": tuple(cfg["worker_ids"])}
            if state is not None:
                order = order_id
                prior = orders.get(order)
                if prior is None or end > int(prior["end_seconds"]):
                    orders[order] = {"operation_key": key, "end_seconds": end}
        return WindowBoundaryState(int(factory_id), committed, crossing, tuple(machines), tuple(workers),
                                   orders, routing, source_repair_id)

    def plan_factory_windows(self, *, factory_id: int, windows: Iterable[RollingWindow],
                             prepared_provider: Callable[[RollingWindow, CommittedScheduleState], Mapping[str, Any]],
                             manager_overrides=(), objective_mode=True, **kwargs) -> RollingPlanResult:
        """Advance accepted windows with exact caller-materialized boundaries.

        The planning adapter remains explicit: it must inject every carried
        commitment into the next prepared canonical state.  This prevents the
        planner from silently reducing a boundary to a free-time estimate.
        """
        committed = CommittedScheduleState(int(factory_id)); records = []
        for window in windows:
            if int(window.factory_id) != int(factory_id):
                raise ValueError("window factory does not match planner factory")
            prepared = prepared_provider(window, committed)
            if committed.boundaries and isinstance(prepared, dict):
                facts = list(prepared.get('boundary_facts', ()))
                prior = committed.boundaries[-1]
                for row in prior.machine_commitments:
                    facts.append({**row, 'production_order_id': row.get('production_order_id'),
                                  'end_seconds': row['end_seconds']})
                for key, route in prior.routing_completion_state.items():
                    facts.append({'operation_key': key, **route,
                                  'authoritative_end_seconds': route['authoritative_end_seconds']})
                prepared = {**prepared, 'boundary_facts': tuple(facts)}
            record = self.plan_factory_window(window=window, prepared=prepared,
                                              manager_overrides=manager_overrides,
                                              objective_mode=objective_mode, **kwargs)
            if not record.get("window_acceptance", {}).get("accepted"):
                records.append({**record, "commit_status": "REJECTED_NOT_COMMITTED"})
                return RollingPlanResult(int(factory_id), "WINDOW_REJECTED", tuple(records), committed,
                                         database_writes=0)
            boundary = self.boundary_state_from_accepted_repair(
                record["result"], factory_id=factory_id,
                commit_end_seconds=window.commit_end_seconds, source_repair_id=window.window_id)
            committed = committed.append(boundary)
            records.append({**record, "boundary": boundary, "commit_status": "ACCEPTED_IN_MEMORY_ONLY"})
        return RollingPlanResult(int(factory_id), "ALL_WINDOWS_ACCEPTED", tuple(records), committed,
                                 database_writes=0)
