"""Diagnostic direct-time CP prototype for the three-operation P2 chain.

The production JointExecutableMode contract is unchanged.  This prototype
keeps the selected physical configuration (line/k/machines/workers) but lets
CP-SAT choose a legal start time inside exact recurring worker-calendar
windows.  It is deliberately limited to 1029_293 -> 1029_294 -> 1029_295.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from ortools.sat.python import cp_model

from crafter.disruptions.p2_core_guided_prototype import _prepare_state
from crafter.disruptions.p2_feasibility_origin_audit import _solve_feasibility, _write
from crafter.lns_probe.anchored_model import outside_machine_reservations, outside_worker_reservations
from crafter.lns_probe.joint_mode_model import build_joint_mode_model
from crafter.lns_probe.run_e1_7_final_verified import _phenotype, _replay_mode
from crafter.phenotype import validate
from ga.worker_allocation import operator_available_at

OUT = Path("outputs/crafter/p2_direct_time_micro_repair_v1")
CHAIN = ("1029_293", "1029_294", "1029_295")


def _sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config_key(mode):
    return (int(mode.line_id), int(mode.machine_count), tuple(int(x) for x in mode.machine_ids),
            tuple(str(x) for x in mode.worker_ids), int(mode.setup_seconds), int(mode.productive_seconds))


def _configs(state, key):
    seen = {}
    for mode in state["modes"].get(key, ()):
        seen.setdefault(_config_key(mode), mode)
    return list(seen.values())


def _seconds(problem, when):
    return int(round((when - problem.origin).total_seconds()))


def _calendar_segments(problem, operator_index, workers, setup, productive, horizon):
    """Exact piecewise-constant shift/rest windows for a fixed crew."""
    origin = problem.origin
    first = origin.replace(hour=0, minute=0, second=0, microsecond=0)
    last_date = (origin + timedelta(seconds=int(horizon))).date()
    out = []
    day = first
    while day.date() <= last_date:
        points = [day, day + timedelta(hours=6), day + timedelta(hours=14),
                  day + timedelta(hours=22), day + timedelta(days=1)]
        for left, right in zip(points, points[1:]):
            if right <= origin:
                continue
            probe_left = max(left, origin) + timedelta(seconds=1)
            probe_right = right - timedelta(seconds=1)
            if probe_right < origin:
                continue
            if all(operator_available_at(operator_index, int(w) if str(w).isdigit() else w, probe_left)
                   and operator_available_at(operator_index, int(w) if str(w).isdigit() else w, probe_right)
                   for w in workers):
                lo = max(0, _seconds(problem, left) - int(setup))
                hi = min(int(horizon) - int(setup + productive),
                         _seconds(problem, right) - int(setup + productive))
                if lo <= hi:
                    out.append((lo, hi))
        day += timedelta(days=1)
    return out


def _subtract_forbidden(intervals, forbidden):
    current = list(intervals)
    for f0, f1 in forbidden:
        next_intervals = []
        for a, b in current:
            if f1 < a or f0 > b:
                next_intervals.append((a, b)); continue
            if a < f0:
                next_intervals.append((a, f0 - 1))
            if f1 < b:
                next_intervals.append((f1 + 1, b))
        current = [(a, b) for a, b in next_intervals if a <= b]
    return current


def _allowed_starts(state, mode, machine_res, worker_res, lower_bound=0):
    setup = int(mode.setup_seconds); productive = int(mode.productive_seconds)
    horizon = int(state["problem"].horizon_seconds)
    intervals = _calendar_segments(state["problem"], state["operator_index"], mode.worker_ids,
                                   setup, productive, horizon)
    intervals = [(max(a, int(lower_bound)), b) for a, b in intervals if b >= int(lower_bound)]
    forbidden = []
    for mid in mode.machine_ids:
        for left, right, _order, _other in machine_res.get(int(mid), ()):
            forbidden.append((int(left) - setup - productive + 1, int(right) - 1))
    for wid in mode.worker_ids:
        for left, right in worker_res.get(str(wid), ()):
            l = _seconds(state["problem"], left); r = _seconds(state["problem"], right)
            forbidden.append((l - setup - productive + 1, r - setup - 1))
    return _subtract_forbidden(intervals, forbidden)


def _build_direct_model(state, mutable=CHAIN, *, outside=True):
    model = cp_model.CpModel()
    mutable = tuple(mutable)
    machine_res = outside_machine_reservations(state["problem"], state["after_index"], set(mutable)) if outside else {}
    worker_res = outside_worker_reservations(state["problem"], state["after_index"], set(mutable)) if outside else {}
    starts, ends, literals, configs_by_key = {}, {}, {}, {}
    machine_intervals = {}
    worker_intervals = {}
    allowed = {}
    delayed_end = int(state["event"].metadata.get("delayed_end_seconds", 0))
    for key in mutable:
        configs = _configs(state, key)
        configs_by_key[key] = configs
        if not configs:
            raise ValueError(f"no physical configurations for {key}")
        lits = [model.NewBoolVar(f"direct_cfg_{key}_{i}") for i in range(len(configs))]
        literals[key] = lits
        starts[key] = model.NewIntVar(0, int(state["problem"].horizon_seconds), f"direct_start_{key}")
        duration_expr = sum(l * int(m.setup_seconds + m.productive_seconds) for l, m in zip(lits, configs))
        ends[key] = model.NewIntVar(0, int(state["problem"].horizon_seconds) + 100000, f"direct_end_{key}")
        model.Add(ends[key] == starts[key] + duration_expr)
        model.AddExactlyOne(lits)
        machine_intervals[key] = []
        worker_intervals[key] = []
        allowed[key] = []
        for i, (lit, mode) in enumerate(zip(lits, configs)):
            lower = delayed_end if key == CHAIN[0] else 0
            windows = _allowed_starts(state, mode, machine_res, worker_res, lower_bound=lower)
            allowed[key].append(windows)
            if windows:
                model.AddLinearExpressionInDomain(starts[key], cp_model.Domain.FromIntervals(windows)).OnlyEnforceIf(lit)
            else:
                model.Add(lit == 0)
            dur = int(mode.setup_seconds + mode.productive_seconds)
            machine_intervals[key].append((int(mode.line_id), lit,
                model.NewOptionalFixedSizeIntervalVar(starts[key], dur, lit, f"direct_m_{key}_{i}"), mode))
            worker_start = model.NewIntVar(0, int(state["problem"].horizon_seconds) + 100000, f"direct_prod_start_{key}_{i}")
            model.Add(worker_start == starts[key] + int(mode.setup_seconds))
            worker_intervals[key].append((lit,
                model.NewOptionalFixedSizeIntervalVar(worker_start, int(mode.productive_seconds), lit,
                                                      f"direct_w_{key}_{i}"), mode))
    # Exact route order and event lower bound.
    model.Add(starts[CHAIN[0]] >= delayed_end)
    for left, right in zip(CHAIN, CHAIN[1:]):
        model.Add(ends[left] <= starts[right])
    # Fixed external boundaries, if requested.
    fixed_m = []; fixed_w = []
    if outside:
        for mid, rows in machine_res.items():
            for i, (left, right, _order, _other) in enumerate(rows):
                if right > left:
                    fixed_m.append((int(mid), model.NewFixedSizeIntervalVar(int(left), int(right-left), f"outside_m_{mid}_{i}")))
        for wid, rows in worker_res.items():
            for i, (left, right) in enumerate(rows):
                s = _seconds(state["problem"], left); e = _seconds(state["problem"], right)
                if e > s:
                    fixed_w.append((str(wid), model.NewFixedSizeIntervalVar(s, e-s, f"outside_w_{wid}_{i}")))
    # NoOverlap with all physical identities.  Optional intervals are indexed
    # by their selected configuration; worker occupancy is productive only.
    by_machine = {}
    by_worker = {}
    for key in mutable:
        for _line, _lit, iv, mode in machine_intervals[key]:
            for mid in mode.machine_ids:
                by_machine.setdefault(int(mid), []).append(iv)
        for lit, iv, mode in worker_intervals[key]:
            for wid in mode.worker_ids:
                by_worker.setdefault(str(wid), []).append(iv)
    for mid, iv in fixed_m:
        by_machine.setdefault(int(mid), []).append(iv)
    for wid, iv in fixed_w:
        by_worker.setdefault(wid, []).append(iv)
    for vals in by_machine.values():
        model.AddNoOverlap(vals)
    for vals in by_worker.values():
        model.AddNoOverlap(vals)
    return {"model": model, "starts": starts, "ends": ends, "literals": literals,
            "configs": configs_by_key, "allowed": allowed,
            "machine_reservations": machine_res, "worker_reservations": worker_res,
            "size": {"operations": len(mutable), "configurations": sum(len(v) for v in configs_by_key.values()),
                      "variables": len(model.Proto().variables), "constraints": len(model.Proto().constraints)},
            "outside": outside}


def _solve_direct(art, state, seconds=30.0):
    try: art["model"].ClearObjective()
    except AttributeError: pass
    solver = cp_model.CpSolver(); solver.parameters.max_time_in_seconds = float(seconds)
    solver.parameters.num_search_workers = 1; solver.parameters.random_seed = 42
    status = solver.Solve(art["model"])
    result = {"status": solver.StatusName(status), "status_code": int(status),
              "wall_time_seconds": solver.WallTime(), "deterministic_time": solver.UserTime(),
              "branches": solver.NumBranches(), "conflicts": solver.NumConflicts(), "model": art["size"]}
    if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        selected = {}
        for key, lits in art["literals"].items():
            idx = next(i for i, lit in enumerate(lits) if solver.Value(lit))
            mode = art["configs"][key][idx]
            start = int(solver.Value(art["starts"][key])); end = int(solver.Value(art["ends"][key]))
            seg_start = state["problem"].origin + timedelta(seconds=start + int(mode.setup_seconds))
            seg_end = state["problem"].origin + timedelta(seconds=end)
            selected[key] = {"config_index": idx, "mode": mode, "start": start, "end": end,
                             "productive_start": start + int(mode.setup_seconds),
                             "worker_segments": ((seg_start, seg_end),)}
        result["selected"] = {k: {"config_index": v["config_index"], "start": v["start"],
                                  "productive_start": v["productive_start"], "end": v["end"],
                                  "line": v["mode"].line_id, "k": v["mode"].machine_count,
                                  "machines": list(v["mode"].machine_ids), "workers": list(v["mode"].worker_ids),
                                  "worker_segments": [[a.isoformat(), b.isoformat()] for a,b in v["worker_segments"]]}
                          for k,v in selected.items()}
        result["_selected_internal"] = selected
    return result


def _mode_coverage(state, direct_result):
    rows = []
    for key, selected in direct_result.get("_selected_internal", {}).items():
        mode = selected["mode"]
        match = []
        for existing in state["modes"].get(key, ()):
            if (_config_key(existing) == _config_key(mode) and int(existing.start_seconds) == selected["start"]
                    and int(existing.end_seconds) == selected["end"]):
                match.append(existing.mode_id)
        rows.append({"operation_key": key, "direct_start": selected["start"], "direct_end": selected["end"],
                     "config": {"line": mode.line_id, "k": mode.machine_count, "machines": list(mode.machine_ids),
                                "workers": list(mode.worker_ids)}, "equivalent_mode_ids": match,
                     "exact_mode_present": bool(match)})
    return rows


def _inject_modes(state, direct_result):
    modes = {k: list(v) for k, v in state["modes"].items()}
    injected = []
    internal = direct_result.get("_selected_internal", {})
    for key, selected in internal.items():
        base = selected["mode"]; start = selected["start"]; end = selected["end"]
        segs = tuple(selected["worker_segments"])
        candidate = replace(base, mode_id=f"{base.mode_id}|DIRECT_TIME_{start}",
                            start_seconds=start, productive_start_seconds=start + base.setup_seconds,
                            end_seconds=end, elapsed_seconds=end-start, pause_seconds=0,
                            worker_segments=segs, machine_occupancy_start_seconds=start,
                            machine_occupancy_end_seconds=end, incumbent=False,
                            provenance=tuple(base.provenance) + ("DIRECT_TIME_DIAGNOSTIC",),
                            recovery_level="BOUNDED_CRAFT_REPAIR", recovery_reason="P2_DIRECT_TIME_DIAGNOSTIC",
                            source_event_id=state["event"].event_id)
        modes[key].append(candidate); injected.append(candidate)
    return {k: tuple(v) for k,v in modes.items()}, injected


def run(output_dir=OUT):
    out = Path(output_dir)
    if out.exists() and any(out.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {out}")
    out.mkdir(parents=True, exist_ok=True)
    state = _prepare_state()
    inputs = []
    for key in CHAIN:
        inputs.append({"operation_key": key, "mode_count": len(state["modes"].get(key, ())),
                       "physical_configurations": len(_configs(state, key)),
                       "configs": [{"line": m.line_id, "k": m.machine_count, "machines": list(m.machine_ids),
                                    "workers": list(m.worker_ids), "setup": m.setup_seconds,
                                    "productive_seconds": m.productive_seconds} for m in _configs(state, key)]})
    _write(out / "micro_operation_inputs.json", {"event": state["event"].as_dict(), "operations": inputs,
                                                  "shift_policy": "FIXED_CREW_PAUSE_RESUME", "database_writes": 0})
    _write(out / "direct_time_model_definition.json", {"start_cp_variable": True,
        "end_cp_variable": "start + setup + productive_seconds", "worker_intervals": "productive segment only",
        "machine_intervals": "continuous setup+productive envelope", "calendar_windows": "exact 06/14/22/midnight piecewise windows",
        "routing": ["1029_293->1029_294", "1029_294->1029_295"], "objective": "PURE_FEASIBILITY",
        "outside_boundaries": "enabled only in boundary integration", "database_writes": 0})
    micro_art = _build_direct_model(state, outside=False)
    micro = _solve_direct(micro_art, state, seconds=45.0)
    _write(out / "direct_time_micro_result.json", {k:v for k,v in micro.items() if k != "_selected_internal"})
    boundary_art = _build_direct_model(state, outside=True)
    boundary = _solve_direct(boundary_art, state, seconds=45.0)
    boundary_status = "MICRO_FEASIBLE_BOUNDARY_INFEASIBLE" if micro["status"] in ("FEASIBLE", "OPTIMAL") and boundary["status"] not in ("FEASIBLE", "OPTIMAL") else ("MICRO_FEASIBLE" if boundary["status"] in ("FEASIBLE", "OPTIMAL") else "MICRO_INFEASIBLE")
    _write(out / "boundary_integration_result.json", {k:v for k,v in boundary.items() if k != "_selected_internal"} | {"classification": boundary_status})
    coverage = _mode_coverage(state, boundary if boundary["status"] in ("FEASIBLE", "OPTIMAL") else micro)
    _write(out / "joint_mode_coverage_comparison.json", coverage)
    injection = {"status": "NOT_RUN_NO_FEASIBLE_DIRECT_TIME", "injected": []}
    n100 = {"status": "NOT_RUN_NO_FEASIBLE_DIRECT_TIME"}
    replay = {"status": "NOT_RUN"}; validator = {"status": "NOT_RUN"}
    if boundary["status"] in ("FEASIBLE", "OPTIMAL"):
        injected_modes, injected = _inject_modes(state, boundary)
        n100_art = build_joint_mode_model(state["problem"], state["after_index"], state["neighborhood"], injected_modes)
        n100 = _solve_feasibility(n100_art, seconds_limit=60.0)
        injection = {"status": "INJECTED_EXPERIMENTAL_MODES", "count": len(injected),
                     "mode_ids": [m.mode_id for m in injected], "n100_model": n100_art["size"],
                     "production_generation_changed": False}
        if n100["status"] in ("FEASIBLE", "OPTIMAL"):
            selected = n100.get("selected", {})
            baseline = state["baseline"]
            phenotype = _phenotype(state["problem"], baseline, selected)
            valid = validate(state["problem"], phenotype, state["operator_index"])
            validator = {"status": "VALID" if valid.status.value == "WORKER_FEASIBLE" else str(valid.status),
                         "phenotype_status": str(valid.status)}
            mismatches = []
            for key, mode in selected.items():
                end, segs, mm = _replay_mode(state["problem"], state["operator_index"], state["after_index"], state["neighborhood"], mode)
                mismatches.extend([{**x, "operation_key": key} for x in mm])
            replay = {"status": "PASS" if not mismatches else "FAIL", "mismatches": mismatches}
    _write(out / "diagnostic_mode_injection.json", injection)
    _write(out / "n100_repair_result.json", {k:v for k,v in n100.items() if k != "_selected_internal"})
    _write(out / "replay_result.json", replay)
    _write(out / "validator_result.json", validator)
    if boundary["status"] not in ("FEASIBLE", "OPTIMAL"):
        root = "MICRO_INFEASIBLE" if micro["status"] not in ("FEASIBLE", "OPTIMAL") else "TRUE_BOUNDARY_CONFLICT"
    elif n100["status"] in ("FEASIBLE", "OPTIMAL") and replay.get("status") == "PASS" and validator.get("status") == "VALID":
        root = "CRAFT_P2_TIMING_DOMAIN_ROOT_CAUSE_CONFIRMED"
    else:
        root = "CRAFT_P2_JOINT_MODE_TIMING_INCOMPLETENESS_CONFIRMED" if any(not x["exact_mode_present"] for x in coverage) else "N100_REMAINS_INFEASIBLE_AFTER_TIMING_INJECTION"
    _write(out / "root_cause_classification.json", {"status": root, "micro_classification": boundary_status,
        "micro_status": micro["status"], "boundary_status": boundary["status"], "n100_status": n100.get("status"),
        "physical_semantics_changed": False, "shift_policy": "FIXED_CREW_PAUSE_RESUME", "database_writes": 0})
    report = f"""# P2 direct-time micro repair

Primary classification: `{root}`.

The three-operation route was tested with CP-owned starts and derived end times. Micro status: `{micro["status"]}`. Boundary status: `{boundary["status"]}`. N100 after experimental timing injection: `{n100.get("status")}`. Replay: `{replay.get("status")}`. Validator: `{validator.get("status")}`.

This prototype did not alter production mode generation or scheduling semantics. Shift policy remained `FIXED_CREW_PAUSE_RESUME`; no handover, floater, database, B/N, or iteration changes were made.
"""
    (out / "final_report.md").write_text(report, encoding="utf-8")
    files = {str(p.relative_to(out)).replace("\\", "/"): _sha(p) for p in sorted(out.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    _write(out / "artifact_hash_manifest.json", {"files": files, "artifact_count": len(files), "status": root, "database_writes": 0})
    return {"status": root, "output_dir": str(out), "micro": micro["status"], "boundary": boundary["status"], "n100": n100.get("status"), "database_writes": 0}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
