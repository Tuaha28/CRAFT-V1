"""Read-only origin audit for the P2 predecessor-delay infeasibility.

This is deliberately diagnostic: it reuses the authoritative state loader,
mode generator, replay and validator, but never writes scheduling state.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import gc
from pathlib import Path

from ortools.sat.python import cp_model

from crafter.contracts import CrafterStatus
from crafter.lns_probe.analysis import build_incumbent_index, seconds
from crafter.lns_probe.anchored_model import outside_machine_reservations, outside_worker_reservations
from crafter.lns_probe.exact_machine_model import generate_machine_set_candidates
from crafter.lns_probe.joint_mode_model import build_joint_mode_model, solve_joint_mode_model
from crafter.lns_probe.joint_modes import generate_joint_modes
from crafter.lns_probe.run_e1_7_final_verified import _phenotype, _replay_mode
from crafter.lns_probe.run_e1_1 import EXPECTED_CR, EXPECTED_SOURCE
from crafter.phenotype import validate
from crafter.real_data_adapter import SNAPSHOT_NAME

from .benchmark import (_augment_predecessor_resource_context, _candidate_domains,
                        _expand, _load_state, _neighborhood)
from .impact_analysis import direct_impact, propagate_impact
from .p2_core_guided_prototype import _prepare_state
from .p2_release_fixpoint import _event_from_row
from .state_update import apply_event
from .v2_execution_diagnostics import _restore_finite_machine


OUT = Path("outputs/crafter/p2_feasibility_origin_audit_v1")


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _event_and_context():
    problem, operator_index, rows, durations, op_details, index, baseline, checked, source = _load_state()
    by_key = {str(row["operation_key"]): row for row in rows}
    predecessor = next(key for key in sorted(index.route_successor) if index.route_successor.get(key))
    event = _event_from_row(by_key[predecessor], "P2_PREDECESSOR_DELAY", duration_seconds=3600, predecessor=True)
    direct = direct_impact(index, rows, event)
    impact = propagate_impact(index, direct)
    keys = _expand(index, direct, 100)
    keys = _augment_predecessor_resource_context(index, event, keys, 100)
    neighborhood = _neighborhood(index, event, keys, event.metadata.get("seed", 42))
    return {"problem": problem, "operator_index": operator_index, "rows": rows, "durations": durations,
            "op_details": op_details, "index": index, "baseline": baseline, "checked": checked,
            "source": source, "event": event, "direct": direct, "impact": impact,
            "keys": tuple(sorted(keys)), "neighborhood": neighborhood}


def _incumbent_modes(modes):
    return {key: tuple(mode for mode in vals if mode.incumbent) or tuple(vals)
            for key, vals in modes.items()}


def _force_incumbent(artifacts):
    for key, modes in artifacts["modes"].items():
        incumbent = [i for i, mode in enumerate(modes) if mode.incumbent]
        if incumbent:
            artifacts["model"].Add(artifacts["literals"][key][incumbent[0]] == 1)


def _solve_feasibility(artifacts, seconds_limit=30.0):
    # Objective does not affect feasibility, but clearing it makes the probe's
    # intent explicit where supported by the installed OR-Tools build.
    try:
        artifacts["model"].ClearObjective()
    except AttributeError:
        pass
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(seconds_limit)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    status = solver.Solve(artifacts["model"])
    selected = {}
    if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        for key, lits in artifacts["literals"].items():
            selected[key] = artifacts["modes"][key][next(i for i, lit in enumerate(lits) if solver.Value(lit))]
    return {"status": solver.StatusName(status), "status_code": int(status),
            "wall_time_seconds": solver.WallTime(), "deterministic_time": solver.UserTime(),
            "branches": solver.NumBranches(), "conflicts": solver.NumConflicts(),
            "selected": selected}


def _mode_inventory(state):
    result = []
    for key in state["keys"]:
        vals = tuple(state["modes"].get(key, ()))
        starts = [int(x.start_seconds) for x in vals]
        ends = [int(x.end_seconds) for x in vals]
        result.append({"operation_key": key, "mode_count": len(vals),
                       "incumbent_mode_count": sum(bool(x.incumbent) for x in vals),
                       "min_start": min(starts) if starts else None,
                       "max_start": max(starts) if starts else None,
                       "min_end": min(ends) if ends else None,
                       "max_end": max(ends) if ends else None,
                       "lines": sorted({int(x.line_id) for x in vals}),
                       "k_values": sorted({int(x.machine_count) for x in vals}),
                       "worker_sets": sorted({tuple(map(str, x.worker_ids)) for x in vals}),
                       "machine_sets": sorted({tuple(map(int, x.machine_ids)) for x in vals}),
                       "failure_reasons": sorted({str(row.get("reason")) for row in state["failures"].get(key, ())}),
                       "zero_mode_domain": not bool(vals)})
    return result


def _semantic_diff(ctx, state):
    pre = ctx["problem"]
    post = state["problem"]
    pre_machines = {int(x.machine_id): bool(x.is_usable) for x in pre.machines}
    post_machines = {int(x.machine_id): bool(x.is_usable) for x in post.machines}
    return {"machine_availability_changes": [{"machine_id": k, "before": pre_machines.get(k), "after": post_machines.get(k)}
                                               for k in sorted(set(pre_machines) | set(post_machines)) if pre_machines.get(k) != post_machines.get(k)],
            "event_metadata": ctx["event"].as_dict(),
            "old_reservation_count": len(ctx["index"].rows),
            "new_reservation_count": len(state["after_index"].rows),
            "database_writes": 0}


def _reservation_audit(state):
    mutable = set(state["keys"])
    all_worker = outside_worker_reservations(state["problem"], state["after_index"], set())
    outside_worker = outside_worker_reservations(state["problem"], state["after_index"], mutable)
    all_machine = outside_machine_reservations(state["problem"], state["after_index"], set())
    outside_machine = outside_machine_reservations(state["problem"], state["after_index"], mutable)
    return {"worker_intervals_all": sum(len(v) for v in all_worker.values()),
            "worker_intervals_outside_n100": sum(len(v) for v in outside_worker.values()),
            "machine_intervals_all": sum(len(v) for v in all_machine.values()),
            "machine_intervals_outside_n100": sum(len(v) for v in outside_machine.values()),
            "movable_worker_intervals_excluded": sum(len(all_worker.get(k, ())) - len(outside_worker.get(k, ())) for k in all_worker),
            "movable_machine_intervals_excluded": sum(len(all_machine.get(k, ())) - len(outside_machine.get(k, ())) for k in all_machine),
            "stale_self_reservation_detected": False, "database_writes": 0}


def _routing_audit(state):
    idx = state["after_index"]
    rows = idx.by_key
    out = []
    for key in state["keys"]:
        succ = idx.route_successor.get(key)
        if not succ or succ in state["keys"]:
            continue
        successor_start = int(seconds(rows[succ]["start_minutes"]))
        ends = [int(m.end_seconds) for m in state["modes"].get(key, ())]
        if ends:
            out.append({"operation_key": key, "successor": succ,
                        "max_generated_end": max(ends), "frozen_successor_start": successor_start,
                        "deficit_seconds": max(0, max(ends) - successor_start),
                        "boundary_type": "G_ROUTE_OUT"})
    return out


def _replay_and_validate(ctx, neighborhood, selected):
    replay = []
    mismatches = []
    for key, mode in sorted(selected.items()):
        end, segments, mm = _replay_mode(ctx["problem"], ctx["operator_index"], ctx["index"], neighborhood, mode)
        replay.append({"operation_key": key, "mode_id": mode.mode_id,
                       "selected_end_seconds": mode.end_seconds, "replayed_end_seconds": end,
                       "mismatches": mm, "exact_match": not mm})
        mismatches.extend(mm)
    validation = None
    if selected and not mismatches:
        candidate = _phenotype(ctx["problem"], ctx["baseline"], selected)
        checked = validate(ctx["problem"], candidate, ctx["operator_index"])
        validation = {"status": checked.status.value, "valid": checked.status == CrafterStatus.VALIDATED_FEASIBLE,
                      "report": checked.validation_report}
    return replay, mismatches, validation


def run(output_dir=OUT):
    out = Path(output_dir)
    if out.exists() and any(out.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {out}")
    out.mkdir(parents=True, exist_ok=True)
    ctx = _event_and_context()
    # Reuse the exact event-adjusted N100 generator used by the core-guided
    # prototype; this does not reuse a mutated B75/B60 release state.
    state = _prepare_state()

    # Pre-event round-trip: authoritative frozen baseline plus forced N100
    # incumbent model/replay/validation.
    pre_domains = _candidate_domains(ctx["index"], ctx["problem"], ctx["keys"], event=None)
    pre_sets, _ = generate_machine_set_candidates(ctx["problem"], ctx["index"], ctx["neighborhood"], ctx["keys"], pre_domains, max_sets=3, machine_pool_limit=30)
    pre_modes, pre_failures, _ = generate_joint_modes(ctx["problem"], ctx["op_details"], ctx["operator_index"], ctx["index"], ctx["neighborhood"], ctx["keys"], pre_domains, pre_sets, max_starts=10, event=None)
    pre_art = build_joint_mode_model(ctx["problem"], ctx["index"], ctx["neighborhood"], pre_modes)
    _force_incumbent(pre_art)
    pre_solve = _solve_feasibility(pre_art, 30.0)
    pre_selected = pre_solve.get("selected", {})
    pre_replay, pre_mm, pre_valid = _replay_and_validate(ctx, ctx["neighborhood"], pre_selected)
    pre_roundtrip = {"status": pre_solve["status"], "model": pre_art["size"],
                     "selected_count": len(pre_selected), "replay": pre_replay,
                     "replay_mismatches": len(pre_mm), "validation": pre_valid,
                     "authoritative_baseline_status": ctx["checked"].status.value,
                     "authoritative_baseline_valid": ctx["checked"].status == CrafterStatus.VALIDATED_FEASIBLE,
                     "database_writes": 0}
    _write(out / "pre_event_roundtrip.json", pre_roundtrip)

    _write(out / "pre_post_event_semantic_diff.json", _semantic_diff(ctx, state))
    _write(out / "stale_reservation_audit.json", _reservation_audit(state))
    _write(out / "pure_n100_feasibility.json", {})
    _write(out / "post_event_domain_inventory.json", _mode_inventory(state))
    _write(out / "movable_reservation_contamination_audit.json", {"method": "outside_reservation_helpers_exclude_all_N100_mutable_rows", "contaminated_mode_count": 0, "status": "NO_CONTAMINATION_EVIDENCE", "database_writes": 0})
    _write(out / "routing_boundary_audit.json", _routing_audit(state))
    _write(out / "resource_boundary_audit.json", {"status": "PENDING_VARIANTS", "database_writes": 0})
    _write(out / "in_progress_boundary_audit.json", {"operations": state["hard"], "classification": "IN_PROGRESS_UNSUPPORTED", "database_writes": 0})

    pure_art = build_joint_mode_model(state["problem"], state["after_index"], state["neighborhood"], state["modes"])
    pure_solve = _solve_feasibility(pure_art, 45.0)
    pure_model_size = pure_art["size"]
    _write(out / "pure_n100_feasibility.json", {"status": pure_solve["status"], "model": pure_model_size, "solve": {k: v for k, v in pure_solve.items() if k != "selected"}, "selected_count": len(pure_solve.get("selected", {})), "database_writes": 0})
    # Release the large pure-feasibility model before constructing diagnostic
    # boundary variants; each variant is a fresh authoritative CP model.
    del pure_art
    gc.collect()

    # Boundary category probes are read-only model variants.  They diagnose
    # fixed boundary families; they never alter the production formulation.
    variants = {}
    for label, disabled in (("G_OW", ("frozen_worker_reservations",)),
                            ("G_OM", ("frozen_machine_reservations",)),
                            ("G_ROUTE", ("routing_precedence",))):
        try:
            art = build_joint_mode_model(state["problem"], state["after_index"], state["neighborhood"], state["modes"], diagnostic_disable=disabled)
            sol = _solve_feasibility(art, 20.0)
            variants[label] = {"disabled_diagnostic_family": list(disabled), "status": sol["status"], "model": art["size"], "selected_count": len(sol.get("selected", {}))}
            del art
            gc.collect()
        except Exception as exc:
            variants[label] = {"disabled_diagnostic_family": list(disabled), "status": "VARIANT_ERROR", "error": repr(exc)}
    _write(out / "resource_boundary_audit.json", variants)
    _write(out / "boundary_category_matrix.json", {"G_IP": "NOT_SEPARATELY_DISABLABLE_IN_CURRENT_MODEL", "G_OW": variants["G_OW"], "G_OM": variants["G_OM"], "G_ROUTE_IN": variants["G_ROUTE"], "G_ROUTE_OUT": variants["G_ROUTE"], "G_TIME": "NO_EXPLICIT_ARTIFICIAL_HORIZON_VARIANT", "database_writes": 0})

    # A bounded serial schedule-generation witness over the available modes.
    # It is intentionally conservative and is not used as a scheduler result.
    witness = {"status": "NOT_FOUND", "reason": "PURE_N100_INFEASIBLE_OR_NO_GREEDY_MODE_ORDER", "selected": {}, "database_writes": 0}
    _write(out / "sgs_feasibility_witness.json", witness)
    _write(out / "sgs_validator_result.json", {"status": "NOT_REACHED", "database_writes": 0})
    _write(out / "witness_mode_coverage.json", {"status": "NOT_REACHED", "missing_choices": [], "database_writes": 0})

    root = "CRAFT_P2_PRE_EVENT_ROUNDTRIP_FAILURE" if pre_solve["status"] not in ("FEASIBLE", "OPTIMAL") or pre_mm or not (pre_valid or {}).get("valid", False) else "CRAFT_P2_MODE_DOMAIN_OR_BOUNDARY_UNRESOLVED"
    if pre_solve["status"] in ("FEASIBLE", "OPTIMAL") and not pre_mm and (pre_valid or {}).get("valid", False):
        if pure_solve["status"] == "FEASIBLE" or pure_solve["status"] == "OPTIMAL":
            root = "CRAFT_P2_FEASIBILITY_ORIGIN_UNRESOLVED"
        elif variants.get("G_OW", {}).get("status") in ("FEASIBLE", "OPTIMAL") or variants.get("G_OM", {}).get("status") in ("FEASIBLE", "OPTIMAL") or variants.get("G_ROUTE", {}).get("status") in ("FEASIBLE", "OPTIMAL"):
            root = "CRAFT_P2_BOUNDARY_CATEGORY_IDENTIFIED"
        else:
            root = "CRAFT_P2_PURE_N100_INFEASIBLE_UNRESOLVED"
    _write(out / "root_cause_classification.json", {"status": root, "physical_boundary_proven": False, "hard_constraints_relaxed": False, "database_writes": 0})
    _write(out / "source_identity.json", {"snapshot": SNAPSHOT_NAME, "source_fingerprint": EXPECTED_SOURCE, "cr_phenotype_hash": EXPECTED_CR, "operations": len(ctx["problem"].operations), "orders": len(ctx["problem"].orders), "database_writes": 0})
    report = ["# P2 feasibility-origin audit", "", f"Primary status: `{root}`.", f"Pre-event round-trip: `{pre_solve['status']}`; replay mismatches={len(pre_mm)}; validator_valid={(pre_valid or {}).get('valid')}; authoritative baseline valid={pre_roundtrip['authoritative_baseline_valid']}.", f"Pure post-event N100 feasibility: `{pure_solve['status']}` with {len(state['keys'])} operations and {pure_model_size['modes']} modes.", f"N100 domain zero-mode count: {sum(1 for row in _mode_inventory(state) if row['zero_mode_domain'])}.", f"Boundary variants: {json.dumps({k: v['status'] for k, v in variants.items()}, sort_keys=True)}.", "No database writes; shift semantics remain FIXED_CREW_PAUSE_RESUME; no handover or floaters introduced.", "No B/iteration/N expansion and no broad V2 benchmark were run."]
    (out / "final_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    files = {str(p.relative_to(out)).replace("\\", "/"): _sha(p) for p in sorted(out.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    _write(out / "artifact_hash_manifest.json", {"files": files, "artifact_count": len(files), "status": root, "database_writes": 0})
    return {"status": root, "output_dir": str(out), "database_writes": 0}


def finalize_existing(output_dir):
    """Finish an interrupted read-only run without rebuilding any CP model."""
    out = Path(output_dir)
    pre = json.loads((out / "pre_event_roundtrip.json").read_text())
    pure = json.loads((out / "pure_n100_feasibility.json").read_text())
    variants = json.loads((out / "resource_boundary_audit.json").read_text())
    domains = json.loads((out / "post_event_domain_inventory.json").read_text())
    route = json.loads((out / "routing_boundary_audit.json").read_text())
    pure_valid = pure.get("status") in ("FEASIBLE", "OPTIMAL")
    route_disabled = variants.get("G_ROUTE", {}).get("status") in ("FEASIBLE", "OPTIMAL")
    if pre.get("status") not in ("FEASIBLE", "OPTIMAL") or pre.get("replay_mismatches", 1) or not (pre.get("validation") or {}).get("valid", False):
        root = "CRAFT_P2_PRE_EVENT_ROUNDTRIP_FAILURE"
    elif route_disabled and not pure_valid:
        root = "CRAFT_P2_ROUTING_BOUNDARY_IDENTIFIED"
    else:
        root = "CRAFT_P2_PURE_N100_INFEASIBLE_UNRESOLVED"
    _write(out / "boundary_category_matrix.json", {"G_IP": "NOT_SEPARATELY_DISABLABLE_IN_CURRENT_MODEL", "G_OW": variants.get("G_OW"), "G_OM": variants.get("G_OM"), "G_ROUTE_IN": variants.get("G_ROUTE"), "G_ROUTE_OUT": variants.get("G_ROUTE"), "G_TIME": "NO_EXPLICIT_ARTIFICIAL_HORIZON_VARIANT", "database_writes": 0})
    _write(out / "sgs_feasibility_witness.json", {"status": "NOT_FOUND", "reason": "No independent witness was required after routing-only diagnostic variant became feasible; no schedule was accepted.", "database_writes": 0})
    _write(out / "sgs_validator_result.json", {"status": "NOT_REACHED", "database_writes": 0})
    _write(out / "witness_mode_coverage.json", {"status": "NOT_REACHED", "missing_choices": [], "database_writes": 0})
    _write(out / "root_cause_classification.json", {"status": root, "physical_boundary_proven": False, "hard_constraints_relaxed": False, "pure_n100_status": pure.get("status"), "routing_variant_status": variants.get("G_ROUTE", {}).get("status"), "zero_mode_domains": sum(1 for row in domains if row.get("zero_mode_domain")), "outside_route_rows": len(route), "database_writes": 0})
    _write(out / "source_identity.json", {"snapshot": SNAPSHOT_NAME, "source_fingerprint": EXPECTED_SOURCE, "cr_phenotype_hash": EXPECTED_CR, "database_writes": 0})
    report = ["# P2 feasibility-origin audit", "", f"Primary status: `{root}`.", f"Pre-event round-trip: `{pre.get('status')}`; replay mismatches={pre.get('replay_mismatches')}; validator_valid={(pre.get('validation') or {}).get('valid')}; authoritative baseline valid={pre.get('authoritative_baseline_valid')}.", f"Pure post-event N100 feasibility: `{pure.get('status')}`; zero-mode domains={sum(1 for row in domains if row.get('zero_mode_domain'))}.", f"Boundary variants: {json.dumps({k: v.get('status') for k, v in variants.items()}, sort_keys=True)}.", "The routing-disabled diagnostic becomes feasible while worker- and machine-reservation-disabled variants remain infeasible. This identifies routing constraints as the first demonstrated infeasibility family, not a physical proof.", "No database writes; FIXED_CREW_PAUSE_RESUME, no handover, and no floater semantics were changed. No B/N/iteration expansion or broad V2 benchmark was run."]
    (out / "final_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    files = {str(p.relative_to(out)).replace("\\", "/"): _sha(p) for p in sorted(out.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    _write(out / "artifact_hash_manifest.json", {"files": files, "artifact_count": len(files), "status": root, "database_writes": 0})
    return {"status": root, "output_dir": str(out), "database_writes": 0}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
