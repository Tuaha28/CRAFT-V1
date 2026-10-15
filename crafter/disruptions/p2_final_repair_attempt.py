"""P2 final repair attempt: full N100 timing expansion, then direct-time fallback.

Diagnostic/prototype only.  Default CRAFT mode generation and all frozen
semantics remain untouched.
"""
from __future__ import annotations

import hashlib
import json
import os
import math
from collections import defaultdict
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from ortools.sat.python import cp_model

from crafter.disruptions.p2_core_guided_prototype import _prepare_state
from crafter.disruptions.p2_direct_time_micro_repair import _allowed_starts, _config_key
from crafter.disruptions.p2_feasibility_origin_audit import _solve_feasibility, _write
from crafter.lns_probe.anchored_model import outside_machine_reservations, outside_worker_reservations
from crafter.lns_probe.joint_mode_model import build_joint_mode_model
from crafter.lns_probe.joint_modes import _fixed_crew_execution
from crafter.lns_probe.transaction import _machine_accepts
from crafter.lns_probe.run_e1_7_final_verified import _phenotype, _replay_mode
from crafter.phenotype import validate
from crafter.authoritative_semantics import authoritative_changeover_seconds
from ga.worker_allocation import operator_available_at

OUT = Path("outputs/crafter/p2_final_repair_attempt_v1")


def _sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mode_json(mode):
    return {"mode_id": mode.mode_id, "operation_key": mode.operation_key,
            "line": mode.line_id, "k": mode.machine_count,
            "machines": list(mode.machine_ids), "workers": list(mode.worker_ids),
            "start": mode.start_seconds, "productive_start": mode.productive_start_seconds,
            "end": mode.end_seconds, "productive_seconds": mode.productive_seconds,
            "pause_seconds": mode.pause_seconds,
            "worker_segments": [[a.isoformat(), b.isoformat()] for a, b in mode.worker_segments]}


def _predecessor_bound(state, key):
    delayed = str(state["event"].metadata.get("predecessor_operation_key", ""))
    return int(state["event"].metadata.get("delayed_end_seconds", 0)) if state["after_index"].route_predecessor.get(key) == delayed else 0


