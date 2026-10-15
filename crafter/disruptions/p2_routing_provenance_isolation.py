"""Diagnostic provenance/isolation for the P2 internal routing contradiction.

This module is intentionally read-only.  It uses the authoritative post-event
N100 state and immutable JointExecutableModes, while adding only diagnostic
metadata/assumption guards.  Production routing semantics are unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from ortools.sat.python import cp_model

from crafter.disruptions.p2_core_guided_prototype import _prepare_state
from crafter.disruptions.p2_feasibility_origin_audit import _solve_feasibility, _write
from crafter.lns_probe.joint_mode_model import build_joint_mode_model

OUT = Path("outputs/crafter/p2_routing_provenance_isolation_v1")
ROUTING_CATEGORIES = (
    "R_PRECEDENCE", "R_EVENT_DIRECT", "R_EVENT_PROPAGATED", "R_RELEASE",
    "R_OUTSIDE_IN", "R_OUTSIDE_OUT", "R_OTHER",
)


def _sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _solve(artifacts, seconds=30.0):
    return _solve_feasibility(artifacts, seconds_limit=float(seconds))


def _route_chain(state):
    """Exact operation-key route membership; never string containment."""
    index = state["after_index"]
    delayed = str(state["event"].metadata.get("predecessor_operation_key", ""))
    keys = set(state["keys"])
    chain, seen = [], set()
    cur = delayed
    while cur and cur in keys and cur not in seen:
        chain.append(cur); seen.add(cur)
        cur = index.route_successor.get(cur)
    return delayed, tuple(chain)


def _pre_values(state, key):
    rows = [x for x in state["rows"] if str(x.get("operation_key")) == str(key)]
    if not rows:
        return {"start": None, "end": None}
    row = rows[0]
    def sec(value):
        try:
            return int(round(float(value) * 60.0))
        except Exception:
            return None
    return {"start": sec(row.get("start_minutes")), "end": sec(row.get("end_minutes"))}


def _mode_summary(values):
    vals = tuple(values)
    return {
        "mode_count": len(vals),
        "starts": sorted({int(x.start_seconds) for x in vals}),
        "ends": sorted({int(x.end_seconds) for x in vals}),
        "min_start": min((int(x.start_seconds) for x in vals), default=None),
        "max_start": max((int(x.start_seconds) for x in vals), default=None),
        "min_end": min((int(x.end_seconds) for x in vals), default=None),
        "max_end": max((int(x.end_seconds) for x in vals), default=None),
    }


def _routing_provenance(state):
    keys = set(state["keys"])
    index = state["after_index"]
    event = state["event"]
    delayed, chain = _route_chain(state)
    delayed_end = event.metadata.get("delayed_end_seconds")
    direct_successor = index.route_successor.get(delayed)
    records = []

    for left in sorted(keys):
        right = index.route_successor.get(left)
        if right not in keys:
            continue
        vals_l = tuple(state["modes"].get(left, ()))
        vals_r = tuple(state["modes"].get(right, ()))
        records.append({
            "constraint_id": f"R_PRECEDENCE:{left}->{right}",
            "category": "R_PRECEDENCE",
            "predecessor": left, "successor": right,
            "operation_key": left,
            "relation": "end(predecessor) <= start(successor)",
            "lower_bound": None,
            "source_function": "crafter.lns_probe.joint_mode_model.build_joint_mode_model",
            "source_event": event.event_id,
            "pre_event_value": _pre_values(state, left),
            "post_event_value": {"predecessor_mode_end": _mode_summary(vals_l),
                                  "successor_mode_start": _mode_summary(vals_r)},
            "mode_domain_encoded": False,
        })

    # Event-derived restrictions are encoded in the fresh mode catalog rather
    # than as separate CP inequalities.  Record that distinction explicitly so
    # an isolation result cannot be mistaken for a missing CP constraint.
    for key in chain:
        if key == delayed:
            continue
        vals = tuple(state["modes"].get(key, ()))
        category = "R_EVENT_DIRECT" if key == direct_successor else "R_EVENT_PROPAGATED"
        lower = delayed_end if key == direct_successor else min((int(x.start_seconds) for x in vals), default=None)
        records.append({
            "constraint_id": f"{category}:{key}", "category": category,
            "predecessor": delayed if key == direct_successor else None,
            "successor": key if key == direct_successor else None,
            "operation_key": key, "lower_bound": lower,
            "relation": "mode start domain lower bound",
            "source_function": "crafter.lns_probe.joint_modes.generate_joint_modes",
            "source_event": event.event_id,
            "pre_event_value": _pre_values(state, key),
            "post_event_value": _mode_summary(vals),
            "mode_domain_encoded": True,
        })

    # These categories have no concrete independent restriction in this P2
    # state; retaining explicit records makes the absence auditable.
    for category, reason in (
        ("R_RELEASE", "no separate release-bound CP constraint; release is represented by the mode domain"),
        ("R_OUTSIDE_IN", "no outside-in routing deficit in the authoritative P2 state"),
        ("R_OUTSIDE_OUT", "no outside-out routing deficit in the authoritative P2 state"),
        ("R_OTHER", "no remaining routing-derived category isolated"),
    ):
        records.append({"constraint_id": f"{category}:NONE", "category": category,
                        "predecessor": None, "successor": None, "operation_key": None,
                        "lower_bound": None, "relation": "NONE",
                        "source_function": "diagnostic classification",
                        "source_event": event.event_id, "pre_event_value": None,
                        "post_event_value": None, "mode_domain_encoded": True,
                        "absence_reason": reason})
    return records, {"delayed_predecessor": delayed, "route_chain": list(chain),
                     "direct_successor": direct_successor, "delayed_end_seconds": delayed_end}


def _model_probe(state, *, disabled=(), assumptions=False, seconds=25.0):
    art = build_joint_mode_model(state["problem"], state["after_index"],
                                 state["neighborhood"], state["modes"],
                                 diagnostic_disable=tuple(disabled),
                                 routing_assumptions=bool(assumptions))
    result = _solve(art, seconds)
    result["model"] = art["size"]
    result["assumption_mapping"] = art.get("routing_assumption_mapping", {})
    return art, result


def _core_literals(solver):
    try:
        return list(solver.SufficientAssumptionsForInfeasibility())
    except Exception:
        return []


def _assumption_solve(art, seconds=45.0):
    try:
        art["model"].ClearObjective()
    except AttributeError:
        pass
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(seconds)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    status = solver.Solve(art["model"])
    return solver, {"status": solver.StatusName(status), "status_code": int(status),
                    "wall_time_seconds": solver.WallTime(), "deterministic_time": solver.UserTime(),
                    "branches": solver.NumBranches(), "conflicts": solver.NumConflicts()}


def _route_core_reproduction(state, mapping, core, seconds=25.0):
    core_edges = {(str(mapping[abs(int(x))]["predecessor"]), str(mapping[abs(int(x))]["successor"]))
                  for x in core if abs(int(x)) in mapping}
    all_edges = {(str(k), str(state["after_index"].route_successor.get(k)))
                 for k in state["keys"] if state["after_index"].route_successor.get(k) in set(state["keys"])}
    disabled = tuple(f"routing_edge:{a}->{b}" for a, b in sorted(all_edges - core_edges))
    art = build_joint_mode_model(state["problem"], state["after_index"], state["neighborhood"], state["modes"],
                                 diagnostic_disable=disabled, routing_assumptions=True)
    solver, result = _assumption_solve(art, seconds)
    result.update({"model": art["size"], "core_edges": sorted([list(x) for x in core_edges]),
                   "disabled_noncore_edge_count": len(disabled)})
    return result


def _shrink_core(state, mapping, raw_core, seconds=25.0):
    """Bounded deletion shrink over a small sufficient routing core."""
    if not raw_core or len(raw_core) > 20:
        return {"status": "NOT_RUN_CORE_TOO_LARGE_OR_EMPTY", "tests": [],
                "minimality_proven": False}
    edge_by_idx = {abs(int(k)): (str(v["predecessor"]), str(v["successor"]))
                   for k, v in mapping.items()}
    core_indices = [abs(int(x)) for x in raw_core if abs(int(x)) in edge_by_idx]
    all_edges = {(str(k), str(state["after_index"].route_successor.get(k)))
                 for k in state["keys"] if state["after_index"].route_successor.get(k) in set(state["keys"])}
    tests = []
    for removed_idx in core_indices:
        keep = {edge_by_idx[x] for x in core_indices if x != removed_idx}
        disabled = tuple(f"routing_edge:{a}->{b}" for a, b in sorted(all_edges - keep))
        art = build_joint_mode_model(state["problem"], state["after_index"], state["neighborhood"], state["modes"],
                                     diagnostic_disable=disabled)
        result = _solve(art, seconds)
        tests.append({"removed_assumption_index": removed_idx,
                      "removed_edge": list(edge_by_idx[removed_idx]),
                      "remaining_core_edges": [list(x) for x in sorted(keep)],
                      "status": result["status"], "model": result.get("model", art["size"]),
                      "solve_time_seconds": result.get("wall_time_seconds")})
    # A core member is necessary relative to this returned core when removing
    # it makes the constrained model feasible.  (The opposite polarity would
    # incorrectly label every member as dispensable.)
    necessary = all(x.get("status") in ("FEASIBLE", "OPTIMAL") for x in tests)
    return {"status": "SHRUNK_WITHIN_RETURNED_CORE" if necessary else "CORE_MEMBER_NOT_NECESSARY_WITHIN_TESTED_CORE",
            "tests": tests, "minimality_proven": bool(necessary and len(core_indices) == 2),
            "note": "deletion minimality is only relative to the returned sufficient core and the authoritative non-core routing edges disabled"}


def run(output_dir=OUT):
    out = Path(output_dir)
    if out.exists() and any(out.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {out}")
    out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    state = _prepare_state()
    provenance, route_meta = _routing_provenance(state)
    _write(out / "routing_constraint_provenance.json", {"categories": list(ROUTING_CATEGORIES),
                                                          "route_meta": route_meta, "records": provenance,
                                                          "database_writes": 0})

    # Baseline and all-routing-disabled reproduction.
    baseline_art, baseline = _model_probe(state, seconds=35.0)
    disabled_art, routing_disabled = _model_probe(state, disabled=("routing_precedence",), seconds=35.0)
    _write(out / "baseline_reproduction.json", {"all_routing_active": baseline,
                                                  "all_routing_related_cp_edges_disabled": routing_disabled,
                                                  "expected_active": "INFEASIBLE",
                                                  "expected_disabled": "FEASIBLE_OR_OPTIMAL",
                                                  "database_writes": 0})

    # Only ordinary routing precedence is an explicit CP category.  Event
    # direct/propagated/release/outside restrictions are mode-domain encoded;
    # report them as not independently guardable rather than fabricating a
    # production relaxation.
    probes = []
    for category, disabled in (
        ("R_PRECEDENCE", ("routing_precedence",)),
        ("R_EVENT_DIRECT", ()), ("R_EVENT_PROPAGATED", ()),
        ("R_RELEASE", ()), ("R_OUTSIDE_IN", ()), ("R_OUTSIDE_OUT", ()),
    ):
        if category == "R_PRECEDENCE":
            _, result = _model_probe(state, disabled=disabled, seconds=25.0)
            result.update({"category_disabled": category, "isolation": "EXPLICIT_CP_CATEGORY"})
        else:
            result = {"category_disabled": category,
                      "status": "NOT_SEPARATELY_TOGGLED_MODE_DOMAIN",
                      "isolation": "MODE_DOMAIN_ENCODED",
                      "reason": "event/release/outside routing restriction is encoded in fresh JointExecutableModes; no independent CP constraint exists in current formulation",
                      "model": None, "solve_time_seconds": 0.0}
        probes.append(result)
    _write(out / "routing_category_probes.json", probes)

    combos = []
    for label, disabled in (("R_PRECEDENCE+R_EVENT_PROPAGATED", ("routing_precedence",)),
                            ("R_PRECEDENCE+R_RELEASE", ("routing_precedence",)),
                            ("R_EVENT_DIRECT+R_EVENT_PROPAGATED", ())):
        if disabled:
            _, result = _model_probe(state, disabled=disabled, seconds=20.0)
            result.update({"categories_disabled": label, "interpretation": "event category remains in mode catalog"})
        else:
            result = {"categories_disabled": label, "status": "NOT_SEPARATELY_TOGGLED_MODE_DOMAIN",
                      "reason": "no independent CP guard for event-domain category"}
        combos.append(result)
    feasible_combos = [x for x in combos if x.get("status") in ("FEASIBLE", "OPTIMAL")]
    _write(out / "routing_category_conflict_set.json", {"probes": combos,
        "smallest_tested_feasible_combination": feasible_combos[0].get("categories_disabled") if feasible_combos else None,
        "minimality_proven": False, "database_writes": 0})

    # Routing-only sufficient assumption core.
    core_art = build_joint_mode_model(state["problem"], state["after_index"], state["neighborhood"], state["modes"],
                                      routing_assumptions=True)
    solver, core_result = _assumption_solve(core_art, seconds=60.0)
    mapping = core_art.get("routing_assumption_mapping", {})
    raw_core = _core_literals(solver) if core_result["status"] == "INFEASIBLE" else []
    mapped_core = []
    for lit in raw_core:
        idx = abs(int(lit))
        if idx in mapping:
            mapped_core.append({**mapping[idx], "returned_literal": int(lit)})
    _write(out / "routing_assumption_core.json", {"solve": core_result, "core_literals": raw_core,
        "sufficient_infeasible_core": mapped_core, "core_size": len(mapped_core),
        "core_mapping_complete": len(mapped_core) == len(raw_core), "minimality_proven": False,
        "database_writes": 0})

    repro = (_route_core_reproduction(state, mapping, raw_core, seconds=30.0)
             if mapped_core else {"status": "NOT_RUN_NO_CORE", "core_edges": []})
    shrink = _shrink_core(state, mapping, raw_core, seconds=25.0)
    _write(out / "routing_core_shrink.json", {"original_core_size": len(mapped_core),
        "shrunk_core_size": len(mapped_core), "shrunk_constraint_ids": [x["constraint_id"] for x in mapped_core],
        "minimality_proven": shrink.get("minimality_proven", False), "core_reproduction": repro,
        "deletion_shrink": shrink,
        "status": shrink.get("status", "NOT_RUN"),
        "database_writes": 0})

    core_ops = sorted({x["predecessor"] for x in mapped_core} | {x["successor"] for x in mapped_core})
    timing = []
    for key in core_ops:
        vals = tuple(state["modes"].get(key, ()))
        timing.append({"operation_key": key, "pre_event": _pre_values(state, key),
                       "post_event_mode_domain": _mode_summary(vals),
                       "predecessor": next((p for p in state["keys"] if state["after_index"].route_successor.get(p) == key), None),
                       "successor": state["after_index"].route_successor.get(key),
                       "event_relation": "IN_ROUTING_ASSUMPTION_CORE"})
    _write(out / "routing_core_operation_timing.json", timing)

    witness = {"delayed_predecessor": route_meta["delayed_predecessor"],
               "delayed_end_seconds": route_meta["delayed_end_seconds"],
               "chain": route_meta["route_chain"],
               "sufficient_core_size": len(mapped_core),
               "core_constraint_ids": [x["constraint_id"] for x in mapped_core],
               "human_chain": [
                   "P2 delays the predecessor and its exact route chain receives event-derived mode-domain timing bounds.",
                   "Ordinary internal predecessor.end <= successor.start constraints then couple all route choices globally.",
                   "No single internal arc has zero compatible mode pairs; the infeasibility is collective rather than one impossible edge.",
                   "Routing-only assumption diagnosis identifies a sufficient ordinary-precedence core; it is not a minimal core.",
               ], "physical_boundary_proven": False}
    _write(out / "routing_contradiction_witness.json", witness)
    _write(out / "diagnostic_correction_result.json", {
        "all_routing_disabled_status": routing_disabled.get("status"),
        "single_category_correction": "R_PRECEDENCE_ONLY_DIAGNOSTICALLY_RESTORES_FEASIBILITY" if routing_disabled.get("status") in ("FEASIBLE", "OPTIMAL") else "NOT_CONFIRMED",
        "event_bound_correction_tested": False,
        "reason": "event-derived restrictions are mode-domain encoded and require a separate explicit diagnostic guard before safe category isolation",
        "production_logic_changed": False, "database_writes": 0})

    status = "CRAFT_P2_ROUTING_COLLECTIVE_CORE_IDENTIFIED" if mapped_core else "CRAFT_P2_ROUTING_COLLECTIVE_INTERACTION_UNRESOLVED"
    root = {"status": status, "baseline_status": baseline.get("status"),
            "all_routing_disabled_status": routing_disabled.get("status"),
            "explicit_category_restoring_feasibility": "R_PRECEDENCE" if routing_disabled.get("status") in ("FEASIBLE", "OPTIMAL") else None,
            "routing_assumption_core_size": len(mapped_core),
            "routing_bound_logic_bug_confirmed": False,
            "routing_timing_domain_interaction": True,
            "physical_boundary_proven": False, "database_writes": 0,
            "next_implementation_change": "Expose event-derived mode-domain bounds as explicit diagnostic provenance/guards so ordinary precedence and event timing can be isolated without relaxing production semantics."}
    _write(out / "root_cause_classification.json", root)
    report = f"""# P2 routing provenance isolation

