"""Read-only inspection of the frozen Window-14 route/resource state."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from crafter.disruptions.benchmark import _load_state
from crafter.repair.boundary_aware_modes import (
    build_boundary_availability_index,
    generate_boundary_aware_configurations,
)
from crafter.repair.planning import build_planning_state
from scratch.run_window14_step4b import W14_KEYS, context, make_boundary, quiet, rows_and_provenance


def main():
    loaded = quiet(_load_state)
    _accepted, committed, _provenance = rows_and_provenance()
    boundary, _facts = make_boundary(loaded[5], committed)
    state = quiet(build_planning_state, loaded, context(W14_KEYS, "step4c-inspection"), boundary=boundary)
    configs, funnel, _stats, index = quiet(
        generate_boundary_aware_configurations,
        state,
        boundary,
        crew_diversity=16,
        machine_set_cap=3,
        max_start_probes=4,
    )
    rows = {}
    for key in ("251_2064", "251_2065", "251_2066"):
        op = state["problem"].operation_by_key[key]
        signatures = {}
        for cfg in configs.get(key, ()):
            sig = (int(cfg.line_id), int(cfg.k), tuple(cfg.machine_ids))
            bucket = signatures.setdefault(sig, {"crews": set(), "workers": set()})
            bucket["crews"].add(tuple(cfg.worker_ids))
            bucket["workers"].update(str(x) for x in cfg.worker_ids)
        rows[key] = {
            "predecessor": state["after_index"].route_predecessor.get(key),
            "successor": state["after_index"].route_successor.get(key),
            "capability": str(op.required_capability),
            "config_count": len(configs.get(key, ())),
            "config_groups": [
                {
                    "line": sig[0],
                    "k": sig[1],
                    "machines": list(sig[2]),
                    "crew_count": len(values["crews"]),
                    "distinct_exposed_workers": len(values["workers"]),
                }
                for sig, values in sorted(signatures.items())
            ],
            "domain_rows": [
                {
                    "line_id": row["line_id"],
                    "k": row["k"],
                    "machine_pool_count": len(row.get("machine_pool", ())),
                    "worker_pool_count": len(row.get("worker_pool", ())),
                    "total_legal_worker_crews": row.get("total_legal_worker_crews"),
                    "more_worker_crews_available": row.get("more_worker_crews_available"),
                }
                for row in funnel[key]["rows"]
            ],
        }
    Path("outputs/crafter/factory2_172_v1/window14_step4c_inspection.json").write_text(
        json.dumps(rows, indent=2, default=str) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