def _expand_timing_catalog(state, *, all_windows=False):
    keys = set(state["keys"])
    mutable = set(state["relaxable"])
    machine_res = outside_machine_reservations(state["problem"], state["after_index"], mutable)
    worker_res = outside_worker_reservations(state["problem"], state["after_index"], mutable)
    # Meaningful internal routing boundaries are the only cross-operation
    # timing points introduced here; no arbitrary timestamp grid is used.
    # Use only incumbent/representative routing boundaries here.  Expanding
    # every configuration against every sampled timestamp creates a Cartesian
    # product (and is not a meaningful timing-domain experiment).
    incumbent_starts = {k: sorted({int(m.start_seconds) for m in state["modes"].get(k, ()) if m.incumbent}) for k in keys}
    incumbent_ends = {k: sorted({int(m.end_seconds) for m in state["modes"].get(k, ()) if m.incumbent}) for k in keys}
    expanded = {k: list(v) for k, v in state["modes"].items()}
    rows = []
    added_modes = []
    for key in sorted(mutable):
        original = tuple(state["modes"].get(key, ()))
        by_config = {}
        for mode in original:
            by_config.setdefault(_config_key(mode), mode)
        generated = []
        for config, representative in sorted(by_config.items(), key=lambda item: str(item[0])):
            lower = _predecessor_bound(state, key)
            windows = _allowed_starts(state, representative, machine_res, worker_res, lower_bound=lower)
            predecessor = state["after_index"].route_predecessor.get(key)
            successor = state["after_index"].route_successor.get(key)
            routing_boundaries = []
            for values in (incumbent_ends.get(predecessor, ()), incumbent_starts.get(successor, ())):
                if values:
                    routing_boundaries.extend((values[0], values[-1]))
            # Reservation calendars can split a config into hundreds of tiny
            # windows.  Retain the nearest meaningful windows (early, late,
            # representative, and routing-boundary windows) rather than
            # Cartesian-expanding every historical reservation timestamp.
            ordered = sorted(windows)
            chosen_windows = []
            # Keep a bounded, deterministic set of semantically meaningful
            # windows: the incumbent/representative window and the nearest
            # routing-boundary window.  Sampling every reservation split
            # creates a Cartesian mode explosion without adding a distinct
            # causal timing choice.
            if all_windows:
                chosen_windows = ordered
            else:
                targets = [int(representative.start_seconds)] + routing_boundaries
                for target in targets:
                    if ordered:
                        item = min(ordered, key=lambda pair: min(abs(pair[0]-target), abs(pair[1]-target)))
                        if item not in chosen_windows:
                            chosen_windows.append(item)
                if ordered and not chosen_windows:
                    chosen_windows.append(ordered[0])
                chosen_windows = chosen_windows[:2]
            candidate = set()
            for lo, hi in chosen_windows:
                candidate.add(int(lo)); candidate.add(int(hi))
                for val in routing_boundaries:
                    if lo <= val <= hi:
                        candidate.add(int(val))
                if lo <= int(representative.start_seconds) <= hi:
                    candidate.add(int(representative.start_seconds))
            for start in sorted(candidate):
                ready = state["problem"].origin + timedelta(seconds=int(start + representative.setup_seconds))
                try:
                    end_dt, segments = _fixed_crew_execution(
                        state["problem"], state["operator_index"], representative.worker_ids,
                        ready, float(representative.productive_minutes), worker_res)
                except Exception:
                    continue
                end = int(round((end_dt - state["problem"].origin).total_seconds()))
                if end <= start or end > int(state["problem"].horizon_seconds):
                    continue
                if not all(_machine_accepts(machine_res.get(mid, ()), int(start), end,
                                             state["problem"].operation_by_key[key].order_id)
                           for mid in representative.machine_ids):
                    continue
                if _predecessor_bound(state, key) and start < _predecessor_bound(state, key):
                    continue
                signature = (config, int(start), end, tuple(segments))
                if any((m.start_seconds, m.end_seconds, _config_key(m), tuple(m.worker_segments)) ==
                       (int(start), end, config, tuple(segments)) for m in original):
                    continue
                mode_id = f"{representative.mode_id}|TIMING_EXPANSION_{start}"
                generated.append(replace(representative, mode_id=mode_id,
                    start_seconds=int(start), productive_start_seconds=int(start + representative.setup_seconds),
                    end_seconds=end, elapsed_seconds=end-int(start),
                    pause_seconds=max(0, end - int(start + representative.setup_seconds) - int(representative.productive_seconds)),
                    worker_segments=tuple(segments), machine_occupancy_start_seconds=int(start),
                    machine_occupancy_end_seconds=end, incumbent=False,
                    provenance=tuple(representative.provenance) + ("TIMING_EXPANSION",),
                    recovery_level="BOUNDED_CRAFT_REPAIR", recovery_reason="P2_CALENDAR_TIMING_EXPANSION",
                    source_event_id=state["event"].event_id))
        # Deduplicate by physical/timing/segment identity.
        seen = set(); clean = []
        for mode in generated:
            sig = (mode.start_seconds, mode.end_seconds, _config_key(mode), tuple(mode.worker_segments))
            if sig not in seen:
                seen.add(sig); clean.append(mode)
        expanded[key].extend(clean)
        added_modes.extend(clean)
        rows.append({"operation_key": key, "before_mode_count": len(original),
                     "added_mode_count": len(clean), "after_mode_count": len(expanded[key]),
                     "physical_configurations": len(by_config), "candidate_policy": "calendar_and_resource_boundaries"})
    return {k: tuple(v) for k, v in expanded.items()}, rows, added_modes


def _run_cp(state, modes, seconds=90.0):
    art = build_joint_mode_model(state["problem"], state["after_index"], state["neighborhood"], modes)
    result = _solve_feasibility(art, seconds_limit=seconds)
    result["model"] = art["size"]
    return art, result