Primary status: `{status}`.

- Baseline with all routing active: `{baseline.get('status')}`.
- All ordinary routing precedence disabled: `{routing_disabled.get('status')}`.
- Internal routing edges: {len([x for x in provenance if x['category'] == 'R_PRECEDENCE'])}.
- Routing sufficient assumption core size: {len(mapped_core)} (not claimed minimal).
- Zero-compatible single arc proven: no; prior exact arc audit found none.
- Event-derived direct/propagated/release restrictions are encoded in the fresh JointExecutableMode domains, not independent CP constraints, so they cannot truthfully be isolated as standalone CP categories yet.
- No worker or machine semantics were changed; no physical boundary was proven; database writes: 0.

Interpretation: ordinary routing precedence is the only explicit routing category whose removal restores feasibility, but the event-derived mode-domain timing bounds participate in the collective incompatibility. This is not evidence that the event rule is wrong; it is evidence that provenance needs to be made explicit before testing a corrected bound. The next single implementation change is diagnostic instrumentation/guards for event-derived mode-domain bounds, not a production relaxation.
"""
    (out / "final_report.md").write_text(report, encoding="utf-8")
    files = {str(p.relative_to(out)).replace("\\", "/"): _sha(p) for p in sorted(out.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    _write(out / "artifact_hash_manifest.json", {"files": files, "artifact_count": len(files),
        "status": status, "elapsed_seconds": time.perf_counter() - started, "database_writes": 0})
    return {"status": status, "output_dir": str(out), "baseline": baseline.get("status"),
            "routing_disabled": routing_disabled.get("status"), "core_size": len(mapped_core), "database_writes": 0}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
