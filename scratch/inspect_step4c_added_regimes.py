"""Explain why matching-generated target crews do or do not create early regimes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from crafter.disruptions.benchmark import _load_state
from crafter.repair.boundary_aware_modes import build_boundary_availability_index
from crafter.repair.calendar_regimes import (
    CalendarRegimeCache,
    _breakpoints,
    _execution,
    generate_calendar_regimes,
)
from crafter.repair.planning import build_planning_state
from crafter.repair.resource_configs import generate_resource_configurations
from crafter.repair.route_aware_crew_expansion import (
    build_worker_contention,
    generate_additional_crew_configurations,
)
from scratch.run_window14_step4b import W14_KEYS, context, make_boundary, quiet, rows_and_provenance


def main():
    loaded = quiet(_load_state)
    _accepted, committed, _ = rows_and_provenance()
    boundary, _facts = make_boundary(loaded[5], committed)
    state = quiet(build_planning_state, loaded, context(W14_KEYS, "step4c-regime-inspection"), boundary=boundary)
    availability = build_boundary_availability_index(state, boundary, horizon_start_seconds=0)
    configs, audit, _summary = quiet(
        generate_resource_configurations, state,
        max_machine_sets=3, max_worker_crews=16, machine_pool_limit=30)
    pressure = build_worker_contention(audit, state["keys"])
    additions, trace = generate_additional_crew_configurations(
        state, availability, configs, "251_2065", 528572, 8,
        worker_pressure=pressure)
    only = {key: () for key in state["keys"]}
    only["251_2065"] = additions
    cache = CalendarRegimeCache()
    regimes, summary, total, equivalence = quiet(
        generate_calendar_regimes, state, only, cache=cache)
    rows = []
    for config in additions:
        config_regimes = [row for row in regimes["251_2065"] if row["config"] == config]
        direct = {}
        try:
            end, segments = _execution(state, config, 528572, cache=None)
            direct = {"status": "PASS", "end_seconds": end, "segments": segments}
        except Exception as exc:
            direct = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
        breakpoints = _breakpoints(state, "251_2065", config, 0, cache=None)
        surrounding = [value for value in breakpoints if 500000 <= int(value) <= 560000]
        rows.append({
            "worker_ids": list(config.worker_ids),
            "direct_execution_at_528572": direct,
            "regime_count": len(config_regimes),
            "minimum_lo": min((int(row["lo"]) for row in config_regimes), default=None),
            "maximum_hi": max((int(row["hi"]) for row in config_regimes), default=None),
            "regime_covering_528572": [
                {"lo": int(row["lo"]), "hi": int(row["hi"]), "formula": row["formula"]}
                for row in config_regimes if int(row["lo"]) <= 528572 <= int(row["hi"])
            ],
            "nearby_breakpoints": surrounding,
        })
    Path("outputs/crafter/factory2_172_v1/window14_step4c/added_regime_diagnosis.json").write_text(
        json.dumps({"generation_trace": trace, "rows": rows, "total_regimes": total,
                    "summary": summary, "equivalence": equivalence, "database_writes": 0},
                   indent=2, default=str) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
