"""Experimental exact calendar-behavior equivalence engine.

V2 does not merge physical ResourceConfigurations.  It shares only their
calendar function construction when the complete worker-calendar,
worker-reservation, and machine-reservation behavior is identical.  The
result remains the canonical V1 CalendarExecutionRegime schema, so the CP,
materialization, replay, and validator paths are unchanged.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import time

from .calendar_regimes import CalendarRegimeCache, generate_calendar_regimes


def _index_value(mapping, worker_id):
    raw = int(worker_id) if str(worker_id).isdigit() else worker_id
    if raw in mapping:
        return mapping[raw]
    return mapping.get(str(worker_id))


def _worker_behavior(state, worker_id):
    index = state["operator_index"]
    crew = _index_value(index.get("crew_of", {}), worker_id)
    if crew is not None:
        calendar = ("crew", tuple(crew))
    else:
        shift = _index_value(index.get("shift_of", {}), worker_id)
        calendar = (("shift", tuple(shift)) if shift is not None
                    else ("always",))
    reservations = tuple(
        (left.isoformat(), right.isoformat())
        for left, right in state.get("_worker_res", {}).get(str(worker_id), ()))
    return calendar, reservations


def _machine_behavior(state, machine_id):
    return tuple(tuple(row) for row in state.get("_machine_res", {}).get(int(machine_id), ()))


def configuration_calendar_signature(state, config):
    """Every authoritative input that can change temporal execution."""
    worker_behaviors = tuple(sorted(
        (_worker_behavior(state, worker) for worker in config.worker_ids),
        key=repr))
    machine_behaviors = tuple(sorted(
        (_machine_behavior(state, machine) for machine in config.machine_ids),
        key=repr))
    return (
        int(config.setup_seconds),
        int(config.productive_seconds),
        float(config.productive_minutes),
        len(config.worker_ids),
        len(config.machine_ids),
        worker_behaviors,
        machine_behaviors,
    )


def generate_calendar_regimes_v2(
        state, configurations, *, cache=None,
        additional_breakpoints_by_operation=None,
        operation_keys=None):
    """Generate V1-compatible regimes via exact behavior representatives."""
    started = time.perf_counter()
    cache = cache or CalendarRegimeCache()
    keys = tuple(sorted(str(key) for key in (
        state["keys"] if operation_keys is None else operation_keys)))
    representative_configs = {}
    groups_by_key = {}
    configuration_count = 0
    class_count = 0
    for key in keys:
        grouped = defaultdict(list)
        for config in configurations.get(key, ()):
            grouped[configuration_calendar_signature(state, config)].append(config)
            configuration_count += 1
        groups = []
        representatives = []
        for signature in sorted(grouped, key=repr):
            values = tuple(sorted(grouped[signature], key=repr))
            representative = values[0]
            groups.append((signature, representative, values))
            representatives.append(representative)
        groups_by_key[key] = tuple(groups)
        representative_configs[key] = tuple(representatives)
        class_count += len(groups)

    base_regimes, base_summary, _base_total, base_equivalence = generate_calendar_regimes(
        state, representative_configs, cache=cache,
        additional_breakpoints_by_operation=additional_breakpoints_by_operation,
        operation_keys=keys)

    regimes = {}
    summary = []
    total = 0
    base_summary_by_key = {str(row["operation_key"]): row for row in base_summary}
    for key in keys:
        by_representative = defaultdict(list)
        for regime in base_regimes.get(key, ()):
            by_representative[regime["config"]].append(regime)
        rows = []
        raw_behavior_regimes = 0
        for _signature, representative, configs in groups_by_key[key]:
            template_rows = tuple(by_representative.get(representative, ()))
            raw_behavior_regimes += len(template_rows)
            for config in configs:
                for regime in template_rows:
                    rows.append({**regime, "config": config, "base": config})
        rows.sort(key=lambda row: (
            repr(row["config"]), int(row["lo"]), int(row["hi"]),
            repr(row["formula"])))
        regimes[key] = tuple(rows)
        total += len(rows)
        base = base_summary_by_key.get(key, {})
        summary.append({
            **base,
            "operation_key": key,
            "resource_configurations": len(configurations.get(key, ())),
            "calendar_behavior_classes": len(groups_by_key[key]),
            "behavior_class_regimes": raw_behavior_regimes,
            "regime_count": len(rows),
            "status": "OK" if rows else "ZERO_REGIME_DOMAIN",
        })

    elapsed = time.perf_counter() - started
    profile = {
        "engine": "CALENDAR_ENGINE_V2_EXACT_BEHAVIOR_CLASSES",
        "resource_configurations": configuration_count,
        "calendar_behavior_classes": class_count,
        "representative_reduction": configuration_count - class_count,
        "representative_reduction_rate": (
            0.0 if configuration_count == 0
            else (configuration_count - class_count) / configuration_count),
        "materialized_regimes": total,
        "generation_seconds": elapsed,
        "cache_profile": cache.snapshot(),
    }
    equivalence = {
        **base_equivalence,
        "calendar_engine_v2": profile,
        "all_equivalent": bool(base_equivalence.get("all_equivalent", True)),
    }
    return regimes, summary, total, equivalence