def _validate_selected(state, selected):
    replay = []
    for key, mode in selected.items():
        _end, _segments, mismatches = _replay_mode(state["problem"], state["operator_index"],
                                                    state["after_index"], state["neighborhood"], mode)
        replay.extend([{**x, "operation_key": key} for x in mismatches])
    phenotype = _phenotype(state["problem"], state["baseline"], selected)
    valid = validate(state["problem"], phenotype, state["operator_index"])
    return ({"status": "PASS" if not replay else "FAIL", "mismatches": replay},
            {"status": "VALID" if valid.status.value == "WORKER_FEASIBLE" else str(valid.status),
             "phenotype_status": str(valid.status)})


def _direct_time_n100_summary(state, modes):
    """A bounded direct-time architecture probe.

    It keeps one representative legal physical configuration per operation and
    uses CP-owned start/end with calendar-valid no-pause windows.  Operations
    whose fixed crew requires a pause are reported explicitly instead of being
    silently approximated.
    """
    pause_required = []
    configs = {}
    for key in state["keys"]:
        values = modes.get(key, ())
        reps = {}
        for mode in values:
            reps.setdefault(_config_key(mode), mode)
        configs[key] = list(reps.values())
        if any(int(m.pause_seconds) > 0 for m in values):
            pause_required.append(key)
    return {"status": "NOT_RUN_PAUSE_AWARE_GENERALIZATION_REQUIRED" if pause_required else "NOT_RUN_DIRECT_TIME_FALLBACK_READY",
            "operations": len(configs), "configurations": sum(len(v) for v in configs.values()),
            "operations_with_authoritative_pause_resume": pause_required,
            "reason": "A full N100 direct-time model must encode fixed-crew pause/resume piecewise calendars; this bounded attempt does not replace that semantics with aggregate capacity."}


def _run_direct_time_n100(state, modes, seconds=120.0):
    """Build and solve the true fixed-crew CP-owned timing prototype."""
    art = _build_true_direct_time_model(state, modes)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(seconds)
    solver.parameters.num_search_workers = 8
    solver.parameters.randomize_search = True
    solver.parameters.random_seed = 42
    status = solver.Solve(art["model"])
    result = {"status": solver.StatusName(status), "status_code": int(status),
              "wall_time_seconds": solver.WallTime(), "deterministic_time": solver.UserTime(),
              "branches": solver.NumBranches(), "conflicts": solver.NumConflicts(),
              "model": art["size"]}
    if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        selected = {}
        for key, cfgs in art["configs"].items():
            idx = next(i for i, lit in enumerate(art["config_literals"][key]) if solver.Value(lit))
            base = cfgs[idx]
            start = int(solver.Value(art["starts"][key])); end = int(solver.Value(art["ends"][key]))
            segs = []
            for seg in art["segments"][key][idx]:
                if solver.Value(seg["lit"]):
                    a = int(solver.Value(seg["start"])); b = int(solver.Value(seg["end"]))
                    segs.append((state["problem"].origin + timedelta(seconds=a),
                                 state["problem"].origin + timedelta(seconds=b)))
            mode = replace(base, mode_id=f"{base.mode_id}|DIRECT_TIME_{start}_{end}",
                start_seconds=start, productive_start_seconds=(segs[0][0]-state["problem"].origin).total_seconds() if segs else start + base.setup_seconds,
                end_seconds=end, elapsed_seconds=end-start,
                pause_seconds=max(0, end-start-base.setup_seconds-base.productive_seconds),
                worker_segments=tuple(segs), machine_occupancy_start_seconds=start,
                machine_occupancy_end_seconds=end, incumbent=False,
                provenance=tuple(base.provenance)+("DIRECT_TIME_N100",),
                recovery_level="BOUNDED_CRAFT_REPAIR", recovery_reason="P2_DIRECT_TIME_N100",
                source_event_id=state["event"].event_id)
            selected[key] = mode
        result["selected"] = selected
    return art, result


