"""Controlled exact diagnostics for the persisted Factory-2 Window-14 state.

This script never commits or mutates production state.  It reconstructs the
Window-14 hard boundary from accepted V3 rows, then compares exact canonical
models under finite resource and temporal exposures.  Diagnostic candidates
are accepted only if the normal scoped replay/validator path passes.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from crafter.disruptions.benchmark import _load_state
from crafter.repair.global_planner import WindowBoundaryState
from crafter.repair.planning import PlanningContextV1, build_planning_state, prepare_planning_schedule
from crafter.repair.resource_domain_v2 import _solve_probe, summarize_resource_domain
from crafter.repair.solve import run_canonical_schedule
from crafter.repair.scoped_validation import validate_planning_window
from crafter.repair.validation_scope import ValidationScopeV1
from crafter.repair.window_phenotype import build_window_phenotype
from crafter.repair.calendar_regimes import CalendarRegimeCache
from crafter.repair.temporal_escalation import TemporalEscalationPolicyV3

ROOT = Path("outputs/crafter/resource_domain_v3")
RUN = ROOT / "factory2_v3_run"
OUT = ROOT / "quality_validation_v1"
OUT.mkdir(parents=True, exist_ok=True)


def read(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def dump(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def _accepted_rows():
    rows = {}
    windows = []
    for path in sorted(RUN.glob("window_*.json")):
        item = read(path, {}) or {}
        if item.get("accepted") is True:
            windows.append(item)
            by_key = {str(row["operation_key"]): row for row in item.get("selected_assignments", ())}
            for key in item.get("committed_keys", ()):
                if str(key) not in by_key:
                    raise ValueError(f"committed key {key} missing from {path.name}")
                if str(key) in rows:
                    raise ValueError(f"duplicate persisted commitment {key}")
                rows[str(key)] = by_key[str(key)]
    return windows, rows


def _make_boundary(route_index, committed_rows, factory_id):
    machine = []
    worker = []
    routing = {}
    route_facts = []
    committed_keys = tuple(sorted(committed_rows))
    for key in committed_keys:
        row = committed_rows[key]
        cfg = row["resource_configuration"]
        order_id = str(row.get("order_id", key.split("_", 1)[0]))
        base = {
            "operation_key": key,
            "production_order_id": order_id,
            "factory_id": int(cfg["factory_id"]),
            "line_id": int(cfg["line_id"]),
            "k": int(cfg["k"]),
            "start_seconds": int(row["start_seconds"]),
            "end_seconds": int(row["end_seconds"]),
            "setup_seconds": int(row.get("setup_seconds", cfg.get("setup_seconds", 0))),
            "productive_seconds": int(row.get("productive_seconds", cfg.get("productive_seconds", 0))),
            "machine_ids": list(cfg["machine_ids"]),
            "worker_ids": list(cfg["worker_ids"]),
        }
        for machine_id in cfg["machine_ids"]:
            machine.append({**base, "machine_id": int(machine_id)})
        for worker_id in cfg["worker_ids"]:
            for left, right in row.get("worker_segments", ()):
                worker.append({
                    "operation_key": key,
                    "production_order_id": order_id,
                    "factory_id": int(cfg["factory_id"]),
                    "worker_id": str(worker_id),
                    "start_seconds": int(left),
                    "end_seconds": int(right),
                })
        successors = route_index.route_successor.get(key, ())
        if isinstance(successors, str):
            successors = (successors,)
        routing[key] = {
            **base,
            "authoritative_end_seconds": int(row["end_seconds"]),
            "successor_operation_key": str(successors[0]) if len(successors) == 1 else None,
        }
        for successor in successors:
            route_facts.append({
                "operation_key": key,
                "predecessor_operation_key": key,
                "successor_operation_key": str(successor),
                "production_order_id": order_id,
                "authoritative_end_seconds": int(row["end_seconds"]),
                "factory_id": int(cfg["factory_id"]),
            })
    boundary = WindowBoundaryState(
        int(factory_id), committed_keys, (), tuple(machine), tuple(worker), {}, routing,
        "factory-2-v3-window14-diagnostic")
    facts = [dict(row) for row in machine] + [dict(row) for row in worker] + route_facts
    return boundary, tuple(facts)


def _rows_for_validation(result, state):
    rows = []
    for row in (result.get("acceptance", {}) or {}).get("selected_schedule", ()) or ():
        item = dict(row)
        op = state["problem"].operation_by_key[str(item["operation_key"])]
        item["order_id"] = str(op.order_id)
        cfg = item["resource_configuration"]
        item["setup_seconds"] = int(cfg.get("setup_seconds", item.get("setup_seconds", 0)))
        item["productive_seconds"] = int(cfg.get("productive_seconds", item.get("productive_seconds", 0)))
        item["productive_minutes"] = float(cfg.get("productive_minutes", item["productive_seconds"] / 60.0))
        rows.append(item)
    return rows


def _scoped_acceptance(result, state, boundary_facts, required_keys):
    rows = _rows_for_validation(result, state)
    future = ()
    scope = ValidationScopeV1("WINDOW", tuple(sorted(required_keys)), future)
    phenotype, _ = build_window_phenotype(
        rows, boundary_facts=boundary_facts,
        future_outside_operation_keys=future,
        validation_scope=scope)
    return validate_planning_window(state, phenotype)


def _stage_summary(result):
    acceptance = result.get("acceptance", {}) or {}
    stages = list(acceptance.get("stages", ()) or ())
    return [{
        "stage": row.get("stage"), "status": row.get("status"),
        "config_limit": row.get("config_limit"), "window_days": row.get("window_days"),
        "resource_configurations": row.get("resource_configurations"),
        "regimes": row.get("regimes"), "variables": row.get("variables"),
        "constraints": row.get("constraints"), "optional_intervals": row.get("optional_intervals"),
        "branches": row.get("branches"), "conflicts": row.get("conflicts"),
        "model_build_seconds": row.get("model_build_seconds"),
        "solve_seconds": row.get("solve_seconds"),
        "time_scope": row.get("time_scope"),
    } for row in stages]


def _run_case(label, state, boundary_facts, required_keys, machine_cap, worker_cap,
              *, unrestricted=False, plan=None, run_probe=False):
    cache = CalendarRegimeCache()
    started = time.perf_counter()
    prepared = prepare_planning_schedule(
        state, max_machine_sets=int(machine_cap), max_worker_crews=int(worker_cap),
        machine_pool_limit=30, cache=cache)
    prep_seconds = time.perf_counter() - started
    probe = _solve_probe(state, prepared, seconds=15.0) if run_probe else None
    solve_started = time.perf_counter()
    result = run_canonical_schedule(
        prepared, objective_mode=False, plan=tuple(plan or ((1, None, 30), (2, 7, 180), (3, 7, 240), (999, 14, 300))),
        unrestricted_final_stage=bool(unrestricted))
    solve_seconds = time.perf_counter() - solve_started
    acceptance = result.get("acceptance", {}) or {}
    candidate = bool(acceptance.get("selected_schedule"))
    planning = _scoped_acceptance(result, state, boundary_facts, required_keys) if candidate else None
    domain = summarize_resource_domain(result.get("resource_audit", ()))
    return {
        "label": label,
        "machine_cap": int(machine_cap), "worker_cap": int(worker_cap),
        "unrestricted_final_stage": bool(unrestricted),
        "required_operation_count": len(required_keys),
        "resource_configuration_count": sum(len(v) for v in prepared.get("resource_configurations", {}).values()),
        "calendar_regime_count": int(prepared.get("regime_count", 0)),
        "zero_regime_operation_keys": sorted(str(key) for key, rows in prepared.get("regimes", {}).items() if not rows),
        "resource_domain_summary": domain,
        "candidate_exists": candidate,
        "canonical_status": acceptance.get("status"),
        "cp_status": acceptance.get("winning_stage", {}).get("status") if acceptance.get("winning_stage") else None,
        "stages": _stage_summary(result),
        "scoped_acceptance": planning,
        "probe": probe,
        "prepare_seconds": prep_seconds,
        "canonical_seconds": solve_seconds,
        "cache_profile": cache.snapshot(),
    }


def _boundary_variant(boundary, facts, *, keep_machine=True, keep_worker=True,
                      keep_routing=True):
    """Return a diagnostic-only boundary projection.

    The projection removes one carried physical dimension while preserving the
    other exact facts.  It is never an accepted production boundary and is
    used only to identify the Window-14 blocker.
    """
    from dataclasses import replace
    machine = tuple(boundary.machine_commitments) if keep_machine else ()
    worker = tuple(boundary.worker_commitments) if keep_worker else ()
    routing = dict(boundary.routing_completion_state) if keep_routing else {}
    kept_facts = []
    for fact in facts:
        if not keep_machine and fact.get("machine_id") is not None:
            continue
        if not keep_worker and fact.get("worker_id") is not None:
            continue
        if not keep_routing and fact.get("predecessor_operation_key") is not None:
            continue
        kept_facts.append(fact)
    return replace(boundary, machine_commitments=machine,
                   worker_commitments=worker, routing_completion_state=routing), tuple(kept_facts)


def main():
    started = time.perf_counter()
    failure = read(RUN / "window_014.json", {}) or {}
    selected = tuple(failure.get("window_acceptance", {}).get("scope", {}).get("missing_required_operations", ()))
    if len(selected) != 22:
        raise ValueError(f"unexpected Window-14 required set: {len(selected)}")
    with contextlib.redirect_stdout(io.StringIO()):
        loaded = _load_state()
    problem, _operator_index, _rows, _durations, _details, route_index, _baseline, _checked, _source = loaded
    windows, committed_rows = _accepted_rows()
    boundary, boundary_facts = _make_boundary(route_index, committed_rows, 2)
    predecessor_closure = {}
    outside_predecessors = {}
    selected_set = set(selected)
    for key in selected:
        pred = route_index.route_predecessor.get(key)
        preds = (pred,) if isinstance(pred, str) else tuple(pred or ())
        predecessor_closure[key] = sorted(str(x) for x in preds)
        outside_predecessors[key] = sorted(str(x) for x in preds if str(x) not in selected_set and str(x) not in committed_rows)
    context = PlanningContextV1(
        "window-014", "ROLLING_PLAN", 2, 326400, 326400, 412800, 346400,
        selected, "FROZEN_BENCHMARK_RAW", snapshot_as_of_seconds=0,
        previous_provisional_plan_reference="provisional-013")
    state = build_planning_state(loaded, context, boundary=boundary)
    diagnostics = []
    if os.environ.get("CRAFT_DIAG_ONLY_NOB") != "1":
        diagnostics.append(_run_case("CURRENT_1X1_BOUNDED_WITH_PRESSURE_PROBE", state, boundary_facts, selected, 1, 1, run_probe=True))
        diagnostics.append(_run_case("WORKER_1X3_BOUNDED", state, boundary_facts, selected, 1, 3))
        diagnostics.append(_run_case("WORKER_1X3_UNRESTRICTED_TEMPORAL", state, boundary_facts, selected, 1, 3,
                                     unrestricted=True, plan=TemporalEscalationPolicyV3().plans[1]))
        diagnostics.append(_run_case("WORKER_1X8_UNRESTRICTED_TEMPORAL", state, boundary_facts, selected, 1, 8,
                                     unrestricted=True, plan=TemporalEscalationPolicyV3().plans[1]))
        diagnostics.append(_run_case("MACHINE_WORKER_3X3_UNRESTRICTED_TEMPORAL", state, boundary_facts, selected, 3, 3,
                                     unrestricted=True, plan=TemporalEscalationPolicyV3().plans[1]))
    # Commitment/reservation ablation is diagnostic only: it removes the
    # reconstructed hard boundary, never an accepted production state.
    state_without_boundary = build_planning_state(loaded, context, boundary=None)
    diagnostics.append(_run_case("NO_COMMITTED_BOUNDARY_1X3_UNRESTRICTED_DIAGNOSTIC",
                                 state_without_boundary, (), selected, 1, 3,
                                 unrestricted=True, plan=TemporalEscalationPolicyV3().plans[1]))
    if os.environ.get("CRAFT_DIAG_BOUNDARY_ABLATIONS") == "1":
        boundary_runs = []
        variants = {
            "NO_MACHINE_BOUNDARY_1X3_UNRESTRICTED": (False, True, True),
            "NO_WORKER_BOUNDARY_1X3_UNRESTRICTED": (True, False, True),
            "NO_ROUTING_BOUNDARY_1X3_UNRESTRICTED": (True, True, False),
            "NO_MACHINE_WORKER_BOUNDARY_1X3_UNRESTRICTED": (False, False, True),
        }
        for label, (km, kw, kr) in variants.items():
            variant, variant_facts = _boundary_variant(boundary, boundary_facts,
                                                       keep_machine=km,
                                                       keep_worker=kw,
                                                       keep_routing=kr)
            variant_state = build_planning_state(loaded, context, boundary=variant)
            boundary_runs.append(_run_case(
                label, variant_state, variant_facts, selected, 1, 3,
                unrestricted=True, plan=TemporalEscalationPolicyV3().plans[1]))
        dump("window14_boundary_ablations.json", {
            "boundary_fact_counts": {
                "machine": len(boundary.machine_commitments),
                "worker": len(boundary.worker_commitments),
                "routing": len(boundary.routing_completion_state),
            },
            "runs": boundary_runs,
            "database_writes": 0,
        })
    # The selected set is already a complete required-predecessor closure if
    # this is empty; retain the explicit result instead of silently assuming it.
    dump("window14_diagnosis.json", {
        "classification": "WINDOW14_UNRESOLVED",
        "factory_id": 2, "window_id": "window-014", "snapshot_as_of_seconds": 0,
        "window_start_seconds": 326400, "window_end_seconds": 412800,
        "commit_end_seconds": 346400,
        "selected_required_keys": list(selected),
        "accepted_prior_window_count": len(windows),
        "committed_operation_count": len(committed_rows),
        "boundary_machine_fact_count": len(boundary.machine_commitments),
        "boundary_worker_fact_count": len(boundary.worker_commitments),
        "boundary_routing_fact_count": len(boundary_facts),
        "predecessors": predecessor_closure,
        "outside_predecessors": outside_predecessors,
        "worker_complete_enumeration": {
            "status": "NOT_ENUMERABLE_WITHIN_SAFE_DIAGNOSTIC_BUDGET",
            "reason": "operation worker-crew capacities are combinatorial and remain truncated",
        },
        "diagnostics": diagnostics,
        "elapsed_seconds": time.perf_counter() - started,
        "database_writes": 0,
    })
    dump("window14_ablations.json", {
        "sequence": [x["label"] for x in diagnostics],
        "runs": diagnostics,
        "accepted_diagnostic_candidates": [
            x["label"] for x in diagnostics
            if (x.get("scoped_acceptance") or {}).get("accepted") is True
        ],
        "database_writes": 0,
    })
    print(json.dumps({
        "selected": len(selected), "committed": len(committed_rows),
        "outside_predecessor_count": sum(bool(v) for v in outside_predecessors.values()),
        "diagnostics": [
            {"label": x["label"], "candidate": x["candidate_exists"],
             "cp_status": x["cp_status"], "configs": x["resource_configuration_count"],
             "regimes": x["calendar_regime_count"],
             "accepted": (x.get("scoped_acceptance") or {}).get("accepted")}
            for x in diagnostics
        ]
    }, indent=2))


if __name__ == "__main__":
    main()
