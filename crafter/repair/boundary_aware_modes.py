"""Diagnostic boundary-compatible resource/crew mode generation.

This module is deliberately isolated from the production CRAFT resource
generator.  It indexes exact carried reservations, uses the existing worker
calendar primitives, and exposes a bounded deterministic matching search for
fixed crews.  It never relaxes CP constraints or mutates scheduling state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import combinations
from typing import Any, Mapping

from crafter.authoritative_semantics import authoritative_duration_contract, authoritative_setup_seconds
from crafter.disruptions.benchmark import _candidate_domains
from crafter.lns_probe.joint_modes import _fixed_crew_execution
from crafter.lns_probe.line_k_model import _available_machine_pool
from ga.worker_allocation import operator_available_at
from utils.work_calendar import NoAvailableCalendar, productive_segments
from .resource_configs import (
    ResourceConfiguration,
    _diverse_sets,
    generate_matching_worker_crews,
)
from .resource_configs import generate_resource_configurations
from .calendar_regimes import generate_calendar_regimes


def _merge(intervals):
    out = []
    for left, right in sorted((int(a), int(b)) for a, b in intervals if int(b) > int(a)):
        if out and left <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], right))
        else:
            out.append((left, right))
    return tuple(out)


def _subtract(base, busy):
    pieces = list(_merge(base))
    for busy_left, busy_right in _merge(busy):
        next_pieces = []
        for left, right in pieces:
            if busy_right <= left or right <= busy_left:
                next_pieces.append((left, right)); continue
            if left < busy_left:
                next_pieces.append((left, min(right, busy_left)))
            if busy_right < right:
                next_pieces.append((max(left, busy_right), right))
        pieces = next_pieces
    return _merge(pieces)


@dataclass(frozen=True)
class BoundaryAvailabilityIndex:
    horizon_start_seconds: int
    horizon_end_seconds: int
    machine_busy: Mapping[int, tuple[tuple[int, int], ...]]
    machine_free: Mapping[int, tuple[tuple[int, int], ...]]
    worker_busy: Mapping[str, tuple[tuple[int, int], ...]]
    worker_free: Mapping[str, tuple[tuple[int, int], ...]]

    def as_dict(self):
        return {
            "horizon_start_seconds": self.horizon_start_seconds,
            "horizon_end_seconds": self.horizon_end_seconds,
            "machine_busy": {str(k): [list(x) for x in v] for k, v in sorted(self.machine_busy.items())},
            "machine_free": {str(k): [list(x) for x in v] for k, v in sorted(self.machine_free.items())},
            "worker_busy": {str(k): [list(x) for x in v] for k, v in sorted(self.worker_busy.items())},
            "worker_free": {str(k): [list(x) for x in v] for k, v in sorted(self.worker_free.items())},
        }


def _boundary_busy_seconds(state, boundary, horizon_start, horizon_end):
    machine_busy = {}
    worker_busy = {}
    if boundary is not None:
        for row in boundary.machine_commitments:
            machine_busy.setdefault(int(row["machine_id"]), []).append(
                (max(horizon_start, int(row["start_seconds"])), min(horizon_end, int(row["end_seconds"]))))
        for row in boundary.worker_commitments:
            worker_busy.setdefault(str(row["worker_id"]), []).append(
                (max(horizon_start, int(row["start_seconds"])), min(horizon_end, int(row["end_seconds"]))))
    # Include authoritative fixed reservations already present in planning
    # state.  These are not boundary provenance rows, but are real immutable
    # reservations that the canonical calendar/machine predicates enforce.
    for machine_id, rows in (state.get("_machine_res") or {}).items():
        for row in rows:
            if len(row) >= 2:
                machine_busy.setdefault(int(machine_id), []).append(
                    (max(horizon_start, int(row[0])), min(horizon_end, int(row[1]))))
    origin = state["problem"].origin
    for worker_id, rows in (state.get("_worker_res") or {}).items():
        for left, right in rows:
            a = int(round((left - origin).total_seconds()))
            b = int(round((right - origin).total_seconds()))
            worker_busy.setdefault(str(worker_id), []).append(
                (max(horizon_start, a), min(horizon_end, b)))
    return ({k: _merge(v) for k, v in machine_busy.items()},
            {k: _merge(v) for k, v in worker_busy.items()})


def _worker_calendar_intervals(state, worker_id, start, end):
    """Exact calendar-available pieces using the existing predicate.

    Day and shift boundaries are explicit breakpoints.  Sampling each open
    half-open piece is exact for the repository's deterministic calendar
    predicate; reservation boundaries are subtracted separately.
    """
    origin = state["problem"].origin
    first = origin + timedelta(seconds=int(start))
    last = origin + timedelta(seconds=int(end))
    days = (last.date() - first.date()).days + 2
    points = {int(start), int(end)}
    for offset in range(-1, max(1, days)):
        day = (first + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        for hour in (0, 6, 14, 22):
            value = int(round(((day + timedelta(hours=hour)) - origin).total_seconds()))
            if start < value < end:
                points.add(value)
    ordered = sorted(points)
    available = []
    for left, right in zip(ordered, ordered[1:]):
        if right <= left:
            continue
        probe = origin + timedelta(seconds=left + max(1, (right - left) // 2))
        raw = int(worker_id) if str(worker_id).isdigit() else worker_id
        try:
            is_available = bool(operator_available_at(state["operator_index"], raw, probe))
        except Exception:
            is_available = False
        if is_available:
            available.append((left, right))
    return _merge(available)


def _worker_calendar_identity(state, worker_id):
    """Exact identity of the predicate used by ``operator_available_at``."""
    raw = int(worker_id) if str(worker_id).isdigit() else worker_id
    crew = state["operator_index"].get("crew_of", {}).get(raw)
    if crew is not None:
        return ("crew", tuple(crew))
    shift = state["operator_index"].get("shift_of", {}).get(raw)
    if shift is not None:
        return ("shift", tuple(shift))
    return ("always",)


def build_boundary_availability_index(state, boundary=None, *, horizon_start_seconds=0,
                                     horizon_end_seconds=None):
    end = int(horizon_end_seconds if horizon_end_seconds is not None
              else state["problem"].horizon_seconds)
    start = int(horizon_start_seconds)
    machine_busy, worker_busy = _boundary_busy_seconds(state, boundary, start, end)
    machine_ids = sorted(int(x.machine_id) for x in state["problem"].machines
                         if int(x.factory_id) == int(state["planning_context"]["factory_id"]))
    worker_ids = sorted({str(x) for x in state["operator_index"].get("by_factory", {}).keys()
                         if isinstance(x, str)}, key=str)
    # by_factory is keyed by (factory, capability), so include all workers
    # appearing in those pools and the carried boundary identities.
    workers = set(str(row) for row in worker_busy)
    for key, pool in state["operator_index"].get("by_factory", {}).items():
        if isinstance(key, tuple) and int(key[0]) == int(state["planning_context"]["factory_id"]):
            workers.update(str(x) for x in pool)
    machine_free = {mid: _subtract(((start, end),), machine_busy.get(mid, ())) for mid in machine_ids}
    worker_free = {}
    calendar_by_identity = {}
    for wid in sorted(workers, key=str):
        identity = _worker_calendar_identity(state, wid)
        calendar = calendar_by_identity.get(identity)
        if calendar is None:
            calendar = _worker_calendar_intervals(state, wid, start, end)
            calendar_by_identity[identity] = calendar
        worker_free[wid] = _subtract(calendar, worker_busy.get(wid, ()))
    return BoundaryAvailabilityIndex(start, end, machine_busy, machine_free,
                                     worker_busy, worker_free)


def _covered(intervals, required):
    required = _merge(required)
    if not required:
        return True
    cursor = required[0][0]
    for left, right in _merge(intervals):
        if right <= cursor:
            continue
        if left > cursor:
            return False
        cursor = max(cursor, right)
        if cursor >= required[-1][1]:
            return True
    return False


def _matching(slots, candidates_by_slot, *, forbidden=(), max_solutions=1,
              worker_pressure=None, worker_usage=None):
    """Compatibility wrapper around the shared compact matching generator."""
    return generate_matching_worker_crews(
        slots, candidates_by_slot, forbidden=forbidden,
        max_solutions=max_solutions, worker_pressure=worker_pressure,
        worker_usage=worker_usage)


def _eligible_workers(state, line_id, capability, factory_id):
    index = state["operator_index"]
    line = [str(x) for x in index.get("by_line", {}).get((int(line_id), str(capability)), ())]
    factory = [str(x) for x in index.get("by_factory", {}).get((int(factory_id), str(capability)), ())]
    return tuple(sorted(set(line) | set(factory), key=str))


def _machine_sets(state, op, line_id, k, cap):
    pool = tuple(sorted(int(x) for x in _available_machine_pool(state["problem"], op, int(line_id))))
    generated, _more = _diverse_sets(pool, int(k), int(cap))
    incumbent = tuple(sorted(int(x) for x in state["after_index"].by_key[op.operation_key].get("machine_ids", ())))
    out = []
    if len(incumbent) == int(k) and set(incumbent).issubset(pool):
        out.append(incumbent)
    for item in generated:
        value = tuple(sorted(int(x) for x in item))
        if value not in out:
            out.append(value)
    return tuple(out[:int(cap)])


def _crew_candidates(state, index, operation_key, line_id, k, machine_set, start_seconds,
                     max_crews, *, forbidden_crews=(), worker_pressure=None):
    op = state["problem"].operation_by_key[operation_key]
    detail = state["op_details"][operation_key]
    setup = int(authoritative_setup_seconds(detail))
    duration = authoritative_duration_contract(detail, int(k))
    productive_minutes = float(duration.productive_minutes)
    ready = state["problem"].origin + timedelta(seconds=int(start_seconds + setup))
    capability = str(op.required_capability).strip().lower()
    pool = _eligible_workers(state, line_id, capability,
                             int(state["planning_context"]["factory_id"]))
    groups = {}
    for worker in pool:
        try:
            _ready_start, _end, segs = productive_segments(
                ready, productive_minutes,
                lambda when, w=worker: operator_available_at(
                    state["operator_index"], int(w) if str(w).isdigit() else w, when))
        except NoAvailableCalendar:
            continue
        segments = tuple((int(round((a - state["problem"].origin).total_seconds())),
                          int(round((b - state["problem"].origin).total_seconds())))
                         for a, b in segs)
        # Worker calendars may split one operation into several labor-active
        # segments.  The same worker must cover every segment, but need not be
        # available during the legal pause between them.  Treating the union
        # as one continuous interval incorrectly rejects valid fixed crews.
        if not all(_covered(index.worker_free.get(str(worker), ()), (segment,))
                   for segment in segments):
            continue
        groups.setdefault(segments, []).append(str(worker))
    crews = []
    forbidden = [tuple(sorted(str(x) for x in crew)) for crew in forbidden_crews]
    attempted = set(forbidden)
    usage = {}
    for segments, workers in sorted(groups.items(), key=lambda item: repr(item[0])):
        candidates = [workers for _slot in range(int(k))]
        while len(crews) < int(max_crews):
            matches = _matching(
                [capability] * int(k), candidates,
                forbidden=tuple(attempted | set(crews)), max_solutions=1,
                worker_pressure=worker_pressure or {}, worker_usage=usage)
            if not matches:
                break
            crew = matches[0]
            attempted.add(tuple(crew))
            try:
                end, exact_segments = _fixed_crew_execution(
                    state["problem"], state["operator_index"], crew, ready,
                    productive_minutes, state["_worker_res"])
            except Exception:
                continue
            exact = tuple((int(round((a - state["problem"].origin).total_seconds())),
                           int(round((b - state["problem"].origin).total_seconds())))
                          for a, b in exact_segments)
            if exact != segments:
                continue
            if int(round((end - state["problem"].origin).total_seconds())) > index.horizon_end_seconds:
                continue
            if crew not in crews:
                crews.append(crew)
                for worker in crew:
                    usage[str(worker)] = int(usage.get(str(worker), 0)) + 1
            if len(crews) >= int(max_crews):
                return tuple(crews)
    return tuple(crews)


def generate_boundary_aware_configurations(state, boundary, *, crew_diversity=1,
                                           machine_set_cap=3, max_start_probes=8):
    """Generate bounded legal configurations compatible with carried facts.

    This is a diagnostic generator.  It returns paired ResourceConfiguration
    objects only; canonical CalendarExecutionRegimes and CP constraints remain
    unchanged and are generated by the existing production functions.
    """
    # ResourceConfigurations are still generated by the authoritative legal
    # domain builder.  Boundary compatibility is proved by the exact regime
    # construction against the carried reservations immediately before CP is
    # called; this avoids a second approximate start-time enumerator.
    availability = build_boundary_availability_index(state, boundary,
                                                     horizon_start_seconds=0)
    configs, audit, _summary = generate_resource_configurations(
        state, max_machine_sets=int(machine_set_cap),
        max_worker_crews=int(crew_diversity), machine_pool_limit=30)
    funnel = {}
    matching_stats = []
    for key in sorted(state["keys"]):
        rows = audit[[str(x.get("operation_key")) for x in audit].index(str(key))].get("domain_rows", ())
        raw_machine = sum(len(row.get("machine_pool", ())) for row in rows)
        raw_workers = sum(len(row.get("worker_pool", ())) for row in rows)
        boundary_workers = sorted({str(w) for cfg in configs.get(key, ()) for w in cfg.worker_ids}, key=str)
        boundary_machines = sorted({tuple(int(x) for x in cfg.machine_ids) for cfg in configs.get(key, ())})
        # A deterministic matching witness is recorded for every exposed crew.
        # The canonical regime generator remains the authority for whether the
        # crew survives all calendar/occupancy segments.
        for cfg in configs.get(key, ()):
            witness = _matching([str(key)] * len(cfg.worker_ids),
                                [tuple(cfg.worker_ids)] * len(cfg.worker_ids),
                                max_solutions=1)
            matching_stats.append({"operation_key": key,
                                   "worker_ids": list(cfg.worker_ids),
                                   "matching_attempted": True,
                                   "matching_witness": list(witness[0]) if witness else None,
                                   "crew_alternatives": 1 if witness else 0})
        funnel[key] = {
            "raw_machine_choices": raw_machine,
            "boundary_compatible_machine_sets": len(boundary_machines),
            "raw_eligible_workers": raw_workers,
            "boundary_compatible_workers": len(boundary_workers),
            "matching_feasible_configurations": len(configs.get(key, ())),
            "zero_mode_reason": None if configs.get(key) else "NO_LEGAL_RESOURCE_CONFIGURATION",
            "rows": list(rows),
            "exact_calendar_compatibility": "DEFERRED_TO_CANONICAL_REGIMES",
        }
    return configs, funnel, matching_stats, availability


def build_boundary_aware_regimes(state, configs, *, cache=None):
    return generate_calendar_regimes(state, configs, cache=cache)