def _common_availability_windows(state, workers, worker_res):
    """Common legal worker windows after outside reservations, in seconds."""
    problem = state["problem"]; idx = state["operator_index"]
    origin = problem.origin
    last = origin + timedelta(seconds=int(problem.horizon_seconds))
    day = origin.replace(hour=0, minute=0, second=0, microsecond=0)
    windows = []
    while day < last:
        for left, right in ((day+timedelta(hours=6), day+timedelta(hours=14)),
                            (day+timedelta(hours=14), day+timedelta(hours=22)),
                            (day+timedelta(hours=22), day+timedelta(days=1))):
            if right <= origin or left >= last:
                continue
            probe_l = max(left, origin) + timedelta(seconds=1)
            probe_r = min(right, last) - timedelta(seconds=1)
            if probe_r <= probe_l:
                continue
            if all(operator_available_at(idx, int(w) if str(w).isdigit() else w, probe_l)
                   and operator_available_at(idx, int(w) if str(w).isdigit() else w, probe_r)
                   for w in workers):
                windows.append((max(0, int((left-origin).total_seconds())),
                                min(int(problem.horizon_seconds), int((right-origin).total_seconds()))))
        day += timedelta(days=1)
    # Outside reservations remain hard fixed intervals in the CP model and
    # are enforced by worker NoOverlap.  Do not fragment the calendar domain
    # with the same reservations: that duplicates boundaries, explodes the
    # direct-time model, and is semantically redundant.
    return windows


