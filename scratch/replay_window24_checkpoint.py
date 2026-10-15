"""Replay one exact first-UNKNOWN CP model at controlled solver budgets.

The checkpoint is diagnostic-only.  The prepared state and canonical regime
domain are rebuilt into the saved CpModelProto and its hash is verified before
any solve is attempted.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CHECKPOINT_DIR = Path(os.environ.get(
    "CRAFT_DIAGNOSTIC_CHECKPOINT_DIR",
    os.environ.get("CRAFT_WINDOW24_CHECKPOINT_DIR",
                   str(ROOT / "outputs" / "crafter" / "first_unknown_cp_checkpoint")),
))
OUT = CHECKPOINT_DIR / "replay"

from ortools.sat.python import cp_model

from crafter.repair.cp_repair import build_repair_model
from crafter.repair.solve import _accept, _materialize, _serialize_cp_model_proto


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_checkpoint() -> tuple[dict, dict]:
    manifest = json.loads((CHECKPOINT_DIR / "checkpoint_manifest.json").read_text(encoding="utf-8"))
    candidates = [row for row in manifest.get("checkpoints", ()) if not row.get("pickle_error")]
    if not candidates:
        raise RuntimeError("no complete prepared-state checkpoint was persisted")
    meta = candidates[-1]
    prepared = pickle.loads((CHECKPOINT_DIR / meta["prepared_pickle"]).read_bytes())
    proto = (CHECKPOINT_DIR / meta["model_proto"]).read_bytes()
    if _sha(proto) != meta["model_sha256"]:
        raise RuntimeError("checkpoint CpModelProto hash mismatch")
    return meta, {"prepared": prepared, "proto": proto}


def _build_exact_artifact(prepared: dict, expected_proto: bytes):
    artifact = build_repair_model(prepared["state"], prepared["regimes"])
    # The checkpointed model includes the exact solve-time hints added by the
    # rolling controller.  Rebuilding the structural model alone therefore
    # differs only in its solution_hint field.  Load the saved CpModelProto
    # into the rebuilt artifact so its variable/index maps remain available for
    # materialization while the solver receives the byte-identical model.
    artifact["model"].Proto().parse_text_format(expected_proto.decode("utf-8"))
    loaded = _serialize_cp_model_proto(artifact["model"].Proto())
    if loaded != expected_proto:
        raise RuntimeError(
            "reconstructed model differs from checkpoint: "
            f"saved={_sha(expected_proto)} rebuilt={_sha(loaded)}"
        )
    return artifact


def _solve(meta: dict, payload: dict, budget: float) -> dict:
    prepared = payload["prepared"]
    artifact = _build_exact_artifact(prepared, payload["proto"])
    solver = cp_model.CpSolver()
    params = meta.get("solver_parameters", {})
    solver.parameters.num_search_workers = int(params.get("num_search_workers", 8))
    solver.parameters.random_seed = int(params.get("random_seed", 42))
    solver.parameters.cp_model_presolve = bool(params.get("cp_model_presolve", True))
    solver.parameters.use_optimization_hints = bool(params.get("use_optimization_hints", True))
    solver.parameters.max_time_in_seconds = float(budget)
    solver.parameters.log_search_progress = True
    OUT.mkdir(parents=True, exist_ok=True)
    log_path = OUT / f"cp_search_{int(budget)}s.log"
    started = time.perf_counter()
    with log_path.open("w", encoding="utf-8") as log, contextlib.redirect_stderr(log):
        code = solver.Solve(artifact["model"])
    solve_seconds = time.perf_counter() - started
    status = solver.StatusName(code)
    result = {
        "time_limit_seconds": float(budget),
        "cp_status": status,
        "candidate_exists": status in {"FEASIBLE", "OPTIMAL"},
        "solve_seconds": solve_seconds,
        "solver_wall_time_seconds": solver.WallTime(),
        "objective": solver.ObjectiveValue(),
        "best_bound": solver.BestObjectiveBound(),
        "branches": solver.NumBranches(),
        "conflicts": solver.NumConflicts(),
        "deterministic_time": getattr(solver.ResponseProto(), "deterministic_time", None),
        "log_file": log_path.name,
        "model_sha256": _sha(payload["proto"]),
        "resource_configurations": meta.get("resource_configuration_count"),
        "calendar_regimes": meta.get("regime_count"),
        "database_writes": 0,
    }
    if status not in {"FEASIBLE", "OPTIMAL"}:
        result.update({"replay": "NOT_REACHED", "physical": "NOT_REACHED", "scope": "NOT_REACHED"})
        return result
    modes, selected, material = _materialize(prepared["state"], artifact, solver)
    replay, validator, event_violations, isolation = _accept(
        prepared["state"], modes, selected, material)
    scoped = validator.get("scope_acceptance") or {}
    mismatch_count = sum(bool(row.get("mismatches")) for row in material)
    replay_status = replay.get("status")
    physical_status = validator.get("status")
    scope_status = scoped.get("scope_status", "SCOPE_VALID" if physical_status == "VALID" else "NOT_REACHED")
    result.update({
        "materialization_mismatch_count": mismatch_count,
        "replay": replay_status,
        "replay_mismatch_count": replay.get("mismatch_count", 0),
        "physical": physical_status,
        "scope": scope_status,
        "event_violations": len(event_violations),
        "factory_isolation_violations": len(isolation),
        "selected_operation_count": len(selected),
    })
    result["accepted"] = (
        mismatch_count == 0
        and replay_status == "PASS"
        and physical_status == "VALID"
        and scope_status == "SCOPE_VALID"
    )
    return result


def main() -> int:
    meta, payload = _load_checkpoint()
    results = []
    for budget in (60.0, 120.0, 240.0):
        result = _solve(meta, payload, budget)
        results.append(result)
        (OUT / f"result_{int(budget)}s.json").write_text(
            json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        if result.get("accepted"):
            break
    summary = {
        "checkpoint": meta,
        "results": results,
        "minimum_solving_budget": next((x["time_limit_seconds"] for x in results if x.get("accepted")), None),
        "database_writes": 0,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "minimum_solving_budget": summary["minimum_solving_budget"],
        "results": results,
        "database_writes": 0,
    }, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
