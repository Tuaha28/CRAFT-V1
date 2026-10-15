"""Bounded route-aware worker-crew expansion for exact CRAFT domains.

The module expands only physically legal ``ResourceConfiguration`` objects.
It never changes CP constraints, commitments, calendars, routing, replay, or
validation.  Existing configurations are retained verbatim and every added
crew is proved against the authoritative fixed-crew execution function and
the carried worker/machine boundary before canonical calendar regimes are
regenerated.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
import time
from typing import Callable, Mapping, Sequence

from crafter.lns_probe.joint_modes import _fixed_crew_execution

from .boundary_aware_modes import (
    BoundaryAvailabilityIndex,
    _covered,
    _crew_candidates,
    _eligible_workers,
)
from .engine import _routing_aware_earliest
from .resource_configs import ResourceConfiguration, generate_resource_configurations


EXPANSION_PROVENANCE = "ROUTE_AWARE_ADAPTIVE_CREW_EXPANSION_V1"


def _compact_solve_diagnostic(result: Mapping | None) -> dict:
    """Keep recovery history bounded without retaining CP/domain payloads."""
    result = result if isinstance(result, Mapping) else {}
    # ``solve_round`` wraps the full canonical result under ``candidate`` so
    # the caller can still accept it.  Diagnostics must unwrap it before
    # reading CP/replay status, then discard that full object immediately.
    source = result
    nested = result.get("candidate")
    if "acceptance" not in source and isinstance(nested, Mapping):
        source = nested
    acceptance = source.get("acceptance") or {}
    stages = acceptance.get("stages") or ()
    winning = acceptance.get("winning_stage") or {}
    last = stages[-1] if stages and isinstance(stages[-1], Mapping) else {}
    selected = acceptance.get("selected_schedule")
    return {
        "accepted": bool(result.get("accepted", False)),
        "candidate_exists": bool(result.get("candidate_exists", bool(selected))),
        "status": acceptance.get("status") or result.get("status"),
        "solver_status": winning.get("status") or last.get("status") or result.get("cp_status"),
        "cp_model_build_seconds": sum(float(row.get("model_build_seconds", 0.0) or 0.0)
                                      for row in stages if isinstance(row, Mapping)),
        "cp_solve_seconds": sum(float(row.get("solve_seconds", row.get("runtime_seconds", 0.0)) or 0.0)
                                for row in stages if isinstance(row, Mapping)),
        "cp_time_limit_seconds": last.get("deadline_budget_seconds"),
        "search_limit_hit": last.get("search_limit_hit"),
        "replay": (acceptance.get("replay") or {}).get("status"),
        "validator": (acceptance.get("validator") or {}).get("status"),
        "materialization_mismatch_count": int(
            (acceptance.get("materialization") or {}).get("mismatch_count", 0) or 0),
        "resource_configuration_count": result.get("resource_configuration_count"),
        "calendar_regime_count": result.get("calendar_regime_count"),
        "database_writes": 0,
    }


@dataclass(frozen=True)
class RouteAwareCrewExpansionPolicyV1:
    """Deterministic structural budget for targeted crew exposure."""

    # ``None`` is the Production V1 fixed-point policy: rounds terminate only
    # on a valid candidate, domain exhaustion, no-progress, or wall-clock
    # safety.  Numeric values remain available to bounded research probes.
    max_rounds: int | None = 3
    total_new_crews_per_operation_by_round: tuple[int, ...] = (8, 16, 16)
    # ``None`` means the complete local causal closure.  The historical
    # numeric defaults remain available to research probes, while Production
    # V1 opts into fixed-point-by-causality semantics.
    max_expansion_operations: int | None = 6
    max_successor_depth: int | None = 4
    max_pressure_targets_per_round: int | None = 1
    # Adaptive selective expansion: broaden slightly after the first CP
    # failure, but never fan out to the complete local neighborhood merely
    # because every compact domain is theoretically truncated.
    pressure_targets_by_round: tuple[int, ...] = (3, 4, 4, 4)
    total_new_machine_alternatives_per_operation_by_round: tuple[int, ...] = (1, 2, 3, 4)
    max_resource_configurations: int | None = 600
    max_calendar_regimes: int | None = 5500
    max_peak_rss_mb: float | None = 1536.0
    max_recovery_runtime_seconds: float | None = 300.0

    def __post_init__(self):
        if self.max_rounds is not None and self.max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        if (self.max_rounds is not None and
                len(self.total_new_crews_per_operation_by_round) < self.max_rounds):
            raise ValueError("every adaptive round requires a crew budget")
        if any(int(value) < 1 for value in self.total_new_crews_per_operation_by_round):
            raise ValueError("crew budgets must be positive")
        if tuple(sorted(self.total_new_crews_per_operation_by_round)) != tuple(
                self.total_new_crews_per_operation_by_round):
            raise ValueError("crew budgets must be cumulative and non-decreasing")
        if self.max_expansion_operations is not None and self.max_expansion_operations < 1:
            raise ValueError("closure budgets must be positive when bounded")
        if self.max_successor_depth is not None and self.max_successor_depth < 0:
            raise ValueError("successor depth must be non-negative when bounded")
        if self.max_pressure_targets_per_round is not None and self.max_pressure_targets_per_round < 1:
            raise ValueError("pressure target budget must be positive when bounded")
        if not self.pressure_targets_by_round or any(int(value) < 1 for value in self.pressure_targets_by_round):
            raise ValueError("adaptive pressure target budgets must be positive")
        if (not self.total_new_machine_alternatives_per_operation_by_round or
                any(int(value) < 0 for value in
                    self.total_new_machine_alternatives_per_operation_by_round)):
            raise ValueError("machine alternative budgets must be non-negative")
        for name in ("max_resource_configurations", "max_calendar_regimes"):
            value = getattr(self, name)
            if value is not None and int(value) < 1:
                raise ValueError(f"{name} must be positive when bounded")
        if self.max_peak_rss_mb is not None and float(self.max_peak_rss_mb) <= 0:
            raise ValueError("max_peak_rss_mb must be positive when bounded")
        if (self.max_recovery_runtime_seconds is not None and
                float(self.max_recovery_runtime_seconds) <= 0):
            raise ValueError("max_recovery_runtime_seconds must be positive when bounded")


def configuration_signature(config: ResourceConfiguration) -> tuple:
    """Canonical identity for strict-superset and duplicate checks."""
    return (
        str(config.operation_key),
        int(config.factory_id),
        int(config.line_id),
        int(config.k),
        tuple(int(value) for value in config.machine_ids),
        tuple(sorted(str(value) for value in config.worker_ids)),
        int(config.setup_seconds),
        int(config.productive_seconds),
        float(config.productive_minutes),
    )


def domain_signatures(configurations: Mapping[str, Sequence[ResourceConfiguration]]) -> dict[str, tuple[tuple, ...]]:
    return {
        str(key): tuple(sorted((configuration_signature(value) for value in values), key=repr))
        for key, values in sorted(configurations.items(), key=lambda item: str(item[0]))
    }


def audit_strict_superset(original, expanded) -> dict:
    before = {key: set(values) for key, values in domain_signatures(original).items()}
    after = {key: set(values) for key, values in domain_signatures(expanded).items()}
    missing = {
        key: sorted(before_values - after.get(key, set()), key=repr)
        for key, before_values in before.items()
        if before_values - after.get(key, set())
    }
    added = {
        key: sorted(after.get(key, set()) - before.get(key, set()), key=repr)
        for key in sorted(after)
        if after.get(key, set()) - before.get(key, set())
    }
    return {
        "strict_superset_or_equal": not missing,
        "strict_superset": not missing and any(added.values()),
        "missing_original_signatures": missing,
        "added_signatures": added,
        "original_configuration_count": sum(len(values) for values in before.values()),
        "expanded_configuration_count": sum(len(values) for values in after.values()),
    }


def _audit_index(resource_audit) -> dict[str, dict]:
    return {str(row.get("operation_key")): row for row in (resource_audit or ())}


def _worker_domain_truncated(audit_row) -> bool:
    return any(bool(row.get("more_worker_crews_available"))
               for row in (audit_row or {}).get("domain_rows", ()))


def worker_domain_accounting(configurations, resource_audit, operation_keys) -> dict[str, dict]:
    """Return deterministic exposed/legal worker-domain accounting.

    ``resource_audit`` is authoritative when it exposes an exact legal crew
    count.  The current configuration set is counted by unique worker tuple,
    so repeated machine/calendar shapes do not falsely inflate exposure.  If
    an older audit lacks an exact total, ``remaining_legal_crews`` is left
    unknown and the explicit truncation flag remains authoritative.
    """
    audit = _audit_index(resource_audit)
    result: dict[str, dict] = {}
    for key in sorted({str(value) for value in operation_keys}):
        row = audit.get(key, {}) or {}
        domains = tuple(row.get("domain_rows") or ())
        legal_values = []
        for domain in domains:
            value = domain.get("total_legal_worker_crews")
            if value is None:
                continue
            try:
                legal_values.append(int(value))
            except (TypeError, ValueError):
                continue
        exposed = {
            tuple(sorted(str(worker) for worker in config.worker_ids))
            for config in (configurations.get(key, ()) or ())
        }
        legal = sum(legal_values) if legal_values else None
        remaining = (max(0, int(legal) - len(exposed))
                     if legal is not None else None)
        explicitly_truncated = _worker_domain_truncated(row)
        fully_exposed = bool(legal is not None and remaining == 0)
        result[key] = {
            "operation_key": key,
            "legal_worker_crews": legal,
            "exposed_worker_crews": len(exposed),
            "remaining_legal_crews": remaining,
            "worker_domain_truncated": explicitly_truncated and not fully_exposed,
            "worker_domain_fully_exposed": fully_exposed,
        }
    return result


def _machine_domain_truncated(audit_row) -> bool:
    return any(bool(row.get("more_machine_sets_available"))
               for row in (audit_row or {}).get("domain_rows", ()))


def machine_domain_accounting(configurations, resource_audit, operation_keys) -> dict[str, dict]:
    """Account for exposed machine sets without enumerating combinations."""
    audit = _audit_index(resource_audit)
    result: dict[str, dict] = {}
    for key in sorted({str(value) for value in operation_keys}):
        row = audit.get(key, {}) or {}
        legal_values = []
        for domain in tuple(row.get("domain_rows") or ()):
            value = domain.get("total_legal_machine_sets")
            if value is None:
                continue
            try:
                legal_values.append(int(value))
            except (TypeError, ValueError):
                continue
        exposed = {
            (int(config.line_id), int(config.k), tuple(int(machine)
                                                       for machine in config.machine_ids))
            for config in (configurations.get(key, ()) or ())
        }
        legal = sum(legal_values) if legal_values else None
        remaining = max(0, int(legal) - len(exposed)) if legal is not None else None
        explicitly_truncated = _machine_domain_truncated(row)
        fully_exposed = bool(legal is not None and remaining == 0)
        result[key] = {
            "operation_key": key,
            "legal_machine_sets": legal,
            "exposed_machine_sets": len(exposed),
            "remaining_legal_machine_sets": remaining,
            "machine_domain_truncated": explicitly_truncated and not fully_exposed,
            "machine_domain_fully_exposed": fully_exposed,
        }
    return result


def _process_peak_rss_mb() -> float | None:
    """Return the OS process peak working set when available."""
    try:
        import psutil
        info = psutil.Process().memory_info()
        value = getattr(info, "peak_wset", None)
        if value is None:
            value = getattr(info, "peak_rss", None)
        if value is None:
            value = info.rss
        return float(value) / (1024.0 * 1024.0)
    except Exception:
        return None


def _resource_contention_by_operation(configurations) -> dict[str, dict[str, int]]:
    """Count exposed local resource sharing as ranking evidence only."""
    workers: Counter[str] = Counter()
    machines: Counter[int] = Counter()
    exposed: dict[str, tuple[set[str], set[int]]] = {}
    for key, rows in configurations.items():
        op_workers = {str(worker) for config in rows for worker in config.worker_ids}
        op_machines = {int(machine) for config in rows for machine in config.machine_ids}
        exposed[str(key)] = (op_workers, op_machines)
        workers.update(op_workers)
        machines.update(op_machines)
    return {
        key: {
            "worker_contention": sum(max(0, workers[value] - 1) for value in op_workers),
            "machine_contention": sum(max(0, machines[value] - 1) for value in op_machines),
        }
        for key, (op_workers, op_machines) in exposed.items()
    }


def rank_adaptive_resource_bottlenecks(diagnosis, worker_accounting,
                                       machine_accounting, configurations) -> tuple[dict, ...]:
    """Rank only evidence-bearing truncated local operations deterministically."""
    contention = _resource_contention_by_operation(configurations)
    blocked_successors: Counter[str] = Counter()
    for source in diagnosis:
        if (source.get("earliest_start_in_exposed_domain") is None and
                str(source.get("earliest_ready_source")) ==
                "UNRESOLVED_INTERNAL_PREDECESSOR"):
            blocked_successors.update(str(value)
                                      for value in source.get("predecessor_keys", ()))
    ranked = []
    for source in diagnosis:
        row = dict(source)
        key = str(row.get("operation_key"))
        worker_truncated = bool(worker_accounting.get(key, {}).get(
            "worker_domain_truncated", False))
        machine_truncated = bool(machine_accounting.get(key, {}).get(
            "machine_domain_truncated", False))
        if not (worker_truncated or machine_truncated):
            continue
        no_executable_start = row.get("earliest_start_in_exposed_domain") is None
        predecessor_blocked = str(row.get("earliest_ready_source")) == \
            "UNRESOLVED_INTERNAL_PREDECESSOR"
        delay = max(0, int(row.get("crew_delay_seconds") or 0))
        resource_contention = contention.get(key, {})
        contention_score = (int(resource_contention.get("worker_contention", 0)) +
                            int(resource_contention.get("machine_contention", 0)))
        reasons = []
        if delay:
            reasons.append("ROUTE_DELAY")
        if worker_truncated:
            reasons.append("WORKER_DOMAIN_TRUNCATED")
        if machine_truncated:
            reasons.append("MACHINE_DOMAIN_TRUNCATED")
        if no_executable_start:
            reasons.append("NO_EXECUTABLE_EXPOSED_START")
        if predecessor_blocked:
            reasons.append("PREDECESSOR_BLOCKED")
        if blocked_successors.get(key, 0):
            reasons.append("BLOCKS_UNEXECUTABLE_SUCCESSOR")
        if contention_score:
            reasons.append("RESOURCE_CONTENTION")
        row.update({
            "worker_domain_truncated": worker_truncated,
            "machine_domain_truncated": machine_truncated,
            "no_executable_exposed_start": no_executable_start,
            "predecessor_blocked": predecessor_blocked,
            "worker_contention_score": int(resource_contention.get("worker_contention", 0)),
            "machine_contention_score": int(resource_contention.get("machine_contention", 0)),
            "resource_contention_score": contention_score,
            "blocked_successor_count": int(blocked_successors.get(key, 0)),
            "bottleneck_reasons": reasons,
        })
        ranked.append(row)
    return tuple(sorted(ranked, key=lambda row: (
        -int(bool(int(row.get("crew_delay_seconds") or 0) > 0)),
        -int(row.get("crew_delay_seconds") or 0),
        -int(row.get("blocked_successor_count", 0)),
        -int(bool(row["no_executable_exposed_start"]) and
             not bool(row["predecessor_blocked"])),
        not (bool(row["worker_domain_truncated"]) and
             bool(row["machine_domain_truncated"])),
        -int(row["resource_contention_score"]),
        bool(row["predecessor_blocked"]),
        str(row["operation_key"]),
    )))


def build_worker_contention(resource_audit, operation_keys) -> dict[str, int]:
    """Count active-neighborhood demand without turning it into feasibility."""
    active = {str(key) for key in operation_keys}
    pressure: Counter[str] = Counter()
    for row in resource_audit or ():
        if str(row.get("operation_key")) not in active:
            continue
        workers = {
            str(worker)
            for domain in row.get("domain_rows", ())
            for worker in domain.get("worker_pool", ())
        }
        pressure.update(workers)
    return dict(sorted(pressure.items(), key=lambda item: str(item[0])))


def _minimum_execution(regimes, required_start: int | None) -> tuple[int | None, int | None]:
    if required_start is None:
        return None, None
    choices = []
    for regime in regimes or ():
        start = max(int(required_start), int(regime.get("lo", 0)))
        if start > int(regime.get("hi", 0)):
            continue
        slope, intercept = regime["formula"][0]
        choices.append((int(slope) * start + int(intercept), start))
    if not choices:
        return None, None
    end, start = min(choices)
    return int(start), int(end)


def _route_ready_seconds(state, operation_key, route_bounds) -> tuple[int | None, str]:
    key = str(operation_key)
    predecessor = state["after_index"].route_predecessor.get(key)
    release = int(state["problem"].order_by_id[
        state["problem"].operation_by_key[key].order_id].release_seconds)
    if predecessor in set(state["keys"]):
        end = (route_bounds.get("earliest_end") or {}).get(str(predecessor))
        return (max(release, int(end)), "INTERNAL_PREDECESSOR_EARLIEST_END") if end is not None else (None, "UNRESOLVED_INTERNAL_PREDECESSOR")
    committed = (state.get("committed_routing_completion") or {}).get(str(predecessor))
    if committed is not None:
        return max(release, int(committed["authoritative_end_seconds"])), "COMMITTED_PREDECESSOR_END"
    return release, "ORDER_RELEASE"


def diagnose_worker_route_pressure(state, configurations, regimes, resource_audit) -> tuple[dict, ...]:
    """Rank truncated worker domains by exact route-propagated delay."""
    bounds = _routing_aware_earliest(state, regimes)
    audit = _audit_index(resource_audit)
    rows = []
    for key in sorted(state["keys"]):
        ready, ready_source = _route_ready_seconds(state, key, bounds)
        exposed_start = (bounds.get("earliest_start") or {}).get(key)
        exposed_end = (bounds.get("earliest_end") or {}).get(key)
        row = audit.get(key, {})
        domain_rows = row.get("domain_rows", ())
        worker_crews = {
            tuple(sorted(str(worker) for worker in config.worker_ids))
            for config in configurations.get(key, ())
        }
        machine_sets = {
            tuple(int(machine) for machine in config.machine_ids)
            for config in configurations.get(key, ())
        }
        delay = None if ready is None or exposed_start is None else int(exposed_start) - int(ready)
        rows.append({
            "operation_key": key,
            "earliest_ready": ready,
            "earliest_ready_source": ready_source,
            "earliest_start_in_exposed_domain": exposed_start,
            "earliest_end_in_exposed_domain": exposed_end,
            "predecessor_keys": [str(value) for value in (state["after_index"].route_predecessor.get(key),) if value],
            "successor_keys": [str(value) for value in (state["after_index"].route_successor.get(key),) if value],
            "resource_configuration_count": len(configurations.get(key, ())),
            "worker_crew_count": len(worker_crews),
            "machine_set_count": len(machine_sets),
            "calendar_regime_count": len(regimes.get(key, ())),
            "worker_domain_truncated": _worker_domain_truncated(row),
            "machine_domain_truncated": any(bool(item.get("more_machine_sets_available")) for item in domain_rows),
            "crew_delay_seconds": delay,
            "route_pressure": bool(_worker_domain_truncated(row) and delay is not None and delay > 0),
            "resource_pressure": "TRUNCATED_WORKER_DOMAIN" if _worker_domain_truncated(row) else "NONE_PROVEN",
            "reason_for_expansion": (
                "TRUNCATED_WORKER_DOMAIN_DELAYS_ROUTE_EXECUTION"
                if _worker_domain_truncated(row) and delay is not None and delay > 0
                else "NO_STRUCTURAL_WORKER_ROUTE_PRESSURE"
            ),
        })
    return tuple(sorted(rows, key=lambda row: (
        not row["route_pressure"],
        -(int(row["crew_delay_seconds"]) if row["crew_delay_seconds"] is not None else -1),
        row["operation_key"],
    )))


def _candidate_end_seconds(state, config, crew, start_seconds):
    ready = state["problem"].origin + timedelta(
        seconds=int(start_seconds) + int(config.setup_seconds))
    end, segments = _fixed_crew_execution(
        state["problem"], state["operator_index"], tuple(crew), ready,
        float(config.productive_minutes), state["_worker_res"])
    end_seconds = int(round((end - state["problem"].origin).total_seconds()))
    worker_segments = tuple(
        (int(round((left - state["problem"].origin).total_seconds())),
         int(round((right - state["problem"].origin).total_seconds())))
        for left, right in segments
    )
    return end_seconds, worker_segments


def authoritative_start_probes(state, availability, configurations, operation_key,
                               route_ready_seconds, limit=12) -> tuple[int, ...]:
    """Return deterministic exact future readiness/calendar event instants.

    These are probe points only.  They do not reject any CP start and do not
    approximate availability; every resulting configuration is still passed
    through canonical regime construction and exact CP.
    """
    key = str(operation_key)
    ready = int(route_ready_seconds)
    horizon = int(availability.horizon_end_seconds)
    points = {ready}
    op = state["problem"].operation_by_key[key]
    capability = str(op.required_capability).strip().lower()
    factory_id = int(state["planning_context"]["factory_id"])
    for config in configurations.get(key, ()):
        for worker in _eligible_workers(state, int(config.line_id), capability, factory_id):
            for left, _right in availability.worker_free.get(str(worker), ()):
                if ready < int(left) < horizon:
                    points.add(int(left))
        for machine in config.machine_ids:
            for left, _right in availability.machine_free.get(int(machine), ()):
                if ready < int(left) < horizon:
                    points.add(int(left))
    return tuple(sorted(points)[:max(1, int(limit))])


def generate_additional_crew_configurations(
        state,
        availability: BoundaryAvailabilityIndex,
        configurations,
        operation_key,
        start_seconds,
        quota,
        worker_pressure=None,
        additional_start_probes=(),
        diversity_mode=False) -> tuple[tuple[ResourceConfiguration, ...], dict]:
    """Price a bounded set of new legal fixed crews for one operation."""
    started = time.perf_counter()
    key = str(operation_key)
    existing = tuple(configurations.get(key, ()))
    existing_signatures = {configuration_signature(value) for value in existing}
    shapes = {}
    for config in existing:
        shape = (
            int(config.line_id), int(config.k), tuple(int(x) for x in config.machine_ids),
            int(config.setup_seconds), int(config.productive_seconds), float(config.productive_minutes),
        )
        shapes.setdefault(shape, config)
    candidates = []
    candidate_signatures = set()
    attempted = 0
    duplicates = 0
    rejected_machine_boundary = 0
    rejected_exact_execution = 0
    probes = tuple(sorted({int(start_seconds), *(int(value) for value in additional_start_probes)}))
    for probe in probes:
        for shape, template in sorted(shapes.items(), key=lambda item: repr(item[0])):
            forbidden = tuple(
                tuple(config.worker_ids)
                for config in existing
                if (int(config.line_id), int(config.k), tuple(int(x) for x in config.machine_ids),
                    int(config.setup_seconds), int(config.productive_seconds), float(config.productive_minutes)) == shape
            )
            crews = _crew_candidates(
                state, availability, key, int(template.line_id), int(template.k),
                tuple(template.machine_ids), int(probe), max_crews=max(int(quota) * 4, int(quota)),
                forbidden_crews=forbidden, worker_pressure=worker_pressure or {},
            )
            for crew in crews:
                attempted += 1
                try:
                    end_seconds, worker_segments = _candidate_end_seconds(
                        state, template, crew, int(probe))
                except Exception:
                    rejected_exact_execution += 1
                    continue
                if not all(_covered(availability.machine_free.get(int(machine), ()),
                                    ((int(probe), int(end_seconds)),))
                           for machine in template.machine_ids):
                    rejected_machine_boundary += 1
                    continue
                value = replace(
                    template,
                    worker_ids=tuple(sorted(str(worker) for worker in crew)),
                    provenance=tuple(template.provenance) + (EXPANSION_PROVENANCE,),
                )
                signature = configuration_signature(value)
                if signature in existing_signatures or signature in candidate_signatures:
                    duplicates += 1
                    continue
                pressure_values = [int((worker_pressure or {}).get(str(worker), 0)) for worker in crew]
                candidates.append({
                    "config": value,
                    "start_seconds": int(probe),
                    "end_seconds": int(end_seconds),
                    "worker_segments": worker_segments,
                    "contention_sum": sum(pressure_values),
                    "contention_max": max(pressure_values, default=0),
                })
                candidate_signatures.add(signature)
        if len(candidates) >= max(int(quota) * 4, int(quota)):
            break
    candidates.sort(key=lambda row: (
        row["end_seconds"], row["contention_sum"], row["contention_max"],
        repr(configuration_signature(row["config"])),
    ))
    selected_rows = []
    if diversity_mode:
        # One candidate from each distinct machine/calendar pattern first;
        # then fill by the unchanged earliest/contention order.  This changes
        # candidate selection only for the explicit breadth-rescue caller.
        # Every candidate has already passed exact crew execution and machine
        # boundary checks above.
        calendar_index = state.get("operator_index") or {}
        crew_of = calendar_index.get("crew_of") or {}
        shift_of = calendar_index.get("shift_of") or {}
        seen_patterns = set()
        for row in candidates:
            config = row["config"]
            worker_pattern = []
            for worker in tuple(sorted(str(value) for value in config.worker_ids)):
                if worker in crew_of:
                    worker_pattern.append(("crew",) + tuple(crew_of[worker]))
                elif worker in shift_of:
                    worker_pattern.append(("shift",) + tuple(shift_of[worker]))
                else:
                    worker_pattern.append(("always", worker))
            pattern = (
                int(config.line_id), int(config.k), tuple(config.machine_ids),
                tuple(sorted(worker_pattern, key=repr)))
            if pattern in seen_patterns:
                continue
            seen_patterns.add(pattern)
            selected_rows.append(row)
            if len(selected_rows) >= int(quota):
                break
        if len(selected_rows) < int(quota):
            selected_signatures = {
                configuration_signature(row["config"]) for row in selected_rows}
            for row in candidates:
                if configuration_signature(row["config"]) in selected_signatures:
                    continue
                selected_rows.append(row)
                if len(selected_rows) >= int(quota):
                    break
    else:
        selected_rows = candidates[:int(quota)]
    selected = tuple(row["config"] for row in selected_rows)
    trace = {
        "operation_key": key,
        "start_probe_seconds": int(start_seconds),
        "start_probes_considered": list(probes),
        "crew_quota": int(quota),
        "configuration_shapes_considered": len(shapes),
        "candidate_crews_generated": attempted,
        "duplicates_rejected": duplicates,
        "machine_boundary_rejections": rejected_machine_boundary,
        "exact_execution_rejections": rejected_exact_execution,
        "new_crews_accepted": len(selected),
        "selected_start_probes": [int(row["start_seconds"]) for row in selected_rows],
        "selected_configuration_signatures": [list(configuration_signature(value)) for value in selected],
        "matching_generation_seconds": time.perf_counter() - started,
        "worker_domain_mode": "MATCHING_GENERATED",
        "selection_mode": ("DIVERSE_EARLIEST" if diversity_mode
                           else "EARLIEST_CONTENTION"),
    }
    return selected, trace


def generate_additional_machine_configurations(
        state, configurations, operation_key, quota,
        *, machine_pool_limit=30) -> tuple[tuple[ResourceConfiguration, ...], dict]:
    """Expose a compact deterministic set of new legal machine shapes.

    The authoritative resource generator supplies the physical candidates.
    Only previously unseen ``(line, k, machine_ids)`` shapes are retained,
    with one matched legal crew witness per new shape.  Worker diversity for
    those shapes is then handled by the existing matching expansion.
    """
    started = time.perf_counter()
    key = str(operation_key)
    limit = max(0, int(quota))
    existing = tuple(configurations.get(key, ()))
    existing_shapes = {
        (int(config.line_id), int(config.k), tuple(int(value) for value in config.machine_ids))
        for config in existing
    }
    if limit == 0:
        return (), {
            "operation_key": key,
            "machine_alternative_quota": 0,
            "new_machine_alternatives_accepted": 0,
            "new_machine_configurations_accepted": 0,
            "machine_generation_seconds": time.perf_counter() - started,
        }
    # ``max_machine_sets`` is a candidate-generation bound, not a physical
    # domain claim.  Increase it only enough to discover the requested number
    # of unseen shapes and keep worker matching compact at one witness.
    candidate_bound = max(1, len(existing_shapes) + limit)
    generated, _audit, _summary = generate_resource_configurations(
        state, max_machine_sets=candidate_bound, max_worker_crews=1,
        machine_pool_limit=int(machine_pool_limit), operation_keys=(key,))
    selected = []
    seen_shapes = set(existing_shapes)
    for value in sorted(generated.get(key, ()), key=configuration_signature):
        shape = (int(value.line_id), int(value.k),
                 tuple(int(machine) for machine in value.machine_ids))
        if shape in seen_shapes:
            continue
        selected.append(replace(
            value, provenance=tuple(value.provenance) +
            ("ADAPTIVE_MACHINE_ALTERNATIVE_EXPANSION_V1",)))
        seen_shapes.add(shape)
        if len(selected) >= limit:
            break
    return tuple(selected), {
        "operation_key": key,
        "machine_alternative_quota": limit,
        "candidate_machine_shapes_examined": max(
            0, len({(int(value.line_id), int(value.k), tuple(value.machine_ids))
                    for value in generated.get(key, ())}) - len(existing_shapes)),
        "new_machine_alternatives_accepted": len(selected),
        "new_machine_configurations_accepted": len(selected),
        "selected_machine_shapes": [
            [int(value.line_id), int(value.k), list(value.machine_ids)]
            for value in selected
        ],
        "machine_generation_seconds": time.perf_counter() - started,
    }


def merge_additional_configurations(configurations, additions) -> dict[str, tuple[ResourceConfiguration, ...]]:
    merged = {str(key): tuple(values) for key, values in configurations.items()}
    for key, rows in sorted(additions.items(), key=lambda item: str(item[0])):
        existing = list(merged.get(str(key), ()))
        seen = {configuration_signature(value) for value in existing}
        for value in rows:
            signature = configuration_signature(value)
            if signature not in seen:
                seen.add(signature)
                existing.append(value)
        merged[str(key)] = tuple(existing)
    return merged


def select_route_successor_closure(
        state,
        pressure_operation_keys,
        previous_bounds,
        expanded_bounds,
        resource_audit,
        max_depth=4,
        max_operations=6,
        configurations=None,
        availability=None) -> tuple[tuple[str, ...], tuple[dict, ...]]:
    """Return the deterministic fixed-point causal repair closure.

    Numeric limits are retained for historical research callers.  Production
    passes ``None`` for depth/operation limits, in which case traversal ends
    only when the known local operation universe has been visited.  Resource
    neighbours are included only when they have an explicit truncated domain,
    so this does not turn a local repair into a factory-wide neighborhood.
    """
    audit = _audit_index(resource_audit)
    active = set(str(key) for key in state["keys"])
    selected = []
    reasons = []
    frontier = [(str(key), 0, "PRESSURE_SEED")
                for key in sorted(set(pressure_operation_keys)) if str(key) in active]
    seen = set()
    operation_limit = len(active) if max_operations is None else int(max_operations)
    while frontier and len(selected) < operation_limit:
        frontier.sort(key=lambda item: (int(item[1]), str(item[0]), str(item[2])))
        key, depth, cause = frontier.pop(0)
        if key in seen:
            continue
        seen.add(key)
        selected.append(key)
        if max_depth is not None and depth >= int(max_depth):
            continue

        neighbors = []
        predecessor = state["after_index"].route_predecessor.get(key)
        successor = state["after_index"].route_successor.get(key)
        if predecessor is not None and str(predecessor) in active:
            neighbors.append((str(predecessor), "ROUTING_PREDECESSOR"))
        if successor is not None and str(successor) in active:
            neighbors.append((str(successor), "ROUTING_SUCCESSOR"))

        # Resource-conflict closure is deliberately local and evidence based:
        # only operations with an explicit truncated worker/machine domain are
        # added when their exposed machine/crew pools intersect this seed.
        if configurations is not None:
            current_configs = configurations.get(key, ())
            key_machines = {int(machine) for cfg in current_configs for machine in cfg.machine_ids}
            key_workers = {str(worker) for cfg in current_configs for worker in cfg.worker_ids}
            for other in sorted(active - seen - {key}):
                if not (_worker_domain_truncated(audit.get(other, {})) or
                        any(bool(row.get("more_machine_sets_available"))
                            for row in (audit.get(other, {}).get("domain_rows") or ()) )):
                    continue
                other_configs = configurations.get(other, ())
                other_machines = {int(machine) for cfg in other_configs for machine in cfg.machine_ids}
                other_workers = {str(worker) for cfg in other_configs for worker in cfg.worker_ids}
                if (key_machines & other_machines) or (key_workers & other_workers):
                    neighbors.append((other, "RESOURCE_CONTENTION"))

        for neighbor, neighbor_cause in sorted(set(neighbors), key=lambda item: (item[0], item[1])):
            if neighbor in seen or neighbor not in active:
                continue
            old_ready = (previous_bounds.get("earliest_end") or {}).get(key)
            new_ready = (expanded_bounds.get("earliest_end") or {}).get(key)
            truncated = _worker_domain_truncated(audit.get(neighbor, {}))
            materially_changed = (
                new_ready is not None and
                (old_ready is None or int(new_ready) < int(old_ready))
            )
            # Routing closure requires a truncated successor/predecessor and a
            # readiness change.  Resource contention is itself causal when a
            # truncated neighbor shares an exposed machine/crew pool.
            required = (neighbor_cause == "RESOURCE_CONTENTION" or
                        (truncated and materially_changed))
            if not required:
                continue
            reasons.append({
                "operation_key": neighbor,
                "trigger_operation": key,
                "trigger_predecessor": key if neighbor_cause == "ROUTING_SUCCESSOR" else None,
                "reason": ("PREDECESSOR_IMPROVEMENT_CHANGES_TRUNCATED_SUCCESSOR_READINESS"
                           if neighbor_cause.startswith("ROUTING")
                           else "TRUNCATED_RESOURCE_CONTENTION"),
                "cause": neighbor_cause,
                "previous_ready_seconds": old_ready,
                "expanded_ready_seconds": new_ready,
                "depth": depth + 1,
            })
            frontier.append((neighbor, depth + 1, neighbor_cause))
    return tuple(selected), tuple(reasons)


def run_route_aware_adaptive_crew_expansion(
        state,
        availability,
        base_configurations,
        resource_audit,
        build_regimes: Callable,
        solve_round: Callable,
        policy: RouteAwareCrewExpansionPolicyV1 | None = None,
        baseline_result: Mapping | None = None,
        recovery_deadline: float | None = None,
        initial_pressure_diagnosis: tuple[dict, ...] | None = None):
    """Run cumulative strict-superset crew expansion around unchanged CP.

    ``max_rounds=None`` is the Production V1 fixed-point policy.  It is
    bounded only by legal-domain exhaustion, no-progress, or the optional
    wall-clock safety deadline; numeric values remain for legacy probes.
    """
    policy = policy or RouteAwareCrewExpansionPolicyV1()
    original = {str(key): tuple(values) for key, values in base_configurations.items()}
    current = dict(original)
    worker_pressure = build_worker_contention(resource_audit, state["keys"])
    active = set(str(key) for key in state["keys"])
    added_counts: Counter[str] = Counter()
    added_machine_counts: Counter[str] = Counter()
    start_probes_by_operation: dict[str, tuple[int, ...]] = {}
    rounds = []
    recovery_started = time.perf_counter()
    peak_rss_mb = _process_peak_rss_mb()

    regimes, regime_metadata = build_regimes(current, {})
    baseline_bounds = _routing_aware_earliest(state, regimes)
    baseline_result = (dict(baseline_result) if baseline_result is not None
                       else solve_round(0, current, regimes, regime_metadata))
    rounds.append({
        "round": 0,
        "trigger": "BASE_DOMAIN",
        "operations_expanded": [],
        "domain_superset": audit_strict_superset(original, current),
        "solve": _compact_solve_diagnostic(baseline_result),
    })
    if baseline_result.get("accepted"):
        return {"status": "BASE_DOMAIN_ACCEPTED", "configurations": current,
                "regimes": regimes, "rounds": rounds, "selected_result": baseline_result}

    previous_bounds = baseline_bounds
    diagnosed_targets = []
    closure = []
    adaptive_round = 1
    relevant_operations: set[str] = set()
    while True:
        round_started = time.perf_counter()
        observed_rss = _process_peak_rss_mb()
        if observed_rss is not None:
            peak_rss_mb = max(float(peak_rss_mb or 0.0), float(observed_rss))
        elapsed = time.perf_counter() - recovery_started
        if (policy.max_recovery_runtime_seconds is not None and
                elapsed >= float(policy.max_recovery_runtime_seconds)):
            accounting = worker_domain_accounting(current, resource_audit, active)
            return {"status": "COMPUTATIONAL_LIMIT",
                    "configurations": current, "regimes": regimes,
                    "start_probes_by_operation": dict(start_probes_by_operation),
                    "rounds": rounds, "selected_result": None,
                    "worker_domain_accounting": accounting,
                    "machine_domain_accounting": machine_domain_accounting(
                        current, resource_audit, active),
                    "remaining_truncated_operations": sorted(
                        key for key, value in accounting.items()
                        if value.get("worker_domain_truncated")),
                    "reason": "MAX_RECOVERY_RUNTIME_SECONDS",
                    "recovery_runtime_seconds": elapsed,
                    "peak_rss_mb": peak_rss_mb}
        if (policy.max_peak_rss_mb is not None and peak_rss_mb is not None and
                float(peak_rss_mb) >= float(policy.max_peak_rss_mb)):
            accounting = worker_domain_accounting(current, resource_audit, active)
            return {"status": "COMPUTATIONAL_LIMIT",
                    "configurations": current, "regimes": regimes,
                    "start_probes_by_operation": dict(start_probes_by_operation),
                    "rounds": rounds, "selected_result": None,
                    "worker_domain_accounting": accounting,
                    "machine_domain_accounting": machine_domain_accounting(
                        current, resource_audit, active),
                    "remaining_truncated_operations": sorted(
                        key for key, value in accounting.items()
                        if value.get("worker_domain_truncated")),
                    "reason": "MAX_PEAK_RSS_MB",
                    "peak_rss_mb": peak_rss_mb,
                    "peak_rss_limit_mb": float(policy.max_peak_rss_mb)}
        if recovery_deadline is not None and time.perf_counter() >= float(recovery_deadline):
            accounting = worker_domain_accounting(current, resource_audit, active)
            return {"status": "WORKER_RECOVERY_RUNTIME_LIMIT",
                    "configurations": current, "regimes": regimes,
                    "start_probes_by_operation": dict(start_probes_by_operation),
                    "rounds": rounds, "selected_result": None,
                    "worker_domain_accounting": accounting,
                    "remaining_truncated_operations": sorted(
                        key for key, value in accounting.items()
                        if value.get("worker_domain_truncated")),
                    "rounds_completed": max(0, adaptive_round - 1),
                    "total_new_crews_generated": sum(added_counts.values()),
                    "final_resource_configurations": sum(len(v) for v in current.values()),
                    "final_calendar_regimes": sum(len(v) for v in regimes.values())}
        if policy.max_rounds is not None and adaptive_round > int(policy.max_rounds):
            accounting = worker_domain_accounting(current, resource_audit, active)
            machine_accounting = machine_domain_accounting(
                current, resource_audit, active)
            return {"status": "COMPUTATIONAL_LIMIT",
                    "configurations": current, "regimes": regimes,
                    "start_probes_by_operation": dict(start_probes_by_operation),
                    "rounds": rounds, "selected_result": None,
                    "worker_domain_accounting": accounting,
                    "machine_domain_accounting": machine_accounting,
                    "remaining_truncated_operations": sorted(
                        key for key in active
                        if (accounting.get(key, {}).get("worker_domain_truncated") or
                            machine_accounting.get(key, {}).get(
                                "machine_domain_truncated"))),
                    "total_new_crews_generated": sum(added_counts.values()),
                    "total_new_machine_alternatives_generated": sum(
                        added_machine_counts.values()),
                    "final_resource_configurations": sum(len(v) for v in current.values()),
                    "final_calendar_regimes": sum(len(v) for v in regimes.values()),
                    "reason": "MAX_WORKER_RECOVERY_ROUNDS",
                    "safety_bound_rounds": int(policy.max_rounds),
                    "peak_rss_mb": peak_rss_mb}
        if adaptive_round == 1 and initial_pressure_diagnosis is not None:
            diagnosis = tuple(initial_pressure_diagnosis)
        else:
            diagnosis = diagnose_worker_route_pressure(
                state, current, regimes, resource_audit)
        accounting_before = worker_domain_accounting(current, resource_audit, active)
        machine_accounting_before = machine_domain_accounting(
            current, resource_audit, active)
        ranked = list(rank_adaptive_resource_bottlenecks(
            diagnosis, accounting_before, machine_accounting_before, current))
        relevant_operations.update(str(row["operation_key"]) for row in ranked)
        fresh_targets = [row["operation_key"] for row in ranked]
        target_index = min(adaptive_round - 1,
                           len(policy.pressure_targets_by_round) - 1)
        target_limit = int(policy.pressure_targets_by_round[target_index])
        if policy.max_pressure_targets_per_round is not None:
            target_limit = min(target_limit,
                               int(policy.max_pressure_targets_per_round))
        target_limit = min(target_limit, len(ranked))
        round_targets = []
        for key in fresh_targets:
            if key not in diagnosed_targets:
                diagnosed_targets.append(key)
                round_targets.append(key)
            if len(round_targets) >= target_limit:
                break
        # If the ranked set has fewer unseen operations than the per-round
        # target budget, keep exposing additional legal crews for the most
        # constrained already-diagnosed operations.  This preserves progress
        # for a single pressured operation without ever broadening a round to
        # every pressured operation in the neighborhood.
        if len(round_targets) < target_limit:
            for row in ranked:
                key = str(row["operation_key"])
                if key in diagnosed_targets and key not in round_targets:
                    round_targets.append(key)
                if len(round_targets) >= target_limit:
                    break
        if not diagnosed_targets and not relevant_operations:
            # Legacy callers without an audit cannot establish exhaustion.
            status = ("WORKER_CREW_DOMAIN_STILL_TRUNCATED" if not resource_audit
                      else "WORKER_DOMAIN_NO_PROGRESS_INCONSISTENCY")
            return {"status": status, "configurations": current,
                    "regimes": regimes, "rounds": rounds,
                    "pressure_diagnosis": diagnosis,
                    "worker_domain_accounting": accounting_before,
                    "selected_result": None}

        budget_index = adaptive_round - 1
        if budget_index < len(policy.total_new_crews_per_operation_by_round):
            total_budget = int(policy.total_new_crews_per_operation_by_round[budget_index])
        else:
            total_budget = int(policy.total_new_crews_per_operation_by_round[-1]) * (
                2 ** (budget_index - len(policy.total_new_crews_per_operation_by_round) + 1))
        machine_budget_index = min(
            budget_index,
            len(policy.total_new_machine_alternatives_per_operation_by_round) - 1)
        total_machine_budget = int(
            policy.total_new_machine_alternatives_per_operation_by_round[
                machine_budget_index])
        round_traces = []
        machine_traces = []
        resource_configuration_seconds = 0.0
        closure_seconds = 0.0
        additions = {}
        machine_additions = {}
        # Only the newly ranked pressure targets are expanded in this round;
        # causal successors discovered by the closure are added separately.
        # ``relevant_operations`` remains diagnostic/accounting state and is
        # never a reason to materialize every pressured operation at once.
        expansion_keys = tuple(dict.fromkeys(round_targets))
        operation_limit = (len(expansion_keys) if policy.max_expansion_operations is None
                           else int(policy.max_expansion_operations))
        for key in expansion_keys[:operation_limit]:
            if not machine_accounting_before.get(str(key), {}).get(
                    "machine_domain_truncated", False):
                continue
            known_machine_remaining = machine_accounting_before.get(str(key), {}).get(
                "remaining_legal_machine_sets")
            machine_remaining = max(
                0, total_machine_budget - int(added_machine_counts[str(key)]))
            if known_machine_remaining is not None:
                machine_remaining = min(machine_remaining,
                                        int(known_machine_remaining))
            if machine_remaining <= 0:
                continue
            resource_started = time.perf_counter()
            generated, trace = generate_additional_machine_configurations(
                state, current, key, machine_remaining)
            resource_configuration_seconds += time.perf_counter() - resource_started
            machine_traces.append(trace)
            if generated:
                machine_additions[str(key)] = generated
                added_machine_counts[str(key)] += int(
                    trace.get("new_machine_alternatives_accepted", 0))
        machine_expanded = merge_additional_configurations(
            current, machine_additions)
        for key in expansion_keys[:operation_limit]:
            if not accounting_before.get(str(key), {}).get("worker_domain_truncated", False):
                continue
            ready, source = _route_ready_seconds(state, key, previous_bounds)
            if ready is None:
                round_traces.append({"operation_key": key, "status": "UNRESOLVED_ROUTE_READY", "source": source})
                continue
            known_remaining = accounting_before.get(str(key), {}).get("remaining_legal_crews")
            remaining = max(0, total_budget - int(added_counts[key]))
            if known_remaining is not None:
                remaining = min(remaining, int(known_remaining))
            if remaining <= 0:
                continue
            resource_started = time.perf_counter()
            generated, trace = generate_additional_crew_configurations(
                state, availability, machine_expanded, key, ready, remaining,
                worker_pressure=worker_pressure,
                additional_start_probes=authoritative_start_probes(
                    state, availability, machine_expanded, key, ready))
            resource_configuration_seconds += time.perf_counter() - resource_started
            trace["route_ready_source"] = source
            round_traces.append(trace)
            if generated:
                additions[key] = generated
                added_counts[key] += len(generated)
        resource_started = time.perf_counter()
        expanded = merge_additional_configurations(machine_expanded, additions)
        resource_configuration_seconds += time.perf_counter() - resource_started
        for trace in round_traces:
            if trace.get("new_crews_accepted") and trace.get("start_probe_seconds") is not None:
                op_key = str(trace["operation_key"])
                start_probes_by_operation[op_key] = tuple(sorted({
                    *start_probes_by_operation.get(op_key, ()),
                    *(int(value) for value in trace.get("selected_start_probes", (trace["start_probe_seconds"],))),
                }))
        first_superset = audit_strict_superset(current, expanded)
        if not first_superset["strict_superset_or_equal"]:
            raise AssertionError("adaptive crew expansion removed an existing configuration")
        expanded_configuration_count = sum(len(values) for values in expanded.values())
        if (policy.max_resource_configurations is not None and
                expanded_configuration_count > int(policy.max_resource_configurations)):
            return {
                "status": "COMPUTATIONAL_LIMIT",
                "configurations": current, "regimes": regimes,
                "rounds": rounds, "selected_result": None,
                "worker_domain_accounting": accounting_before,
                "machine_domain_accounting": machine_accounting_before,
                "remaining_truncated_operations": sorted(
                    str(row["operation_key"]) for row in ranked),
                "reason": "MAX_RESOURCE_CONFIGURATIONS",
                "attempted_resource_configuration_count": expanded_configuration_count,
                "resource_configuration_limit": int(
                    policy.max_resource_configurations),
                "peak_rss_mb": peak_rss_mb,
            }
        expanded_regimes, expanded_meta = build_regimes(expanded, start_probes_by_operation)
        expanded_regime_count = sum(len(values) for values in expanded_regimes.values())
        observed_rss = _process_peak_rss_mb()
        if observed_rss is not None:
            peak_rss_mb = max(float(peak_rss_mb or 0.0), float(observed_rss))
        if (policy.max_calendar_regimes is not None and
                expanded_regime_count > int(policy.max_calendar_regimes)):
            return {
                "status": "COMPUTATIONAL_LIMIT",
                "configurations": current, "regimes": regimes,
                "rounds": rounds, "selected_result": None,
                "worker_domain_accounting": accounting_before,
                "machine_domain_accounting": machine_accounting_before,
                "remaining_truncated_operations": sorted(
                    str(row["operation_key"]) for row in ranked),
                "reason": "MAX_CALENDAR_EXECUTION_REGIMES",
                "attempted_calendar_regime_count": expanded_regime_count,
                "calendar_regime_limit": int(policy.max_calendar_regimes),
                "peak_rss_mb": peak_rss_mb,
            }
        if (policy.max_peak_rss_mb is not None and peak_rss_mb is not None and
                float(peak_rss_mb) >= float(policy.max_peak_rss_mb)):
            return {
                "status": "COMPUTATIONAL_LIMIT",
                "configurations": current, "regimes": regimes,
                "rounds": rounds, "selected_result": None,
                "worker_domain_accounting": accounting_before,
                "machine_domain_accounting": machine_accounting_before,
                "remaining_truncated_operations": sorted(
                    str(row["operation_key"]) for row in ranked),
                "reason": "MAX_PEAK_RSS_MB",
                "peak_rss_mb": peak_rss_mb,
                "peak_rss_limit_mb": float(policy.max_peak_rss_mb),
            }
        expanded_bounds = _routing_aware_earliest(state, expanded_regimes)

        closure_started = time.perf_counter()
        selected_closure, closure_reasons = select_route_successor_closure(
            state, diagnosed_targets, previous_bounds, expanded_bounds, resource_audit,
            max_depth=policy.max_successor_depth,
            max_operations=policy.max_expansion_operations,
            configurations=expanded, availability=availability)
        closure_seconds += time.perf_counter() - closure_started
        closure = [key for key in selected_closure if key not in diagnosed_targets]
        relevant_operations.update(str(key) for key in selected_closure)
        reason_keys = {(str(row.get("operation_key")), str(row.get("cause")))
                       for row in closure_reasons}
        known_factory_keys = state.get("factory_operation_keys") or state.get("known_operation_keys")
        closure_report = {
            "closure_seed_count": len(diagnosed_targets),
            "closure_final_count": len(selected_closure),
            "max_route_depth_observed": max(
                (int(row.get("depth", 0)) for row in closure_reasons), default=0),
            "routing_neighbors_added": sum(
                1 for row in closure_reasons
                if str(row.get("cause", "")).startswith("ROUTING")),
            "resource_conflicts_added": sum(
                1 for row in closure_reasons
                if str(row.get("cause", "")) == "RESOURCE_CONTENTION"),
            "displaced_operations_added": sum(
                1 for row in closure_reasons
                if str(row.get("cause", "")) == "DISPLACED_PROVISIONAL"),
            "duplicate_visits_skipped": max(0, len(closure_reasons) - len(reason_keys)),
            # ``state["keys"]`` is the bounded local planning universe, not
            # the full factory universe.  Only claim factory-wide reach when
            # the caller explicitly supplies that universe.
            "entire_factory_reached": bool(known_factory_keys) and
                set(selected_closure) >= {str(key) for key in known_factory_keys},
            "known_local_universe_count": len(active),
            "known_factory_universe_count": (len(known_factory_keys)
                                              if known_factory_keys else None),
        }
        closure_additions = {}
        # Causal closure is retained for diagnostics and future targeting, but
        # worker-domain exposure remains bounded by the same per-round target
        # budget.  In particular, a closure around one pressure seed must not
        # fan out into every operation in the local neighborhood in one round.
        closure_budget = max(0, target_limit - len(round_targets))
        for reason in closure_reasons:
            key = reason["operation_key"]
            if len(closure_additions) >= closure_budget:
                break
            if (policy.max_expansion_operations is not None and
                    len(additions) + len(closure_additions) >= int(policy.max_expansion_operations)):
                break
            ready = reason.get("expanded_ready_seconds")
            accounting_now = worker_domain_accounting(expanded, resource_audit, active)
            if not accounting_now.get(str(key), {}).get("worker_domain_truncated", False):
                continue
            known_remaining = accounting_now.get(str(key), {}).get("remaining_legal_crews")
            remaining = max(0, total_budget - int(added_counts[key]))
            if known_remaining is not None:
                remaining = min(remaining, int(known_remaining))
            if ready is None or remaining <= 0:
                continue
            resource_started = time.perf_counter()
            generated, trace = generate_additional_crew_configurations(
                state, availability, expanded, key, int(ready), remaining,
                worker_pressure=worker_pressure,
                additional_start_probes=authoritative_start_probes(
                    state, availability, expanded, key, int(ready)))
            resource_configuration_seconds += time.perf_counter() - resource_started
            trace["route_ready_source"] = "EXPANDED_PREDECESSOR_EARLIEST_END"
            trace["closure_reason"] = reason
            round_traces.append(trace)
            if generated:
                closure_additions[key] = generated
                added_counts[key] += len(generated)
        if closure_additions:
            resource_started = time.perf_counter()
            expanded = merge_additional_configurations(expanded, closure_additions)
            resource_configuration_seconds += time.perf_counter() - resource_started
            for trace in round_traces:
                if trace.get("new_crews_accepted") and trace.get("start_probe_seconds") is not None:
                    op_key = str(trace["operation_key"])
                    start_probes_by_operation[op_key] = tuple(sorted({
                        *start_probes_by_operation.get(op_key, ()),
                        *(int(value) for value in trace.get("selected_start_probes", (trace["start_probe_seconds"],))),
                    }))
            expanded_regimes, expanded_meta = build_regimes(expanded, start_probes_by_operation)
            expanded_bounds = _routing_aware_earliest(state, expanded_regimes)

        superset = audit_strict_superset(original, expanded)
        if not superset["strict_superset_or_equal"]:
            raise AssertionError("expanded domain is not a superset of the base domain")
        accounting_after = worker_domain_accounting(expanded, resource_audit, active)
        machine_accounting_after = machine_domain_accounting(
            expanded, resource_audit, active)
        operations_still_truncated = sorted(
            key for key in relevant_operations
            if (accounting_after.get(str(key), {}).get(
                    "worker_domain_truncated", False) or
                machine_accounting_after.get(str(key), {}).get(
                    "machine_domain_truncated", False)))
        result = solve_round(adaptive_round, expanded, expanded_regimes, expanded_meta)
        observed_rss = _process_peak_rss_mb()
        if observed_rss is not None:
            peak_rss_mb = max(float(peak_rss_mb or 0.0), float(observed_rss))
        selected_reasons = [
            {
                "operation_key": str(row["operation_key"]),
                "why_selected": list(row.get("bottleneck_reasons") or ()),
                "route_delay_seconds": row.get("crew_delay_seconds"),
                "worker_contention_score": row.get("worker_contention_score", 0),
                "machine_contention_score": row.get("machine_contention_score", 0),
                "no_executable_exposed_start": bool(
                    row.get("no_executable_exposed_start", False)),
                "predecessor_blocked": bool(row.get("predecessor_blocked", False)),
            }
            for row in ranked if str(row["operation_key"]) in set(expansion_keys)
        ]
        rounds.append({
            "round": adaptive_round,
            "trigger": "TRUNCATED_WORKER_ROUTE_PRESSURE",
            "pressure_diagnosis": diagnosis,
            "operations_expanded": sorted(
                set(additions) | set(machine_additions) | set(closure_additions)),
            "why_selected": selected_reasons,
            "closure": list(closure_reasons),
            "closure_report": closure_report,
            "crew_generation": round_traces,
            "machine_generation": machine_traces,
            "added_crews_by_operation": dict(sorted(added_counts.items())),
            "added_machine_alternatives_by_operation": dict(
                sorted(added_machine_counts.items())),
            "operations_still_truncated_before": sorted(
                key for key in relevant_operations
                if accounting_before.get(str(key), {}).get("worker_domain_truncated", False)),
            "remaining_truncated_operations": operations_still_truncated,
            "worker_domain_accounting": accounting_after,
            "machine_domain_accounting": machine_accounting_after,
            "domain_exhausted": not operations_still_truncated,
            "new_crews_attempted": sum(int(row.get("candidate_crews_generated", 0))
                                       for row in round_traces),
            "new_crews_accepted": sum(int(row.get("new_crews_accepted", 0))
                                      for row in round_traces),
            "new_machine_alternatives_accepted": sum(
                int(row.get("new_machine_alternatives_accepted", 0))
                for row in machine_traces),
            "closure_seconds": float(closure_seconds),
            "resource_configuration_seconds": float(resource_configuration_seconds),
            "matching_generation_seconds": sum(
                float(row.get("matching_generation_seconds", 0.0) or 0.0)
                for row in round_traces),
            "calendar_regime_seconds": float(
                expanded_meta.get("calendar_generation_seconds", 0.0) or 0.0),
            "round_runtime_seconds": time.perf_counter() - round_started,
            "cumulative_runtime_seconds": time.perf_counter() - recovery_started,
            "peak_rss_mb": peak_rss_mb,
            "domain_superset": superset,
            "solve": _compact_solve_diagnostic(result),
        })
        current = expanded
        regimes = expanded_regimes
        previous_bounds = expanded_bounds
        if result.get("accepted"):
            return {"status": "ADAPTIVE_CREW_EXPANSION_ACCEPTED",
                    "configurations": current, "regimes": regimes,
                    "rounds": rounds, "pressure_diagnosis": diagnosis,
                    "selected_result": result}
        # A CP incumbent that fails materialization, replay, or validation is
        # not an invitation to keep expanding the worker domain.  Production
        # timing semantics permit a second attempt only when the first solve
        # produced no incumbent at all; preserve the failed candidate as the
        # truthful stopping reason.
        if result.get("candidate_exists"):
            return {"status": "RECOVERY_CANDIDATE_REJECTED",
                    "configurations": current, "regimes": regimes,
                    "rounds": rounds, "pressure_diagnosis": diagnosis,
                    "selected_result": None,
                    "rejected_candidate": result}
        if not operations_still_truncated:
            return {"status": "WORKER_CREW_DOMAIN_EXHAUSTED_NO_CANDIDATE",
                    "configurations": current, "regimes": regimes,
                    "rounds": rounds, "pressure_diagnosis": diagnosis,
                    "worker_domain_accounting": accounting_after,
                    "selected_result": None,
                    "total_new_crews_generated": sum(added_counts.values()),
                    "final_resource_configurations": sum(len(v) for v in current.values()),
                    "final_calendar_regimes": sum(len(v) for v in regimes.values())}
        if not additions and not machine_additions and not closure_additions:
            return {"status": "WORKER_DOMAIN_NO_PROGRESS_INCONSISTENCY",
                    "configurations": current, "regimes": regimes,
                    "rounds": rounds, "pressure_diagnosis": diagnosis,
                    "worker_domain_accounting": accounting_after,
                    "remaining_truncated_operations": operations_still_truncated,
                    "selected_result": None,
                    "total_new_crews_generated": sum(added_counts.values()),
                    "final_resource_configurations": sum(len(v) for v in current.values()),
                    "final_calendar_regimes": sum(len(v) for v in regimes.values())}
        adaptive_round += 1
    # A complete causal closure was traversed.  If Production V1 reaches this
    # point, the remaining limitation is the explicitly configured number of
    # adaptive rounds, not an artificial operation/depth closure cutoff.
    terminal_status = (
        "WORKER_CREW_DOMAIN_STILL_TRUNCATED"
        if policy.max_expansion_operations is None and policy.max_successor_depth is None
        else "ROUTE_SUCCESSOR_CLOSURE_BUDGET_EXCEEDED"
    )
    return {"status": terminal_status, "configurations": current,
            "regimes": regimes, "rounds": rounds, "selected_result": None}


def serialize_policy(policy: RouteAwareCrewExpansionPolicyV1) -> dict:
    return asdict(policy)
