"""Derive a compact, non-authoritative closure report from a Window-1 probe."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(directory: str) -> int:
    root = Path(directory)
    window = json.loads((root / "window_001.json").read_text(encoding="utf-8"))
    identity = json.loads((root / "benchmark_identity.json").read_text(encoding="utf-8"))
    production = window["production_controller"]
    round_one = production["rounds"][1]
    report = dict(round_one.get("closure_report") or {})
    report["known_factory_universe_count"] = int(identity["initial_operation_count"])
    report["entire_factory_reached"] = (
        int(report.get("closure_final_count", 0)) >= report["known_factory_universe_count"]
    )
    report.update({
        "worker_truncation_proven": bool(production.get("diagnosis", {}).get("worker_truncation_evidence")),
        "route_pressure_seed_count": len(production.get("diagnosis", {}).get("worker_route_pressure", ())),
        "new_crews_generated": sum(
            int(row.get("new_crews_accepted", 0))
            for row in round_one.get("crew_generation", ())
        ),
        "resource_configurations": int(
            round_one.get("domain_superset", {}).get("expanded_configuration_count", 0)
        ),
        "calendar_regimes": int(production.get("recovery", {}).get("regimes", 0)),
        "cp_status": production.get("recovery", {}).get("solver_status") or "NO_VALID_CANDIDATE",
        "replay": production.get("recovery", {}).get("replay", "NOT_REACHED"),
        "physical_validator": production.get("recovery", {}).get("validator", "NOT_REACHED"),
        "database_writes": 0,
    })
    (root / "closure_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