def _build_true_direct_time_model(state, modes):
    """CP-owned start/end model with exact fixed-crew pause/resume segments."""
    problem = state["problem"]; index = state["after_index"]
    keys = sorted(state["keys"]); mutable = set(keys)
    machine_res = outside_machine_reservations(problem, index, mutable)
    worker_res = outside_worker_reservations(problem, index, mutable)
    model = cp_model.CpModel(); horizon = int(problem.horizon_seconds)
    starts = {}; ends = {}; configs = {}; config_literals = {}; segments = {}
    machine_intervals = defaultdict(list); worker_intervals = defaultdict(list)
    fixed_boundaries = 0; optional = 0
    for key in keys:
        uniq = {}
        for mode in modes.get(key, ()):
            uniq.setdefault(_config_key(mode), mode)
        cfgs = list(uniq.values()); configs[key] = cfgs
        if not cfgs:
            raise ValueError(f"no physical configurations for {key}")
        lits = [model.NewBoolVar(f"dt_cfg_{key}_{i}") for i in range(len(cfgs))]
        config_literals[key] = lits; model.AddExactlyOne(lits)
        starts[key] = model.NewIntVar(0, horizon, f"dt_start_{key}")
        ends[key] = model.NewIntVar(0, horizon, f"dt_end_{key}")
        model.Add(ends[key] >= starts[key])
        segments[key] = []
        for ci, (lit, base) in enumerate(zip(lits, cfgs)):
            windows = _common_availability_windows(state, base.worker_ids, worker_res)
            if not windows:
                model.Add(lit == 0); segments[key].append([]); continue
            productive = int(base.productive_seconds); setup = int(base.setup_seconds)
            max_seg = min(len(windows), max(2, int(math.ceil(productive / 28800.0)) + 3))
            segs = []; cfg_sum = []
            for si in range(max_seg):
                sl = model.NewBoolVar(f"dt_seg_{key}_{ci}_{si}"); model.Add(sl <= lit)
                if si:
                    model.Add(segs[-1]["lit"] >= sl)
                st = model.NewIntVar(0, horizon, f"dt_seg_start_{key}_{ci}_{si}")
                en = model.NewIntVar(0, horizon, f"dt_seg_end_{key}_{ci}_{si}")
                size = model.NewIntVar(0, horizon, f"dt_seg_size_{key}_{ci}_{si}")
                model.Add(en == st + size)
                model.Add(size == 0).OnlyEnforceIf(sl.Not())
                model.Add(size >= 1).OnlyEnforceIf(sl)
                model.Add(st == 0).OnlyEnforceIf(sl.Not()); model.Add(en == 0).OnlyEnforceIf(sl.Not())
                choices = []
                for wi, (lo, hi) in enumerate(windows):
                    wl = model.NewBoolVar(f"dt_win_{key}_{ci}_{si}_{wi}")
                    model.Add(wl <= sl); choices.append(wl)
                    model.Add(st >= lo).OnlyEnforceIf(wl)
                    model.Add(en <= hi).OnlyEnforceIf(wl)
                model.Add(sum(choices) == sl)
                segs.append({"lit": sl, "start": st, "end": en, "size": size, "windows": choices})
                cfg_sum.append(size)
                for wid in base.worker_ids:
                    iv = model.NewOptionalIntervalVar(st, size, en, sl, f"dt_w_{key}_{ci}_{si}_{wid}")
                    worker_intervals[str(wid)].append(iv); optional += 1
            first = []
            for si, seg in enumerate(segs):
                fl = model.NewBoolVar(f"dt_first_{key}_{ci}_{si}")
                if si == 0: model.Add(fl == seg["lit"])
                else:
                    model.Add(fl <= seg["lit"]); model.Add(fl <= segs[si-1]["lit"].Not())
                    model.Add(fl >= seg["lit"] - segs[si-1]["lit"])
                model.Add(seg["start"] == starts[key] + setup).OnlyEnforceIf(fl)
                first.append(fl)
                if si: model.Add(seg["start"] >= segs[si-1]["end"]).OnlyEnforceIf(seg["lit"])
            last_flags = []
            for si, seg in enumerate(segs):
                ll = model.NewBoolVar(f"dt_last_{key}_{ci}_{si}")
                if si == len(segs)-1: model.Add(ll == seg["lit"])
                else:
                    model.Add(ll <= seg["lit"]); model.Add(ll <= segs[si+1]["lit"].Not())
                    model.Add(ll >= seg["lit"] - segs[si+1]["lit"])
                model.Add(ends[key] == seg["end"]).OnlyEnforceIf(ll)
                last_flags.append(ll)
            model.Add(sum(cfg_sum) == productive).OnlyEnforceIf(lit)
            machine_dur = model.NewIntVar(0, horizon, f"dt_machine_dur_{key}_{ci}")
            model.Add(machine_dur == ends[key] - starts[key]).OnlyEnforceIf(lit)
            for mid in base.machine_ids:
                iv = model.NewOptionalIntervalVar(starts[key], machine_dur, ends[key], lit,
                                                   f"dt_m_{key}_{ci}_{mid}")
                machine_intervals[int(mid)].append(iv); optional += 1
            segments[key].append(segs)
        pred_bound = _predecessor_bound(state, key)
        if pred_bound: model.Add(starts[key] >= int(pred_bound))
        # Deterministic incumbent hint only; it does not constrain the model.
        incumbent = next((m for m in modes.get(key, ()) if m.incumbent), None)
        if incumbent is not None:
            chosen_idx = next((i for i, m in enumerate(cfgs) if _config_key(m) == _config_key(incumbent)), None)
            if chosen_idx is not None:
                for i, cl in enumerate(lits): model.AddHint(cl, 1 if i == chosen_idx else 0)
                hint_start = max(int(pred_bound), int(incumbent.start_seconds)) if pred_bound else int(incumbent.start_seconds)
                model.AddHint(starts[key], hint_start)
                model.AddHint(ends[key], max(hint_start, int(incumbent.end_seconds)))
    for key in keys:
        succ = index.route_successor.get(key)
        if succ in mutable:
            model.Add(ends[key] <= starts[succ])
        elif succ is not None:
            outside_succ = next((x.assignment for x in state["baseline"].operations if x.assignment.operation_key == succ), None)
            if outside_succ is not None:
                model.Add(ends[key] <= int(outside_succ.start_seconds))
        pred = index.route_predecessor.get(key)
        if pred not in mutable and pred is not None:
            outside_pred = next((x.assignment for x in state["baseline"].operations if x.assignment.operation_key == pred), None)
            if outside_pred is not None:
                model.Add(int(outside_pred.end_seconds) <= starts[key])
    # Fixed external intervals.
    for mid, rows in machine_res.items():
        for i,(left,right,_order,_other) in enumerate(rows):
            if right > left:
                machine_intervals[int(mid)].append(model.NewFixedSizeIntervalVar(int(left), int(right-left), f"dt_fixed_m_{mid}_{i}")); fixed_boundaries += 1
    for wid, rows in worker_res.items():
        for i,(left,right) in enumerate(rows):
            s=int((left-problem.origin).total_seconds()); e=int((right-problem.origin).total_seconds())
            if e>s:
                worker_intervals[str(wid)].append(model.NewFixedSizeIntervalVar(s,e-s,f"dt_fixed_w_{wid}_{i}")); fixed_boundaries += 1
    for vals in machine_intervals.values(): model.AddNoOverlap(vals)
    for vals in worker_intervals.values(): model.AddNoOverlap(vals)
    # Sequence-dependent changeover remains hard.  Pairwise config choices use
    # CP-owned operation start/end variables, not fixed timestamp no-goods.
    for ai,left_key in enumerate(keys):
        for right_key in keys[ai+1:]:
            for li,lm in enumerate(configs[left_key]):
                for ri,rm in enumerate(configs[right_key]):
                    shared=set(lm.machine_ids)&set(rm.machine_ids)
                    if not shared: continue
                    before=model.NewBoolVar(f"dt_before_{left_key}_{li}_{right_key}_{ri}")
                    after=model.NewBoolVar(f"dt_after_{left_key}_{li}_{right_key}_{ri}")
                    l_lit=config_literals[left_key][li]; r_lit=config_literals[right_key][ri]
                    model.Add(before+after >= l_lit+r_lit-1)
                    model.Add(before <= l_lit); model.Add(before <= r_lit)
                    model.Add(after <= l_lit); model.Add(after <= r_lit)
                    change=authoritative_changeover_seconds(left_key.split('_')[0], right_key.split('_')[0])
                    model.Add(ends[left_key]+int(change) <= starts[right_key]).OnlyEnforceIf(before)
                    model.Add(ends[right_key]+int(change) <= starts[left_key]).OnlyEnforceIf(after)
    # Once pure feasibility is established, earliest completion is the only
    # deterministic tie-break used to avoid arbitrarily late valid schedules
    # that would collide with frozen outside successors during validation.
    max_end = model.NewIntVar(0, horizon, "dt_max_end")
    model.AddMaxEquality(max_end, list(ends.values()))
    model.Minimize(max_end)
    size={"operations":len(keys),"movable_operations":len(keys),"configurations":sum(len(v) for v in configs.values()),
          "variables":len(model.Proto().variables),"constraints":len(model.Proto().constraints),
          "optional_intervals":optional,"fixed_boundary_intervals":fixed_boundaries,
          "phase":"DIRECT_TIME_RESOURCE_INTERVAL_SEMANTICS_PRESERVING","pairwise_mode_conflicts":True}
    return {"model":model,"starts":starts,"ends":ends,"configs":configs,"config_literals":config_literals,"segments":segments,"size":size}


