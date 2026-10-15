"""Small CP-SAT selector over immutable, jointly executable modes (E1.7)."""
from __future__ import annotations

from collections import defaultdict
import os
import time

import psutil
from ortools.sat.python import cp_model

from crafter.authoritative_semantics import authoritative_changeover_seconds
from .anchored_model import outside_machine_reservations, outside_worker_reservations


def _rss_mb():
    return psutil.Process(os.getpid()).memory_info().rss / 1024.0**2


def _overlap(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1


def _compatible_machine(left, right):
    if not _overlap(left.start_seconds, left.end_seconds,
                    right.start_seconds, right.end_seconds):
        if left.end_seconds <= right.start_seconds:
            return left.end_seconds + authoritative_changeover_seconds(
                left.operation_key.split("_")[0], right.operation_key.split("_")[0]) <= right.start_seconds
        return right.end_seconds + authoritative_changeover_seconds(
            right.operation_key.split("_")[0], left.operation_key.split("_")[0]) <= left.start_seconds
    return False


def build_joint_mode_model(problem, index, neighborhood, modes_by_key, *, diagnostic_disable=(),
                           routing_assumptions=False):
    started = time.perf_counter(); rss_before = _rss_mb()
    model = cp_model.CpModel()
    mutable = set(neighborhood.operation_keys)
    machine_res = outside_machine_reservations(problem, index, mutable)
    worker_res = outside_worker_reservations(problem, index, mutable)
    literals, starts, ends, lines, counts = {}, {}, {}, {}, {}
    machine_intervals = defaultdict(list); worker_intervals = defaultdict(list)
    optional = 0; fixed_boundary = 0
    for key in sorted(mutable):
        modes = tuple(modes_by_key[key])
        if not modes:
            raise ValueError(f"no executable modes for {key}")
        lits = [model.NewBoolVar(f"mode_{key}_{i}") for i in range(len(modes))]
        literals[key] = lits
        model.AddExactlyOne(lits)
        starts[key] = model.NewIntVar(min(m.start_seconds for m in modes),
                                      max(m.start_seconds for m in modes), f"start_{key}")
        ends[key] = model.NewIntVar(min(m.end_seconds for m in modes),
                                    max(m.end_seconds for m in modes), f"end_{key}")
        lines[key] = model.NewIntVar(min(m.line_id for m in modes), max(m.line_id for m in modes), f"line_{key}")
        counts[key] = model.NewIntVar(min(m.machine_count for m in modes), max(m.machine_count for m in modes), f"k_{key}")
        model.Add(starts[key] == sum(l * m.start_seconds for l, m in zip(lits, modes)))
        model.Add(ends[key] == sum(l * m.end_seconds for l, m in zip(lits, modes)))
        model.Add(lines[key] == sum(l * m.line_id for l, m in zip(lits, modes)))
        model.Add(counts[key] == sum(l * m.machine_count for l, m in zip(lits, modes)))
        for lit, mode in zip(lits, modes):
            for machine_id in mode.machine_ids:
                iv = model.NewOptionalFixedSizeIntervalVar(
                    mode.start_seconds, max(0, mode.end_seconds-mode.start_seconds), lit,
                    f"mach_{key}_{machine_id}_{mode.mode_id}")
                machine_intervals[int(machine_id)].append(iv); optional += 1
            # A worker is reserved only on the authoritative productive segments.
            for worker_id in mode.worker_ids:
                for seg_i, (left, right) in enumerate(mode.worker_segments):
                    s = int(round((left-problem.origin).total_seconds()))
                    e = int(round((right-problem.origin).total_seconds()))
                    if e <= s: continue
                    iv = model.NewOptionalFixedSizeIntervalVar(s, e-s, lit,
                        f"work_{key}_{worker_id}_{seg_i}_{mode.mode_id}")
                    worker_intervals[str(worker_id)].append(iv); optional += 1
    # Fixed external occupancy is included as fixed intervals.  The exact
    # mode generation already screened these boundaries; this protects the
    # CP model against accidental regressions.
    disabled=set(diagnostic_disable)
    for machine_id, rows in machine_res.items():
        for i, (left, right, _order, _other) in enumerate(rows):
            if right > left:
                if "frozen_machine_reservations" not in disabled:
                    machine_intervals[int(machine_id)].append(
                        model.NewFixedSizeIntervalVar(int(left), int(right-left), f"fixed_m_{machine_id}_{i}"))
                fixed_boundary += 1
    for worker_id, rows in worker_res.items():
        for i, (left, right) in enumerate(rows):
            s = int(round((left-problem.origin).total_seconds())); e = int(round((right-problem.origin).total_seconds()))
            if e > s:
                if "frozen_worker_reservations" not in disabled:
                    worker_intervals[str(worker_id)].append(
                        model.NewFixedSizeIntervalVar(s, e-s, f"fixed_w_{worker_id}_{i}"))
                fixed_boundary += 1
    if "machine_no_overlap" not in disabled:
        for intervals in machine_intervals.values():
            model.AddNoOverlap(intervals)
    if "worker_no_overlap" not in disabled:
        for intervals in worker_intervals.values():
            model.AddNoOverlap(intervals)
    # Constant mode compatibility captures sequence-dependent transitions.
    conflict_constraints = 0
    keys = sorted(mutable)
    for ai, left_key in enumerate(keys):
        for right_key in keys[ai+1:]:
            for li, lm in enumerate(modes_by_key[left_key]):
                for ri, rm in enumerate(modes_by_key[right_key]):
                    shared = set(lm.machine_ids) & set(rm.machine_ids)
                    if "mode_conflicts" not in disabled and shared and not _compatible_machine(lm, rm):
                        model.Add(literals[left_key][li] + literals[right_key][ri] <= 1); conflict_constraints += 1
                    shared_workers = set(lm.worker_ids) & set(rm.worker_ids)
                    if shared_workers:
                        # Segment-level worker intervals also enforce this,
                        # while this no-good keeps the Boolean model compact.
                        overlap = any(_overlap(int(round((a-problem.origin).total_seconds())), int(round((b-problem.origin).total_seconds())),
                                                    int(round((c-problem.origin).total_seconds())), int(round((d-problem.origin).total_seconds())))
                                      for a,b in lm.worker_segments for c,d in rm.worker_segments)
                        if "mode_conflicts" not in disabled and overlap:
                            model.Add(literals[left_key][li] + literals[right_key][ri] <= 1); conflict_constraints += 1
    # Routing arcs wholly inside the local region.  The optional assumption
    # guards are diagnostic-only: the default model remains byte-for-byte
    # equivalent in its scheduling semantics.  They let P2 provenance tools
    # ask CP-SAT for a sufficient infeasible core of ordinary routing arcs.
    disabled_routing_edges = set()
    routing_assumption_mapping = {}
    for item in disabled:
        if isinstance(item, str) and item.startswith("routing_edge:") and "->" in item:
            left, right = item.split(":", 1)[1].split("->", 1)
            disabled_routing_edges.add((str(left), str(right)))
    for key in keys:
        succ = index.route_successor.get(key)
        if ("routing_precedence" not in disabled and succ in mutable
                and (str(key), str(succ)) not in disabled_routing_edges):
            if routing_assumptions:
                assumption = model.NewBoolVar(f"assume_route_{key}_{succ}")
                model.Add(ends[key] <= starts[succ]).OnlyEnforceIf(assumption)
                model.AddAssumption(assumption)
                cid = f"R_PRECEDENCE:{key}->{succ}"
                routing_assumption_mapping[int(assumption.Index())] = {
                    "constraint_id": cid,
                    "category": "R_PRECEDENCE",
                    "predecessor": str(key),
                    "successor": str(succ),
                    "relation": "end(predecessor) <= start(successor)",
                    "assumption_index": int(assumption.Index()),
                }
            else:
                model.Add(ends[key] <= starts[succ])
    # Exact E1.4--E1.6 delivery-aware local objective: tardiness dominates,
    # with local makespan as the deterministic tie-break. Final acceptance
    # still uses the complete authoritative phenotype score.
    tardiness = []
    for order_id in neighborhood.order_ids:
        keys_for_order = [k for k in index.order_operations.get(order_id, ()) if k in mutable]
        if not keys_for_order:
            continue
        final = max(index.order_operations[order_id],
                    key=lambda k: problem.operation_by_key[k].sequence)
        if final not in mutable:
            continue
        due = problem.order_by_id[order_id].deadline_seconds
        if due is None:
            continue
        value = model.NewIntVar(0, problem.horizon_seconds, f"tardy_{order_id}")
        model.Add(value >= ends[final] - max(0, due))
        tardiness.append(value)
    max_end = model.NewIntVar(0, problem.horizon_seconds, "local_max_end")
    model.AddMaxEquality(max_end, list(ends.values()))
    model.Minimize((problem.horizon_seconds + 1) * sum(tardiness) + max_end)
    proto = model.Proto()
    size = {"operations": len(keys), "movable_operations": len(keys),
            "modes": sum(len(v) for v in modes_by_key.values()),
            "variables": len(proto.variables), "constraints": len(proto.constraints),
            "optional_intervals": optional, "fixed_boundary_intervals": fixed_boundary,
            "machine_conflict_constraints": conflict_constraints,
            "build_time_seconds": time.perf_counter()-started,
            "rss_increase_mb": _rss_mb()-rss_before}
    return {"model": model, "literals": literals, "starts": starts, "ends": ends,
            "lines": lines, "counts": counts, "size": size,
            "modes": modes_by_key,
            "routing_assumption_mapping": routing_assumption_mapping,
            "routing_assumptions": bool(routing_assumptions)}


def solve_joint_mode_model(artifacts, *, max_time_seconds=30.0, seed=42):
    solver = cp_model.CpSolver(); solver.parameters.max_time_in_seconds = float(max_time_seconds)
    solver.parameters.num_search_workers = 1; solver.parameters.random_seed = int(seed)
    status = solver.Solve(artifacts["model"])
    selected = {}
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for key, lits in artifacts["literals"].items():
            selected[key] = artifacts["modes"][key][next(i for i,l in enumerate(lits) if solver.Value(l))]
    return {"status": solver.StatusName(status), "status_code": int(status),
            "wall_time_seconds": solver.WallTime(), "deterministic_time": solver.UserTime(),
            "objective": solver.ObjectiveValue(), "best_bound": solver.BestObjectiveBound(),
            "branches": solver.NumBranches(), "conflicts": solver.NumConflicts(),
            "selected": selected}
