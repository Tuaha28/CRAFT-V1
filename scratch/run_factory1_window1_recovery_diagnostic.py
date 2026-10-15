"""Diagnostic-only frozen Factory-1 Window-1 Production V1 probe.

This intentionally stops after one prepared window and never enters the
rolling runner/commit loop.  It uses the frozen workload adapter and the
reusable production controller so the recovery lifecycle can be measured
without starting the full Factory-1 experiment.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scratch.run_factory1_craft_frozen_v1 import _build_loaded, WORKLOAD, OUT
from crafter.production.controller_v1 import ProductionControllerV1, ProductionControllerConfigV1
from crafter.repair.calendar_regimes import CalendarRegimeCache
from crafter.repair.planning import (PlanningContextV1, build_planning_state,
                                     prepare_planning_schedule,
                                     select_precedence_safe_window)
from crafter.repair.scoped_validation import validate_canonical_planning_candidate


REPORT = ROOT / "outputs/crafter/factory1_craft_run_v1/window1_recovery_diagnostic.json"


def main():
    process = psutil.Process()
    peak = {"rss": 0}
    stop = threading.Event()

    def sample():
        while not stop.wait(0.10):
            try:
                peak["rss"] = max(peak["rss"], int(process.memory_info().rss))
            except psutil.Error:
                return

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.perf_counter()
    try:
        loaded, boundary, _keys, identity, _contract = _build_loaded()
        problem = loaded[0]
        base_context = PlanningContextV1(
            "window-001", "INITIAL_PLAN", 1, 0, 0, 86400, 20000, (),
            "FROZEN_BENCHMARK_RAW", None, 0)
        base_state = build_planning_state(loaded, base_context, boundary=boundary)
        selected, _provenance = select_precedence_safe_window(
            problem, base_state["after_index"], base_state["classified"],
            base_context, committed_operation_keys=boundary.committed_operation_keys,
            boundary=boundary, maximum_operations=22)
        context = PlanningContextV1(
            "window-001", "INITIAL_PLAN", 1, 0, 0, 86400, 20000, selected,
            "FROZEN_BENCHMARK_RAW", None, 0)
        state = build_planning_state(loaded, context, boundary=boundary)
        cache = CalendarRegimeCache()
        prepared = prepare_planning_schedule(
            state, max_machine_sets=1, max_worker_crews=3, cache=cache)
        controller = ProductionControllerV1(ProductionControllerConfigV1())
        result = controller.run_window(
            prepared, boundary=boundary, cache=cache,
            boundary_facts=(), required_operation_keys=selected)
        trace = result.get("trace") or {}
        rounds = trace.get("rounds") or []
        candidate = result.get("candidate") or {}
        candidate_summary = controller._summary(candidate)
        scoped_validation = (
            validate_canonical_planning_candidate(
                candidate, state, required_operation_keys=selected,
                boundary_facts=())
            if candidate_summary.get("candidate_exists") else {
                "scope_status": "NOT_REACHED",
                "replay_status": "NOT_REACHED",
                "physical_status": "NOT_REACHED",
                "accepted": False,
            })
        payload = {
            "status": "WINDOW1_PROBE_COMPLETE",
            "database": identity.get("database"),
            "reference_time": identity.get("reference_time"),
            "local_window_operations": len(selected),
            "selected_operations": list(selected),
            "initial_resource_configurations": sum(
                len(v) for v in prepared.get("resource_configurations", {}).values()),
            "initial_calendar_regimes": int(prepared.get("regime_count", 0)),
            "initial_cp_status": ((trace.get("normal") or {}).get("attempts") or [{}])[0].get(
                "result", {}).get("solver_status"),
            "recovery": result.get("recovery"),
            "controller_status": trace.get("controller_status"),
            "controller_terminal": trace.get("controller_terminal") or {},
            "balanced_rescue": trace.get("balanced_rescue"),
            "recovery_rounds": len(rounds),
            "rounds": rounds,
            "final_summary": candidate_summary,
            "scope_validation": scoped_validation,
            "peak_rss_mb": round(peak["rss"] / (1024 * 1024), 3),
            "elapsed_seconds": time.perf_counter() - started,
            "database_writes": 0,
        }
    except Exception as exc:
        payload = {
            "status": "WINDOW1_PROBE_ERROR",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "peak_rss_mb": round(peak["rss"] / (1024 * 1024), 3),
            "elapsed_seconds": time.perf_counter() - started,
            "database_writes": 0,
        }
        raise
    finally:
        stop.set()
        sampler.join(timeout=1.0)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
                      encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