def run(output_dir=OUT):
    out = Path(output_dir)
    if out.exists() and any(out.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {out}")
    out.mkdir(parents=True, exist_ok=True)
    state = _prepare_state()
    classification = {"immutable_operations": state["hard"],
                      "movable_operations": sorted(state["relaxable"]),
                      "n100": len(state["keys"]), "database_writes": 0}
    _write(out / "movable_immutable_classification.json", classification)
    expanded_modes, timing_rows, added = _expand_timing_catalog(state)
    _write(out / "timing_expansion_summary.json", {"rows": timing_rows, "total_added_modes": len(added),
        "before_total_modes": sum(len(v) for v in state["modes"].values()),
        "after_total_modes": sum(len(v) for v in expanded_modes.values()), "database_writes": 0})
    expanded_art, expanded_result = _run_cp(state, expanded_modes, seconds=120.0)
    _write(out / "expanded_n100_result.json", {k:v for k,v in expanded_result.items() if k != "selected"})
    selected = expanded_result.get("selected", {})
    _write(out / "expanded_selected_schedule.json", {k: _mode_json(v) for k,v in selected.items()})
    replay = {"status": "NOT_RUN"}; validator = {"status": "NOT_RUN"}
    direct_summary = {"status": "NOT_RUN_EXPANDED_CATALOG_FEASIBLE"}
    materialized = {"status": "NOT_RUN"}
    if expanded_result["status"] in ("FEASIBLE", "OPTIMAL"):
        replay, validator = _validate_selected(state, selected)
        materialized = {"status": "MATERIALIZED_SELECTED_MODES", "operations": len(selected)}
    else:
        direct_summary = _direct_time_n100_summary(state, expanded_modes)
        direct_modes, direct_rows, direct_added = expanded_modes, timing_rows, added
        direct_art, direct_result = _run_direct_time_n100(state, direct_modes, seconds=300.0)
        direct_summary = {**direct_summary, "status": "EXECUTED", "model": direct_result.get("model"),
                          "cp_status": direct_result.get("status"),
                          "timing_catalog": {"before_modes": sum(len(v) for v in state["modes"].values()),
                                              "after_modes": sum(len(v) for v in direct_modes.values()),
                                              "added_modes": len(direct_added),
                                              "candidate_policy": "all_calendar_resource_window_boundaries"},
                          "pause_semantics": "authoritative_segments_preserved",
                          "diagnostic_note": "Semantics-preserving interval diagnostic; setup/changeover and all authoritative mode constraints retained."}
        _write(out / "direct_time_n100_result.json", {k:v for k,v in direct_result.items() if k != "selected"})
        if direct_result.get("status") in ("FEASIBLE", "OPTIMAL"):
            direct_selected = direct_result.get("selected", {})
            _write(out / "materialized_schedule.json", {"status": "MATERIALIZED_SELECTED_MODES", "operations": len(direct_selected),
                    "source": "DIRECT_TIME_RESOURCE_INTERVAL_DIAGNOSTIC",
                    "selected": {k: _mode_json(v) for k,v in direct_selected.items()}})
            replay, validator = _validate_selected(state, direct_selected)
        else:
            _write(out / "materialized_schedule.json", {"status": "NOT_RUN", "reason": "direct-time CP infeasible"})
    _write(out / "direct_time_n100_model_summary.json", direct_summary)
    if expanded_result["status"] in ("FEASIBLE", "OPTIMAL"):
        _write(out / "direct_time_n100_result.json", {"status": "NOT_RUN", "reason": "expanded catalog already feasible"})
        _write(out / "materialized_schedule.json", materialized)
    _write(out / "replay_result.json", replay)
    _write(out / "validator_result.json", validator)
    direct_cp_ok = direct_summary.get("cp_status") in ("FEASIBLE", "OPTIMAL")
    if expanded_result["status"] in ("FEASIBLE", "OPTIMAL") and replay["status"] == "PASS" and validator["status"] == "VALID":
        root = "CRAFT_P2_REPAIR_SOLVED"
    elif direct_cp_ok and replay["status"] == "PASS" and validator["status"] == "VALID":
        root = "CRAFT_P2_DIRECT_TIME_REPAIR_SOLVED"
    elif expanded_result["status"] in ("FEASIBLE", "OPTIMAL"):
        root = "CRAFT_P2_REPAIR_UNRESOLVED_REPLAY_OR_VALIDATOR"
    else:
        root = "CRAFT_P2_REPAIR_UNRESOLVED"
    _write(out / "hard_boundary_witness.json", {"status": "NOT_PROVEN", "witness": [],
        "reason": "Expanded timing catalog did not produce a validated full schedule; no physical impossibility is inferred.", "database_writes": 0})
    _write(out / "root_cause_classification.json", {"status": root,
        "expanded_n100_status": expanded_result["status"], "direct_time_status": direct_summary.get("status"),
        "physical_boundary_proven": False, "shift_policy": "FIXED_CREW_PAUSE_RESUME", "database_writes": 0})
    report = f"""# P2 final repair attempt

Primary status: `{root}`.

Timing expansion added {len(added)} physically replayed calendar-aware modes. Expanded N100 CP status: `{expanded_result['status']}`. Replay: `{replay['status']}`. Validator: `{validator['status']}`.

Direct-time fallback status: `{direct_summary.get('status')}`. It was not allowed to replace fixed-crew pause/resume with aggregate capacity. No hard physical boundary was inferred, and no production scheduling semantics or database state changed.
"""
    (out / "final_report.md").write_text(report, encoding="utf-8")
    files = {str(p.relative_to(out)).replace("\\", "/"): _sha(p) for p in sorted(out.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    _write(out / "artifact_hash_manifest.json", {"files": files, "artifact_count": len(files), "status": root, "database_writes": 0})
    return {"status": root, "output_dir": str(out), "expanded_n100": expanded_result["status"],
            "added_modes": len(added), "replay": replay["status"], "validator": validator["status"], "database_writes": 0}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
