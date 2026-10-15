"""Diagnostic-only capture of Factory-1 Window-1 CP models and search logs."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "outputs" / "crafter" / "factory1_cp_parallelism_audit_v1"
CHECKPOINT = OUT / "checkpoint"
OUT.mkdir(parents=True, exist_ok=True)
os.environ["CRAFT_DIAGNOSTIC_CHECKPOINT_DIR"] = str(CHECKPOINT)

from crafter.repair import solve as canonical_solve


_original_solver = canonical_solve._solver
_logs: list[dict] = []


def _logged_solver():
    solver = _original_solver()
    index = len(_logs) + 1
    entry = {"solver_index": index, "lines": []}
    _logs.append(entry)
    solver.parameters.log_search_progress = True
    solver.parameters.log_to_stdout = False
    solver.log_callback = entry["lines"].append
    return solver


canonical_solve._solver = _logged_solver

from scratch.run_factory1_window1_recovery_diagnostic import main


if __name__ == "__main__":
    try:
        main()
    finally:
        (OUT / "current_solver_search_logs.json").write_text(
            json.dumps(_logs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
