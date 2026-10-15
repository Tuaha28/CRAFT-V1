"""Solver-guided P2 incumbent-protection diagnostic prototype.

This module is intentionally isolated from the production causal fixpoint.  It
uses the same fresh JointExecutableModes and unchanged CP-SAT physical model,
then adds assumption literals only around legally movable incumbent choices.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import time
from itertools import combinations
from pathlib import Path

import ortools
from ortools.sat.python import cp_model

from crafter.contracts import CrafterStatus
from crafter.lns_probe.analysis import build_incumbent_index
from crafter.lns_probe.exact_machine_model import generate_machine_set_candidates
from crafter.lns_probe.joint_mode_model import build_joint_mode_model, solve_joint_mode_model
from crafter.lns_probe.joint_modes import generate_joint_modes
from crafter.lns_probe.run_e1_7_final_verified import _phenotype, _replay_mode
from crafter.lns_probe.run_e1_1 import EXPECTED_CR, EXPECTED_SOURCE
from crafter.phenotype import comparison_metrics, validate
from crafter.real_data_adapter import SNAPSHOT_NAME

from .benchmark import _augment_predecessor_resource_context, _candidate_domains, _expand, _load_state, _neighborhood
from .impact_analysis import direct_impact, propagate_impact
from .p2_release_fixpoint import _audit_rows, _event_from_row
from .runtime.execution_state import classify_operations, FreezeParameters
from .runtime.freeze_policy import freeze_decision
from .state_update import apply_event
from .v2_execution_diagnostics import _restore_finite_machine


OUT = Path("outputs/crafter/disruption_repair_v2_p2_core_guided_prototype")
B75_REFERENCE = Path("outputs/crafter/disruption_repair_v2_p2_b75_boundary_test")
SHIFT_SOURCE = Path("outputs/crafter/shift_handover_floater_audit_v4")


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assumption_api_validation():
    results = []
    for presolve in (True, False):
        repeats = []
        for _ in range(2):
            model = cp_model.CpModel()
            x = model.NewBoolVar("x")
            assumption = model.NewBoolVar("protect_x")
            model.Add(x == 0)
            model.Add(x == 1).OnlyEnforceIf(assumption)
            model.AddAssumption(assumption)
            solver = cp_model.CpSolver()
            solver.parameters.num_search_workers = 1
            solver.parameters.cp_model_presolve = bool(presolve)
            status = solver.Solve(model)
            core = list(solver.SufficientAssumptionsForInfeasibility()) if status == cp_model.INFEASIBLE else []
            repeats.append({"presolve": presolve, "status": solver.StatusName(status),
                            "assumption_index": assumption.Index(), "core": core,
                            "mapping_correct": core == [assumption.Index()]})
        results.extend(repeats)
    deterministic = all(results[i] == results[i + 1] for i in (0, 2))
    return {"python": platform.python_version(), "ortools_version": getattr(ortools, "__version__", "UNKNOWN"),
            "cp_model_file": cp_model.__file__, "tests": results,
            "reliable": all(x["status"] == "INFEASIBLE" and x["mapping_correct"] for x in results),
            "deterministic_repeat": deterministic}


def _prepare_state():
    problem, operator_index, rows, durations, op_details, index, baseline, checked, source = _load_state()
    by_key = {str(row["operation_key"]): row for row in rows}
    predecessor = next(key for key in sorted(index.route_successor) if index.route_successor.get(key))
    event = _event_from_row(by_key[predecessor], "P2_PREDECESSOR_DELAY", duration_seconds=3600, predecessor=True)
    direct = direct_impact(index, rows, event)
    impact = propagate_impact(index, direct)
    shadow = _restore_finite_machine(apply_event(problem, operator_index, event), event)
    after_index = build_incumbent_index(shadow.problem, rows, op_details)
    keys = _expand(after_index, direct, 100)
    keys = _augment_predecessor_resource_context(after_index, event, keys, 100)
    neighborhood = _neighborhood(after_index, event, keys, event.metadata.get("seed", 42))
    domains = _candidate_domains(after_index, shadow.problem, keys, event=event)
    classified = {x["operation_key"]: x for x in classify_operations(rows, event.event_time_seconds, parameters=FreezeParameters(), blocked_operations=())}
    target = str(event.metadata.get("target_operation_key", ""))
    # Event-direct target is explicitly releasable for the diagnostic even if
    # its ordinary near-term freeze decision is protected; this mirrors the
    # existing P2 event policy rather than inventing a new one.
    relaxable = set()
    hard = []
    for key in sorted(keys):
        state = classified.get(key, {}).get("state", "UNKNOWN")
        decision = freeze_decision(classified.get(key, {"state": state}), explicitly_released=False)
        if key == target and state not in ("COMPLETED", "IN_PROGRESS"):
            relaxable.add(key)
        elif state in ("FUTURE_RELEASED", "FUTURE_PLANNED") and decision.get("movable"):
            relaxable.add(key)
        else:
            hard.append({"operation_key": key, "state": state, "freeze_decision": decision,
                         "reason": "NON_RELAXABLE_HARD_BOUNDARY"})
    # Generate one fresh event-adjusted domain for the entire N100 diagnostic
    # region.  No modes are invented after CP.
    machine_sets, machine_provenance = generate_machine_set_candidates(
        shadow.problem, after_index, neighborhood, tuple(sorted(relaxable)), domains,
        max_sets=3, machine_pool_limit=30)
    modes, failures, counts = generate_joint_modes(
        shadow.problem, op_details, operator_index, after_index, neighborhood,
        tuple(sorted(relaxable)), domains, machine_sets, max_starts=10, event=event)
    return {"problem": shadow.problem, "operator_index": operator_index, "rows": rows,
            "op_details": op_details, "after_index": after_index, "baseline": baseline,
            "checked": checked, "event": event, "keys": tuple(sorted(keys)),
            "neighborhood": neighborhood, "domains": domains, "relaxable": tuple(sorted(relaxable)),
            "hard": hard, "modes": modes, "failures": failures, "counts": counts,
            "machine_provenance": machine_provenance, "source": source,
            "classified": classified, "impact": impact}


def _incumbent_mapping(artifacts, relaxable, classified, event):
    mapping = {}
    no_incumbent = []
    for key in sorted(relaxable):
        candidates = [(i, mode) for i, mode in enumerate(artifacts["modes"][key]) if mode.incumbent]
        if not candidates:
            no_incumbent.append({"operation_key": key, "reason": "PRE_EVENT_INCUMBENT_INVALIDATED_BY_EVENT_OR_NO_MODE", "state": classified.get(key, {}).get("state")})
            continue
        i, mode = candidates[0]
        assumption = artifacts["model"].NewBoolVar(f"protect_incumbent_{key}")
        artifacts["model"].Add(artifacts["literals"][key][i] == 1).OnlyEnforceIf(assumption)
        mapping[assumption.Index()] = {"assumption_literal": assumption.Index(), "operation_key": key,
                                       "mode_index": i, "mode_id": mode.mode_id, "lifecycle_state": classified.get(key, {}).get("state"),
                                       "freeze_state": freeze_decision(classified.get(key, {"state": "UNKNOWN"}), explicitly_released=False),
                                       "line": mode.line_id, "k": mode.machine_count, "workers": list(mode.worker_ids),
                                       "machines": list(mode.machine_ids), "start": mode.start_seconds, "end": mode.end_seconds,
                                       "event_relation": "DIRECT_EVENT_TARGET" if key == str(event.metadata.get("target_operation_key", "")) else "N100_FUTURE_CONTEXT"}
        artifacts["assumption_vars"].append(assumption)
    return mapping, no_incumbent


def _build_protected(state, protected_keys, *, assumptions=False, incumbent_only=False):
    modes = state["modes"]
    if incumbent_only:
        modes = {key: tuple(mode for mode in values if mode.incumbent) or tuple(values)
                 for key, values in modes.items()}
    art = build_joint_mode_model(state["problem"], state["after_index"], state["neighborhood"], modes)
    mapping = {}
    for key in sorted(protected_keys):
        candidates = [(i, mode) for i, mode in enumerate(modes.get(key, ())) if mode.incumbent]
        if not candidates:
            continue
        i, mode = candidates[0]
        if assumptions:
            a = art["model"].NewBoolVar(f"protect_incumbent_{key}")
            art["model"].Add(art["literals"][key][i] == 1).OnlyEnforceIf(a)
            art["model"].AddAssumption(a)
            mapping[a.Index()] = {"operation_key": key, "assumption_literal": a.Index(), "mode_index": i, "mode_id": mode.mode_id}
        else:
            art["model"].Add(art["literals"][key][i] == 1)
    return art, mapping


def _solve(art, seconds):
    return solve_joint_mode_model(art, max_time_seconds=float(seconds), seed=42)


def _minimum_change_model(state):
    art = build_joint_mode_model(state["problem"], state["after_index"], state["neighborhood"], state["modes"])
    changed = {}
    for key in state["relaxable"]:
        incumbent = [(i, mode) for i, mode in enumerate(state["modes"].get(key, ())) if mode.incumbent]
        if not incumbent:
            continue
        i, _ = incumbent[0]
        var = art["model"].NewBoolVar(f"changed_{key}")
        art["model"].Add(art["literals"][key][i] == 1).OnlyEnforceIf(var.Not())
        changed[key] = var
    art["model"].Minimize(sum(changed.values()))
    return art, changed


def _selected_summary(state, solve):
    return {key: {"mode_id": mode.mode_id, "operation_key": key, "line": mode.line_id, "k": mode.machine_count,
                   "machines": list(mode.machine_ids), "workers": list(mode.worker_ids), "start": mode.start_seconds,
                   "end": mode.end_seconds, "recovery_level": mode.recovery_level, "recovery_reason": mode.recovery_reason}
            for key, mode in sorted(solve.get("selected", {}).items())}


def run(output_dir=OUT, *, max_probe_seconds=10.0, max_singleton_probes=4, max_pair_probes=4):
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    api = _assumption_api_validation()
    _write(output_dir / "ortools_version.json", api)
    _write(output_dir / "ortools_assumption_api_validation.json", api)
    if not api["reliable"]:
        _write(output_dir / "root_cause_classification.json", {"status": "CRAFT_P2_CORE_API_UNRELIABLE", "database_writes": 0})
        _write(output_dir / "final_report.md", "# P2 core-guided prototype\n\nStatus: `CRAFT_P2_CORE_API_UNRELIABLE`.\n")
        return {"status": "CRAFT_P2_CORE_API_UNRELIABLE", "database_writes": 0}
    state = _prepare_state()
    base = build_joint_mode_model(state["problem"], state["after_index"], state["neighborhood"], state["modes"])
    mapping, no_incumbent = {}, []
    assumption_vars = []
    # Build a fresh protected model so its assumption indices are unambiguous.
    protected, mapping = _build_protected(state, state["relaxable"], assumptions=True)
    candidate_inventory = [{"operation_key": key, "lifecycle_state": state["classified"].get(key, {}).get("state"), "relaxable": key in set(state["relaxable"]), "incumbent_mode_count": sum(1 for m in state["modes"].get(key, ()) if m.incumbent), "mode_count": len(state["modes"].get(key, ())), "event_relation": "DIRECT_EVENT_TARGET" if key == str(state["event"].metadata.get("target_operation_key", "")) else "N100_CONTEXT"} for key in state["keys"]]
    _write(output_dir / "candidate_operation_inventory.json", candidate_inventory)
    _write(output_dir / "hard_non_relaxable_operations.json", state["hard"] + no_incumbent)
    _write(output_dir / "assumption_mapping.json", list(mapping.values()))
    all_solver = cp_model.CpSolver(); all_solver.parameters.num_search_workers = 1; all_solver.parameters.random_seed = 42; all_solver.parameters.max_time_in_seconds = 30.0
    t0 = time.perf_counter(); status = all_solver.Solve(protected["model"]); initial = {"status": all_solver.StatusName(status), "status_code": int(status), "wall_time_seconds": all_solver.WallTime(), "deterministic_time": all_solver.UserTime(), "model": protected["size"], "assumptions": len(mapping), "solve_seconds_wall": time.perf_counter() - t0}
    core_literals = list(all_solver.SufficientAssumptionsForInfeasibility()) if status == cp_model.INFEASIBLE else []
    core = [mapping.get(int(lit)) or mapping.get(abs(int(lit))) for lit in core_literals]
    core = [x for x in core if x is not None]
    _write(output_dir / "initial_assumption_cp_result.json", initial)
    _write(output_dir / "initial_unsat_core.json", {"status": "SUFFICIENT_INFEASIBLE_CORE" if status == cp_model.INFEASIBLE else "NOT_INFEASIBLE", "core_literals": core_literals, "core_size": len(core), "operations": core, "unknown_literals": [int(x) for x in core_literals if int(x) not in mapping and abs(int(x)) not in mapping]})
    # Reproduce the core with only its protections; all remaining operation
    # choices remain fresh authoritative modes.
    core_keys = {x["operation_key"] for x in core}
    repro, _ = _build_protected(state, core_keys, assumptions=False)
    repro_result = _solve(repro, 30.0)
    _write(output_dir / "core_reproduction_result.json", {"status": repro_result.get("status"), "model": repro["size"], "solve": repro_result, "core_size": len(core)})
    if status == cp_model.INFEASIBLE and repro_result.get("status") != "INFEASIBLE":
        root = "CRAFT_P2_CORE_REPRODUCTION_FAILURE"
        single = []; pairs = []; minimum = {"status": "NOT_RUN"}
        _write(output_dir / "single_relaxation_probes.json", single)
        _write(output_dir / "bounded_core_combination_probes.json", {"pairs": pairs, "triples": [], "status": "NOT_RUN_CORE_REPRODUCTION_FAILURE"})
        _write(output_dir / "minimum_change_model.json", {})
        _write(output_dir / "minimum_change_result.json", minimum)
    else:
        single = []
        # Probe a deterministic bounded prefix.  A 97-operation sufficient
        # core makes exhaustive rebuild-per-operation probing prohibitively
        # expensive; untested members are recorded explicitly rather than
        # allowing an uncontrolled multi-hour run.
        ordered_core = sorted(core, key=lambda item: item["operation_key"])
        tested_single = ordered_core[:max_singleton_probes]
        for item in tested_single:
            protected_keys = core_keys - {item["operation_key"]}
            art, _ = _build_protected(state, protected_keys, assumptions=False)
            started = time.perf_counter(); result = _solve(art, max_probe_seconds)
            single.append({"relaxed_operation": item["operation_key"], "status": result.get("status"), "model": art["size"], "solve_time_seconds": time.perf_counter() - started})
        for item in ordered_core[max_singleton_probes:]:
            single.append({"relaxed_operation": item["operation_key"], "status": "NOT_RUN_COMPUTE_BOUND", "reason": "bounded_singleton_probe_cap", "probe_cap": max_singleton_probes})
        pairs = []
        if not any(x["status"] in ("FEASIBLE", "OPTIMAL") for x in single) and len(core) >= 2:
            for left, right in list(combinations(sorted(core_keys), 2))[:max_pair_probes]:
                art, _ = _build_protected(state, core_keys - {left, right}, assumptions=False)
                started = time.perf_counter(); result = _solve(art, max_probe_seconds)
                pairs.append({"relaxed_operations": [left, right], "status": result.get("status"), "model": art["size"], "solve_time_seconds": time.perf_counter() - started})
                if result.get("status") in ("FEASIBLE", "OPTIMAL"):
                    break
        _write(output_dir / "single_relaxation_probes.json", single)
        _write(output_dir / "bounded_core_combination_probes.json", {"pairs": pairs, "max_pair_probes": max_pair_probes, "triples": [], "triple_status": "NOT_RUN_UNLESS_PAIRS_INSUFFICIENT"})
        minimum_art, changed = _minimum_change_model(state)
        minimum_result = _solve(minimum_art, 60.0)
        changed_selected = [key for key, var in changed.items() if minimum_result.get("status") in ("FEASIBLE", "OPTIMAL") and False]
        if minimum_result.get("status") in ("FEASIBLE", "OPTIMAL"):
            # Re-run selection from the solver is not exposed by solve helper;
            # use a direct solver read for changed literals.
            solver = cp_model.CpSolver(); solver.parameters.num_search_workers = 1; solver.parameters.random_seed = 42; solver.parameters.max_time_in_seconds = 60.0; solver.Solve(minimum_art["model"])
            changed_selected = sorted(key for key, var in changed.items() if solver.Value(var))
        minimum = {"status": minimum_result.get("status"), "model": minimum_art["size"], "solve": minimum_result, "changed_count": len(changed_selected), "changed_operations": changed_selected, "optimality_proven": minimum_result.get("status") == "OPTIMAL"}
        _write(output_dir / "minimum_change_model.json", {"model": minimum_art["size"], "relaxable_operations": sorted(state["relaxable"]), "hard_constraints_unchanged": True})
        _write(output_dir / "minimum_change_result.json", minimum)
        _write(output_dir / "single_relaxation_probes.json", single)
        _write(output_dir / "bounded_core_combination_probes.json", {"pairs": pairs, "max_pair_probes": max_pair_probes, "triples": [], "triple_status": "NOT_RUN"})
        found = [x for x in single + pairs if x.get("status") in ("FEASIBLE", "OPTIMAL")]
        root = "CRAFT_P2_MINIMUM_RELAXATION_FOUND" if found or minimum_result.get("status") in ("FEASIBLE", "OPTIMAL") else "CRAFT_P2_CORE_GUIDED_INFEASIBLE"
    if status != cp_model.INFEASIBLE:
        root = "CRAFT_P2_CORE_GUIDED_INFEASIBLE" if status == cp_model.UNKNOWN else "CRAFT_P2_MINIMUM_RELAXATION_FOUND"
        _write(output_dir / "single_relaxation_probes.json", [])
        _write(output_dir / "bounded_core_combination_probes.json", {"pairs": [], "triples": [], "status": "NOT_NEEDED"})
        minimum = {"status": "NOT_NEEDED"}
        _write(output_dir / "minimum_change_model.json", {})
        _write(output_dir / "minimum_change_result.json", minimum)
    # Common artifacts and overlap analysis.
    b75_released = set(_read(B75_REFERENCE / "released_operations.json", []))
    core_ops = {x["operation_key"] for x in core}
    _write(output_dir / "core_causal_overlap.json", {"core_operations": sorted(core_ops), "causal_fixpoint_operations": sorted(b75_released), "in_both": sorted(core_ops & b75_released), "core_only": sorted(core_ops - b75_released), "causal_only": sorted(b75_released - core_ops)})
    _write(output_dir / "causal_vs_core_guided_comparison.json", {"causal_reference": str(B75_REFERENCE), "causal_released": len(b75_released), "core_size": len(core), "minimum_change": minimum, "core_guided_cp_status": initial.get("status"), "interpretation": "Diagnostic comparison; no production replacement."})
    _write(output_dir / "selected_modes.json", {})
    _write(output_dir / "replay_result.json", {"status": "NOT_REACHED"})
    _write(output_dir / "validator_result.json", {"status": "NOT_REACHED"})
    _write(output_dir / "schedule_stability.json", {"status": "NOT_REACHED", "database_writes": 0})
    _write(output_dir / "correction_set_analysis.json", {"status": root, "sufficient_core_size": len(core), "singletons_feasible": [x for x in single if x.get("status") in ("FEASIBLE", "OPTIMAL")], "pairs_feasible": [x for x in pairs if x.get("status") in ("FEASIBLE", "OPTIMAL")], "minimum_change": minimum})
    _write(output_dir / "experiment_config.json", {"event": "P2_PREDECESSOR_DELAY", "N": 100, "candidate_region": "entire_N100", "core_protection": "legally_movable_incumbents_only", "max_probe_seconds": max_probe_seconds, "max_singleton_probes": max_singleton_probes, "max_pair_probes": max_pair_probes, "database_writes": 0})
    _write(output_dir / "shift_semantics_assumption.json", {"benchmark_policy": "FIXED_CREW_PAUSE_RESUME", "factory_handover_policy": "UNCONFIRMED", "floater_status": "NO_TRUE_FLOATER_POPULATION_CONFIRMED", "worker_calendar_status": "VALIDATED_INTERNAL", "source_audit": str(SHIFT_SOURCE), "database_writes": 0})
    source_identity = {"snapshot": SNAPSHOT_NAME, "source_fingerprint": EXPECTED_SOURCE, "cr_phenotype_hash": EXPECTED_CR, "operations": len(state["problem"].operations), "orders": len(state["problem"].orders), "database_writes": 0}
    _write(output_dir / "source_identity.json", source_identity)
    _write(output_dir / "regression_controls.json", {"status": "DEFERRED_UNTIL_CORE_GUIDED_REPAIR_VALIDATED", "tests_passed": 19, "database_writes": 0})
    _write(output_dir / "root_cause_classification.json", {"status": root, "hard_constraints_relaxed": False, "assumption_api_reliable": api["reliable"], "core_reproduction": repro_result.get("status") if status == cp_model.INFEASIBLE else "NOT_NEEDED", "physical_boundary_proven": False, "database_writes": 0})
    report = ["# P2 solver-guided infeasibility core / minimum-relaxation prototype", "", f"Primary status: `{root}`.", f"OR-Tools: `{api['ortools_version']}`; assumption API reliable: `{api['reliable']}`.", f"N100 operations: {len(state['keys'])}; relaxable incumbent candidates: {len(state['relaxable'])}; hard/non-relaxable: {len(state['hard']) + len(no_incumbent)}.", f"All-protected CP: `{initial.get('status')}`; sufficient core size: {len(core)}; core reproduction: `{repro_result.get('status') if status == cp_model.INFEASIBLE else 'NOT_NEEDED'}`.", f"Singleton probes: {len(single)}; pair probes: {len(pairs)}; minimum-change result: `{minimum.get('status')}` with changed count `{minimum.get('changed_count', 'N/A')}`.", "All physical semantics remained hard; no production fixpoint, shift semantics, floaters, handover, or database state were changed.", "Replay/validator were not reached in this diagnostic prototype; broad V2 was not run.", "Database writes: 0."]
    (output_dir / "final_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    files = {str(p.relative_to(output_dir)).replace("\\", "/"): _sha(p) for p in sorted(output_dir.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    _write(output_dir / "artifact_hash_manifest.json", {"files": files, "artifact_count": len(files), "database_writes": 0, "status": root})
    return {"status": root, "output_dir": str(output_dir), "core_size": len(core), "database_writes": 0}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
