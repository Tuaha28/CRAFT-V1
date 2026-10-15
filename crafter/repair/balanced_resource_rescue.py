"""Generic bounded breadth rescue for a truncated local CRAFT domain.

The normal adaptive recovery deliberately follows the strongest bottleneck
evidence.  That depth-first behaviour can leave a few operations with a very
wide exposed domain while other active operations retain only the compact
seed domain.  This module is the single, final breadth pass used after the
normal recovery safety bound is reached.

Nothing here relaxes a physical rule.  All additions are produced by the
authoritative machine generator and fixed-crew matcher, then passed through
the canonical Calendar V1 generator and unchanged CP/replay/validator path.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import math
import statistics
import time
from typing import Callable, Mapping, Sequence

from .engine import _routing_aware_earliest
from .route_aware_crew_expansion import (
    _process_peak_rss_mb,
    _route_ready_seconds,
    authoritative_start_probes,
    build_worker_contention,
    configuration_signature,
    generate_additional_crew_configurations,
    generate_additional_machine_configurations,
    machine_domain_accounting,
    merge_additional_configurations,
    worker_domain_accounting,
)


BALANCED_RESCUE_PROVENANCE = "BALANCED_RESOURCE_FEASIBILITY_RESCUE_V1"


@dataclass(frozen=True)
class BalancedResourceRescuePolicyV1:
    """Operational ceilings and a compact cross-operation coverage floor."""

    minimum_configurations_low: int = 8
    minimum_configurations_high: int = 12
    minimum_worker_crews: int = 4
    minimum_machine_shapes: int = 2
    max_new_worker_crews_per_operation: int = 12
    max_new_machine_alternatives_per_operation: int = 3
    max_resource_configurations: int = 800
    max_calendar_regimes: int = 7500
    max_peak_rss_mb: float = 1536.0
    max_runtime_seconds: float = 300.0

    def __post_init__(self):
        if not (1 <= int(self.minimum_configurations_low)
                <= int(self.minimum_configurations_high)):
            raise ValueError("invalid balanced configuration floor range")
        for name in (
                "minimum_worker_crews", "minimum_machine_shapes",
                "max_new_worker_crews_per_operation",
                "max_new_machine_alternatives_per_operation",
                "max_resource_configurations", "max_calendar_regimes"):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be positive")
        if float(self.max_peak_rss_mb) <= 0 or float(self.max_runtime_seconds) <= 0:
            raise ValueError("memory/runtime ceilings must be positive")


def _crew_signature(config) -> tuple[str, ...]:
    return tuple(sorted(str(worker) for worker in config.worker_ids))


def _machine_signature(config) -> tuple:
    return (int(config.line_id), int(config.k),
            tuple(int(machine) for machine in config.machine_ids))


def _calendar_pattern(state, config) -> tuple:
    """A deterministic diversity signature, not a feasibility shortcut."""
    index = state.get("operator_index") or {}
    crew_of = index.get("crew_of") or {}
    shift_of = index.get("shift_of") or {}
    values = []
    for worker in _crew_signature(config):
        if worker in crew_of:
            values.append(("crew",) + tuple(crew_of[worker]))
        elif worker in shift_of:
            values.append(("shift",) + tuple(shift_of[worker]))
        else:
            values.append(("always", worker))
    return tuple(sorted(values, key=repr))


def resource_coverage_report(state, configurations, regimes, resource_audit,
                             operation_keys=None) -> tuple[dict, ...]:
    """Describe exposed coverage without enumerating the legal cross-product."""
    keys = tuple(sorted(str(key) for key in (
        operation_keys if operation_keys is not None else state["keys"])))
    worker = worker_domain_accounting(configurations, resource_audit, keys)
    machine = machine_domain_accounting(configurations, resource_audit, keys)
    report = []
    for key in keys:
        configs = tuple(configurations.get(key, ()) or ())
        rows = tuple(regimes.get(key, ()) or ())
        crews = {_crew_signature(config) for config in configs}
        shapes = {_machine_signature(config) for config in configs}
        calendars = {_calendar_pattern(state, config) for config in configs}
        legal_crews = worker.get(key, {}).get("legal_worker_crews")
        legal_machines = machine.get(key, {}).get("legal_machine_sets")
        # This product is diagnostic only.  Pair dependencies can make it an
        # upper source count, so it is never used as an exhaustion target.
        legal_source_alternatives = (
            int(legal_crews) * int(legal_machines)
            if legal_crews is not None and legal_machines is not None else None)
        starts = [int(row["lo"]) for row in rows if row.get("lo") is not None]
        report.append({
            "operation_key": key,
            "exposed_resource_configurations": len(configs),
            "legal_resource_source_alternatives": legal_source_alternatives,
            "exposed_worker_crews": len(crews),
            "legal_worker_alternatives": legal_crews,
            "remaining_legal_worker_alternatives": worker.get(key, {}).get(
                "remaining_legal_crews"),
            "exposed_machine_shapes": len(shapes),
            "legal_machine_alternatives": legal_machines,
            "remaining_legal_machine_alternatives": machine.get(key, {}).get(
                "remaining_legal_machine_sets"),
            "exposed_calendar_patterns": len(calendars),
            "executable_calendar_regimes": len(rows),
            "earliest_executable_exposed_start": min(starts) if starts else None,
            "worker_domain_truncated": bool(worker.get(key, {}).get(
                "worker_domain_truncated")),
            "machine_domain_truncated": bool(machine.get(key, {}).get(
                "machine_domain_truncated")),
        })
    return tuple(report)


def derive_configuration_floor(coverage: Sequence[Mapping],
                               policy: BalancedResourceRescuePolicyV1) -> int:
    """Choose an 8--12 floor from the observed domain, deterministically."""
    counts = [int(row.get("exposed_resource_configurations", 0))
              for row in coverage]
    observed = int(round(statistics.median(counts))) if counts else 0
    return min(int(policy.minimum_configurations_high),
               max(int(policy.minimum_configurations_low), observed))


def operations_below_balanced_coverage(
        coverage: Sequence[Mapping], floor: int,
        policy: BalancedResourceRescuePolicyV1) -> tuple[str, ...]:
    selected = []
    for row in coverage:
        if not (row.get("worker_domain_truncated") or
                row.get("machine_domain_truncated")):
            continue
        legal_workers = row.get("legal_worker_alternatives")
        legal_machines = row.get("legal_machine_alternatives")
        desired_workers = min(int(policy.minimum_worker_crews), int(legal_workers)) \
            if legal_workers is not None else int(policy.minimum_worker_crews)
        desired_machines = min(int(policy.minimum_machine_shapes), int(legal_machines)) \
            if legal_machines is not None else int(policy.minimum_machine_shapes)
        if (int(row.get("exposed_resource_configurations", 0)) < int(floor) or
                int(row.get("exposed_worker_crews", 0)) < desired_workers or
                int(row.get("exposed_machine_shapes", 0)) < desired_machines or
                int(row.get("executable_calendar_regimes", 0)) == 0):
            selected.append(str(row["operation_key"]))
    return tuple(selected)


def _projected_regime_cost(coverage_by_key, key) -> int:
    row = coverage_by_key[str(key)]
    configs = max(1, int(row.get("exposed_resource_configurations", 0)))
    regimes = int(row.get("executable_calendar_regimes", 0))
    return max(1, int(math.ceil(float(regimes) / float(configs))))


def _compact_solve(result: Mapping | None) -> dict:
    result = result if isinstance(result, Mapping) else {}
    source = result.get("candidate") if isinstance(result.get("candidate"), Mapping) else result
    acceptance = source.get("acceptance", {}) or {}
    stages = tuple(acceptance.get("stages") or ())
    winning = acceptance.get("winning_stage") or (stages[-1] if stages else {})
    return {
        "accepted": bool(result.get("accepted", False)),
        "candidate_exists": bool(result.get("candidate_exists", False) or
                                  acceptance.get("selected_schedule")),
        "cp_status": winning.get("status"),
        "materialization_mismatch": int(
            (acceptance.get("materialization") or {}).get("mismatch_count", 0) or 0),
        "replay": (acceptance.get("replay") or {}).get("status"),
        "physical": (acceptance.get("validator") or {}).get("status"),
        "cp_model_build_seconds": sum(float(row.get("model_build_seconds", 0.0) or 0.0)
                                      for row in stages if isinstance(row, Mapping)),
        "cp_solve_seconds": sum(float(row.get("solve_seconds", 0.0) or 0.0)
                                for row in stages if isinstance(row, Mapping)),
    }


def run_balanced_resource_feasibility_rescue(
        state,
        availability,
        configurations,
        regimes,
        resource_audit,
        build_regimes: Callable,
        solve_feasibility: Callable,
        solve_objective: Callable,
        *,
        existing_start_probes=None,
        policy: BalancedResourceRescuePolicyV1 | None = None):
    """Apply one bounded breadth expansion, then feasibility-first CP.

    The returned configuration/regime dictionaries are exact strict
    supersets of the supplied domain.  Full solve objects are retained only
    for the selected result; diagnostic history is compact.
    """
    policy = policy or BalancedResourceRescuePolicyV1()
    started = time.perf_counter()
    peak_rss = _process_peak_rss_mb()
    current = {str(key): tuple(values) for key, values in configurations.items()}
    current_regimes = {str(key): tuple(values) for key, values in regimes.items()}
    coverage_before = resource_coverage_report(
        state, current, current_regimes, resource_audit)
    coverage_by_key = {row["operation_key"]: row for row in coverage_before}
    floor = derive_configuration_floor(coverage_before, policy)
    targets = operations_below_balanced_coverage(coverage_before, floor, policy)
    config_count_before = sum(len(values) for values in current.values())
    regime_count_before = sum(len(values) for values in current_regimes.values())
    remaining_config_slots = max(
        0, int(policy.max_resource_configurations) - config_count_before)
    remaining_regime_slots = max(
        0, int(policy.max_calendar_regimes) - regime_count_before)
    worker_pressure = build_worker_contention(resource_audit, state["keys"])
    route_bounds = _routing_aware_earliest(state, current_regimes)
    additions: dict[str, tuple] = {}
    worker_added: dict[str, int] = {}
    machine_added: dict[str, int] = {}
    generation_trace = []
    start_probes: dict[str, tuple[int, ...]] = {
        str(key): tuple(sorted({int(value) for value in values}))
        for key, values in (existing_start_probes or {}).items()
    }
    stop_reason = None

    # Largest coverage deficiency first; key is the deterministic tie-break.
    ranked_targets = sorted(targets, key=lambda key: (
        int(coverage_by_key[key]["exposed_resource_configurations"]) - floor,
        int(coverage_by_key[key]["exposed_machine_shapes"]),
        int(coverage_by_key[key]["exposed_worker_crews"]),
        key,
    ))
    for key in ranked_targets:
        elapsed = time.perf_counter() - started
        observed = _process_peak_rss_mb()
        if observed is not None:
            peak_rss = max(float(peak_rss or 0.0), float(observed))
        if elapsed >= float(policy.max_runtime_seconds):
            stop_reason = "MAX_RESCUE_RUNTIME_SECONDS"
            break
        if peak_rss is not None and peak_rss >= float(policy.max_peak_rss_mb):
            stop_reason = "MAX_PEAK_RSS_MB"
            break
        if remaining_config_slots <= 0 or remaining_regime_slots <= 0:
            stop_reason = "DOMAIN_SIZE_CEILING"
            break

        before = coverage_by_key[key]
        desired_machine_shapes = min(
            int(policy.minimum_machine_shapes),
            int(before["legal_machine_alternatives"])
            if before.get("legal_machine_alternatives") is not None
            else int(policy.minimum_machine_shapes))
        machine_quota = min(
            int(policy.max_new_machine_alternatives_per_operation),
            max(0, desired_machine_shapes - int(before["exposed_machine_shapes"])),
            int(before["remaining_legal_machine_alternatives"])
            if before.get("remaining_legal_machine_alternatives") is not None
            else int(policy.max_new_machine_alternatives_per_operation),
            remaining_config_slots,
        ) if before.get("machine_domain_truncated") else 0
        machine_rows, machine_trace = generate_additional_machine_configurations(
            state, current, key, machine_quota)
        projected_cost = _projected_regime_cost(coverage_by_key, key)
        affordable_machine = min(
            len(machine_rows), remaining_config_slots,
            remaining_regime_slots // projected_cost)
        machine_rows = tuple(replace(
            row, provenance=tuple(row.provenance) +
            (BALANCED_RESCUE_PROVENANCE,))
            for row in machine_rows[:affordable_machine])
        if machine_rows:
            additions[key] = machine_rows
            machine_added[key] = len(machine_rows)
            remaining_config_slots -= len(machine_rows)
            remaining_regime_slots -= len(machine_rows) * projected_cost

        staged = merge_additional_configurations(current, additions)
        current_count = len(staged.get(key, ()))
        config_gap = max(0, int(floor) - current_count)
        desired_crews = min(
            int(policy.minimum_worker_crews),
            int(before["legal_worker_alternatives"])
            if before.get("legal_worker_alternatives") is not None
            else int(policy.minimum_worker_crews))
        crew_gap = max(0, desired_crews - int(before["exposed_worker_crews"]))
        worker_quota = min(
            int(policy.max_new_worker_crews_per_operation),
            max(config_gap, crew_gap),
            int(before["remaining_legal_worker_alternatives"])
            if before.get("remaining_legal_worker_alternatives") is not None
            else int(policy.max_new_worker_crews_per_operation),
            remaining_config_slots,
            remaining_regime_slots // projected_cost,
        ) if before.get("worker_domain_truncated") else 0
        ready, ready_source = _route_ready_seconds(state, key, route_bounds)
        if ready is None:
            ready = before.get("earliest_executable_exposed_start")
            ready_source = "EARLIEST_EXPOSED_REGIME"
        worker_rows = ()
        worker_trace = {"operation_key": key, "new_crews_accepted": 0}
        if ready is not None and worker_quota > 0:
            worker_rows, worker_trace = generate_additional_crew_configurations(
                state, availability, staged, key, int(ready), worker_quota,
                worker_pressure=worker_pressure,
                additional_start_probes=authoritative_start_probes(
                    state, availability, staged, key, int(ready)),
                diversity_mode=True)
            affordable_workers = min(
                len(worker_rows), remaining_config_slots,
                remaining_regime_slots // projected_cost)
            worker_rows = tuple(replace(
                row, provenance=tuple(row.provenance) +
                (BALANCED_RESCUE_PROVENANCE,))
                for row in worker_rows[:affordable_workers])
            if worker_rows:
                additions[key] = tuple(additions.get(key, ())) + worker_rows
                worker_added[key] = len(worker_rows)
                remaining_config_slots -= len(worker_rows)
                remaining_regime_slots -= len(worker_rows) * projected_cost
                start_probes[key] = tuple(sorted(
                    set(start_probes.get(key, ())) |
                    {int(value) for value in worker_trace.get(
                        "selected_start_probes", (int(ready),))}))
        generation_trace.append({
            "operation_key": key,
            "coverage_before": dict(before),
            "route_ready_seconds": ready,
            "route_ready_source": ready_source,
            "projected_regimes_per_configuration": projected_cost,
            "machine_trace": machine_trace,
            "worker_trace": worker_trace,
            "machine_alternatives_added": len(machine_rows),
            "worker_alternatives_added": len(worker_rows),
        })

    expanded = merge_additional_configurations(current, additions)
    config_count_after_generation = sum(len(values) for values in expanded.values())
    if config_count_after_generation > int(policy.max_resource_configurations):
        raise AssertionError("balanced rescue exceeded its configuration ceiling")
    if config_count_after_generation == config_count_before:
        coverage_after = coverage_before
        return {
            "status": "COMPUTATIONAL_LIMIT",
            "reason": stop_reason or "BALANCED_RESCUE_NO_PROGRESS",
            "configurations": current,
            "regimes": current_regimes,
            "coverage_floor": floor,
            "coverage_before": coverage_before,
            "coverage_after": coverage_after,
            "operations_below_minimum_coverage": list(targets),
            "worker_alternatives_added": worker_added,
            "machine_alternatives_added": machine_added,
            "generation_trace": generation_trace,
            "feasibility": {"cp_status": "NOT_REACHED"},
            "objective": {"cp_status": "NOT_REACHED"},
            "selected_result": None,
            "peak_rss_mb": peak_rss,
            "runtime_seconds": time.perf_counter() - started,
        }

    expanded_regimes, regime_meta = build_regimes(expanded, start_probes)
    regime_count_after = sum(len(values) for values in expanded_regimes.values())
    observed = _process_peak_rss_mb()
    if observed is not None:
        peak_rss = max(float(peak_rss or 0.0), float(observed))
    if regime_count_after > int(policy.max_calendar_regimes):
        # Projections are conservative for the current generator.  This
        # branch is still a strict safety stop: it never solves an oversized
        # domain and never calls the result infeasible.
        return {
            "status": "COMPUTATIONAL_LIMIT",
            "reason": "MAX_CALENDAR_REGIMES",
            "attempted_calendar_regimes": regime_count_after,
            "configurations": current,
            "regimes": current_regimes,
            "coverage_floor": floor,
            "coverage_before": coverage_before,
            "coverage_after": coverage_before,
            "operations_below_minimum_coverage": list(targets),
            "worker_alternatives_added": worker_added,
            "machine_alternatives_added": machine_added,
            "generation_trace": generation_trace,
            "feasibility": {"cp_status": "NOT_REACHED"},
            "objective": {"cp_status": "NOT_REACHED"},
            "selected_result": None,
            "peak_rss_mb": peak_rss,
            "runtime_seconds": time.perf_counter() - started,
        }
    if (peak_rss is not None and peak_rss >= float(policy.max_peak_rss_mb)):
        return {
            "status": "COMPUTATIONAL_LIMIT", "reason": "MAX_PEAK_RSS_MB",
            "configurations": expanded, "regimes": expanded_regimes,
            "coverage_floor": floor, "coverage_before": coverage_before,
            "coverage_after": resource_coverage_report(
                state, expanded, expanded_regimes, resource_audit),
            "operations_below_minimum_coverage": list(targets),
            "worker_alternatives_added": worker_added,
            "machine_alternatives_added": machine_added,
            "generation_trace": generation_trace,
            "feasibility": {"cp_status": "NOT_REACHED"},
            "objective": {"cp_status": "NOT_REACHED"},
            "selected_result": None, "peak_rss_mb": peak_rss,
            "runtime_seconds": time.perf_counter() - started,
        }
    if time.perf_counter() - started >= float(policy.max_runtime_seconds):
        return {
            "status": "COMPUTATIONAL_LIMIT",
            "reason": "MAX_RESCUE_RUNTIME_SECONDS",
            "configurations": expanded, "regimes": expanded_regimes,
            "coverage_floor": floor, "coverage_before": coverage_before,
            "coverage_after": resource_coverage_report(
                state, expanded, expanded_regimes, resource_audit),
            "operations_below_minimum_coverage": list(targets),
            "worker_alternatives_added": worker_added,
            "machine_alternatives_added": machine_added,
            "generation_trace": generation_trace,
            "feasibility": {"cp_status": "NOT_REACHED"},
            "objective": {"cp_status": "NOT_REACHED"},
            "selected_result": None, "peak_rss_mb": peak_rss,
            "runtime_seconds": time.perf_counter() - started,
        }

    coverage_after = resource_coverage_report(
        state, expanded, expanded_regimes, resource_audit)
    feasibility_result = solve_feasibility(expanded, expanded_regimes, regime_meta)
    feasibility = _compact_solve(feasibility_result)
    observed = _process_peak_rss_mb()
    if observed is not None:
        peak_rss = max(float(peak_rss or 0.0), float(observed))
    if feasibility.get("cp_status") not in {"FEASIBLE", "OPTIMAL"}:
        worker_after = worker_domain_accounting(
            expanded, resource_audit, state["keys"])
        machine_after = machine_domain_accounting(
            expanded, resource_audit, state["keys"])
        exhausted = all(
            not worker_after[key].get("worker_domain_truncated") and
            not machine_after[key].get("machine_domain_truncated")
            for key in worker_after)
        return {
            "status": "COMPUTATIONAL_LIMIT",
            "reason": "BALANCED_FEASIBILITY_DOMAIN_REJECTED",
            "configurations": expanded, "regimes": expanded_regimes,
            "coverage_floor": floor, "coverage_before": coverage_before,
            "coverage_after": coverage_after,
            "operations_below_minimum_coverage": list(targets),
            "worker_alternatives_added": worker_added,
            "machine_alternatives_added": machine_added,
            "generation_trace": generation_trace,
            "feasibility": feasibility,
            "objective": {"cp_status": "NOT_REACHED"},
            "selected_result": None,
            "domain_exhausted": exhausted,
            "peak_rss_mb": peak_rss,
            "runtime_seconds": time.perf_counter() - started,
            "resource_configuration_count_before": config_count_before,
            "resource_configuration_count_after": config_count_after_generation,
            "calendar_regime_count_before": regime_count_before,
            "calendar_regime_count_after": regime_count_after,
        }

    incumbent = ((feasibility_result.get("candidate") or {}).get("acceptance") or {}).get(
        "selected_schedule", ())
    objective_result = solve_objective(
        expanded, expanded_regimes, regime_meta, incumbent)
    objective = _compact_solve(objective_result)
    observed = _process_peak_rss_mb()
    if observed is not None:
        peak_rss = max(float(peak_rss or 0.0), float(observed))
    accepted = bool(objective_result.get("accepted", False))
    return {
        "status": ("BALANCED_RESOURCE_RESCUE_ACCEPTED" if accepted
                   else "BALANCED_RESCUE_CANDIDATE_REJECTED"),
        "reason": None if accepted else "NORMAL_OBJECTIVE_ACCEPTANCE_FAILED",
        "configurations": expanded, "regimes": expanded_regimes,
        "coverage_floor": floor, "coverage_before": coverage_before,
        "coverage_after": coverage_after,
        "operations_below_minimum_coverage": list(targets),
        "worker_alternatives_added": worker_added,
        "machine_alternatives_added": machine_added,
        "generation_trace": generation_trace,
        "feasibility": feasibility, "objective": objective,
        "selected_result": objective_result if accepted else None,
        "domain_exhausted": False,
        "peak_rss_mb": peak_rss,
        "runtime_seconds": time.perf_counter() - started,
        "resource_configuration_count_before": config_count_before,
        "resource_configuration_count_after": config_count_after_generation,
        "calendar_regime_count_before": regime_count_before,
        "calendar_regime_count_after": regime_count_after,
        "policy": asdict(policy),
        "provenance": BALANCED_RESCUE_PROVENANCE,
    }
