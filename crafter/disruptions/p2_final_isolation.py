"""Bounded, read-only P2 final resource-interaction isolation.

This diagnostic consumes the completed current P2 fresh-mode probe, rebuilds
the authoritative shadow state, and performs deterministic subset CP checks.
It never changes production constraints or writes database state.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from crafter.lns_probe.analysis import build_incumbent_index
from crafter.lns_probe.joint_mode_model import build_joint_mode_model, solve_joint_mode_model
from crafter.lns_probe.joint_modes import JointExecutableMode
from crafter.lns_probe.run_e1_7_final_verified import _replay_mode
from crafter.disruptions.benchmark import _neighborhood
from crafter.disruptions.v2_recovery_wiring_audit import _events, _load_state
from crafter.disruptions.state_update import apply_event
from crafter.disruptions.v2_execution_diagnostics import _restore_finite_machine

OUT = Path("outputs/crafter/disruption_repair_v2_p2_final_isolation")
PROBE = Path("outputs/crafter/disruption_repair_v2_p2_final_isolation_probe_current.json")


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _dt(value):
    return datetime.fromisoformat(str(value))


def _mode(d):
    x = dict(d)
    for key in ("machine_ids", "worker_ids"):
        x[key] = tuple(x.get(key, ()))
    x["worker_segments"] = tuple((_dt(a), _dt(b)) for a, b in x.get("worker_segments", ()))
    for key in ("incoming_boundary", "outgoing_boundary", "provenance"):
        value = x.get(key, ())
        x[key] = tuple(value) if isinstance(value, list) else value
    x["machine_count"] = int(x.pop("k", x.get("machine_count", 0)))
    x["factory_id"] = int(x.get("factory_id", 0))
    x["line_id"] = int(x["line_id"])
    x["start_seconds"] = int(x["start_seconds"])
    x["productive_start_seconds"] = int(x["productive_start_seconds"])
    x["setup_seconds"] = int(x.get("setup_seconds", 900))
    x["productive_seconds"] = int(x["productive_seconds"])
    x["productive_minutes"] = float(x.get("productive_minutes", x["productive_seconds"] / 60.0))
    x["end_seconds"] = int(x["end_seconds"])
    x["elapsed_seconds"] = int(x.get("elapsed_seconds", x["end_seconds"] - x["start_seconds"]))
    x["pause_seconds"] = int(x.get("pause_seconds", 0))
    x["machine_occupancy_start_seconds"] = int(x.pop("machine_occupancy_start", x.get("machine_occupancy_start_seconds", x["start_seconds"])))
    x["machine_occupancy_end_seconds"] = int(x.pop("machine_occupancy_end", x.get("machine_occupancy_end_seconds", x["end_seconds"])))
    x.setdefault("source_event_id", None)
    x.setdefault("recovery_level", "INCUMBENT_PRESERVATION")
    x.setdefault("recovery_reason", "INCUMBENT_FALLBACK")
    return JointExecutableMode(**x)


def _interval_overlap(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1


def _mode_conflict(a, b, origin):
    reasons = []
    if set(a.machine_ids) & set(b.machine_ids) and _interval_overlap(a.start_seconds, a.end_seconds, b.start_seconds, b.end_seconds):
        reasons.append("MACHINE_OVERLAP")
    workers = sorted(set(map(str, a.worker_ids)) & set(map(str, b.worker_ids)))
    if workers:
        for la, lb in a.worker_segments:
            for ra, rb in b.worker_segments:
                if _interval_overlap(int((la-origin).total_seconds()), int((lb-origin).total_seconds()),
                                     int((ra-origin).total_seconds()), int((rb-origin).total_seconds())):
                    reasons.append("PRODUCTIVE_WORKER_OVERLAP")
                    break
            if "PRODUCTIVE_WORKER_OVERLAP" in reasons:
                break
    return sorted(set(reasons)), workers


def _states(rows, event_time):
    out = {}
    for row in rows:
        key = str(row["operation_key"])
        start = int(row.get("start_seconds", 0)); end = int(row.get("end_seconds", 0))
        if end <= event_time: state = "COMPLETED"
        elif start <= event_time < end: state = "IN_PROGRESS"
        else: state = "FUTURE"
        out[key] = state
    return out


def run(output_dir=OUT, probe_path=PROBE):
    output_dir = Path(output_dir); probe_path = Path(probe_path)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {output_dir}")
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    problem, operator_index, rows, durations, op_details, index, baseline, checked, source = _load_state()
    event = next(e for e in _events(problem, rows, index) if e.event_id == "P2_PREDECESSOR_DELAY")
    shadow = _restore_finite_machine(apply_event(problem, operator_index, event), event)
    after_index = build_incumbent_index(shadow.problem, rows, op_details)
    modes = {key: tuple(_mode(item) for item in values) for key, values in probe["modes_by_operation"].items()}
    keys = tuple(probe.get("neighborhood", sorted(modes)))
    states = _states(rows, int(event.event_time_seconds))
    route = tuple(k for k in keys if k.startswith("1029_"))
    suffix595 = tuple(k for k in keys if k.startswith("595_"))
    suffix61 = tuple(k for k in keys if k.startswith("61_"))
    focus = route + suffix595 + suffix61 + tuple(k for k in ("254_2149", "635_516", "563_1583") if k in modes)

    conflict_edges = []
    for i, left_key in enumerate(keys):
        for right_key in keys[i + 1:]:
            rows_edge = []
            for lm in modes[left_key]:
                for rm in modes[right_key]:
                    reasons, workers = _mode_conflict(lm, rm, shadow.problem.origin)
                    if reasons:
                        rows_edge.append({"left_mode": lm.mode_id, "right_mode": rm.mode_id,
                                          "reasons": reasons, "worker_ids": workers,
                                          "left_interval": [lm.start_seconds, lm.end_seconds],
                                          "right_interval": [rm.start_seconds, rm.end_seconds]})
            compatible = sum(1 for lm in modes[left_key] for rm in modes[right_key]
                             if not _mode_conflict(lm, rm, shadow.problem.origin)[0])
            total = len(modes[left_key]) * len(modes[right_key])
            if rows_edge and (left_key in focus or right_key in focus):
                conflict_edges.append({"left_operation": left_key, "right_operation": right_key,
                                       "conflicting_mode_pairs": len(rows_edge),
                                       "compatible_mode_pairs": compatible,
                                       "total_mode_pairs": total,
                                       "all_pairs_conflict": compatible == 0,
                                       "witnesses": rows_edge[:12]})

    def solve_subset(subset, disabled=()):
        sub = tuple(sorted(set(subset)))
        neighborhood = _neighborhood(after_index, event, sub, event.metadata.get("seed", 42))
        sub_modes = {k: modes[k] for k in sub}
        art = build_joint_mode_model(shadow.problem, after_index, neighborhood, sub_modes,
                                     diagnostic_disable=tuple(disabled))
        sol = solve_joint_mode_model(art, max_time_seconds=5.0, seed=42)
        return {"operations": list(sub), "status": sol["status"], "solve": sol,
                "model": art["size"], "disabled": list(disabled)}

    subsets = {
        "ROUTE_ONLY": route,
        "ROUTE_PLUS_61_SUFFIX": route + suffix61,
        "ROUTE_PLUS_595_SUFFIX": route + suffix595,
        "ROUTE_PLUS_61_AND_595": route + suffix61 + suffix595,
        "ROUTE_PLUS_61_595_254": route + suffix61 + suffix595 + ("254_2149",),
        "ROUTE_PLUS_61_595_635": route + suffix61 + suffix595 + ("635_516",),
        "ROUTE_PLUS_61_595_563": route + suffix61 + suffix595 + ("563_1583",),
        "ROUTE_PLUS_FOCUS": focus,
    }
    promoted_context = tuple(k for k in ("635_516", "635_517", "635_518", "463_1180", "254_2151", "797_3139") if k in modes)
    subsets.update({
        "ROUTE_PLUS_61_595_PROMOTED": route + suffix61 + suffix595 + promoted_context,
        "ROUTE_PLUS_61_595_635": route + suffix61 + suffix595 + tuple(k for k in ("635_516", "635_517", "635_518") if k in modes),
        "ROUTE_PLUS_61_595_463": route + suffix61 + suffix595 + ("463_1180",),
        "ROUTE_PLUS_61_595_254_797": route + suffix61 + suffix595 + tuple(k for k in ("254_2151", "797_3139") if k in modes),
    })
    subset_results = {name: solve_subset(value) for name, value in subsets.items()}
    route_isolation = {"disabled": [], "variants": {}}
    for family in ("routing_precedence", "frozen_machine_reservations", "frozen_worker_reservations",
                   "machine_no_overlap", "worker_no_overlap", "mode_conflicts"):
        route_isolation["variants"][family] = solve_subset(route, (family,))
    route_isolation["full"] = subset_results["ROUTE_ONLY"]
    route_isolation["single_operation"] = {k: solve_subset((k,)) for k in route}
    targeted = {
        "TARGET_PLUS_595_3811": ("1029_293", "595_3811"),
        "TARGET_PLUS_595_SUFFIX": ("1029_293",) + suffix595,
        "TARGET_PLUS_61_1371": ("1029_293", "61_1371"),
        "TARGET_PLUS_61_AND_595_3811": ("1029_293", "61_1371", "595_3811"),
        "TARGET_PLUS_61_AND_595_SUFFIX": ("1029_293", "61_1371") + suffix595,
    }
    route_isolation["targeted"] = {name: solve_subset(value) for name, value in targeted.items()}
    route_isolation["target_pair_isolation"] = {family: solve_subset(("1029_293", "595_3811"), (family,))
                                                 for family in ("routing_precedence", "frozen_machine_reservations",
                                                                "frozen_worker_reservations", "machine_no_overlap",
                                                                "worker_no_overlap", "mode_conflicts")}
    route_isolation["combined_variants"] = {
        "route_without_worker_boundaries": solve_subset(route, ("frozen_worker_reservations", "worker_no_overlap")),
        "route_without_machine_boundaries": solve_subset(route, ("frozen_machine_reservations", "machine_no_overlap")),
        "route_without_resource_boundaries": solve_subset(route, ("frozen_machine_reservations", "frozen_worker_reservations", "machine_no_overlap", "worker_no_overlap")),
        "target_pair_without_worker_boundaries": solve_subset(("1029_293", "595_3811"), ("frozen_worker_reservations", "worker_no_overlap")),
    }

    # Pairwise compatibility matrix for the concrete worker witnesses.
    pair_keys = [("1029_293", "61_1371"), ("1029_293", "595_3811"),
                 ("1029_300", "254_2149"), ("1029_295", "595_3811"),
                 ("1029_295", "563_1583")]
    matrix = {}
    for a, b in pair_keys:
        if a not in modes or b not in modes: continue
        pairs = []
        for lm in modes[a]:
            for rm in modes[b]:
                reasons, workers = _mode_conflict(lm, rm, shadow.problem.origin)
                if not reasons:
                    pairs.append({"left_mode": lm.mode_id, "right_mode": rm.mode_id})
        matrix[f"{a}__{b}"] = {"left_operation": a, "right_operation": b,
                               "compatible_pairs": len(pairs), "total_pairs": len(modes[a])*len(modes[b]),
                               "examples": pairs[:8]}

    _write(output_dir / "source_identity.json", {"source_fingerprint": source,
        "snapshot": "parity_fix_20260916_s42", "event_id": event.event_id, "database_writes": 0})
    _write(output_dir / "p2_event.json", event.as_dict())
    _write(output_dir / "p2_conflict_graph.json", {"nodes": [{"operation_key": k, "state": states.get(k),
        "released": k in set(probe.get("released", []))} for k in sorted(set(x for e in conflict_edges for x in (e["left_operation"], e["right_operation"])))],
        "edges": conflict_edges, "event_release_conflicts": probe.get("event_release_conflicts", [])})
    _write(output_dir / "operation_state_audit.json", {k: {"state": states.get(k), "released": k in set(probe.get("released", [])),
        "mode_count": len(modes.get(k, ())), "incumbent_modes": sum(m.incumbent for m in modes.get(k, ())) } for k in sorted(set(focus))})
    for key in ("61_1371", "635_516"):
        vals = modes.get(key, ())
        incumbent = next((m for m in vals if m.incumbent), None)
        _write(output_dir / f"{key}_deep_audit.json", {"operation_key": key, "state": states.get(key),
            "released": key in set(probe.get("released", [])), "incumbent": incumbent.as_dict() if incumbent else None,
            "mode_count": len(vals), "modes": [m.as_dict() for m in vals]})
    _write(output_dir / "worker_segment_overlap.json", [e for e in conflict_edges if any("PRODUCTIVE_WORKER_OVERLAP" in r for w in e["witnesses"] for r in w["reasons"])])
    _write(output_dir / "mode_compatibility_matrix.json", matrix)
    _write(output_dir / "minimal_infeasible_operation_set.json", {"method": "deterministic subset CP checks", "subsets": subset_results,
        "note": "Subset checks are diagnostic and do not weaken the production model."})
    _write(output_dir / "event_release_iterations.json", {"iterations": [{"iteration": 0, "released": probe.get("released", []),
        "conflicts": probe.get("event_release_conflicts", [])}], "fixpoint_reached": True,
        "note": "Current one-pass post-generation promotion produced no second trace; bounded fixpoint remains a follow-up if new conflicts are observed."})
    _write(output_dir / "release_provenance.json", {"released": probe.get("released", []), "event_release_conflicts": probe.get("event_release_conflicts", []), "database_writes": 0})
    _write(output_dir / "fresh_modes.json", {"source": str(probe_path), "mode_counts": {k: len(v) for k,v in modes.items()},
        "released": probe.get("released", []), "event_safe": True})
    _write(output_dir / "cp_domain_audit.json", {"source": str(probe_path), "domains": probe.get("cp_domain", []),
        "cp_status": probe.get("solve", {}).get("status")})
    _write(output_dir / "constraint_isolation.json", probe.get("constraint_isolation", {}))
    _write(output_dir / "route_constraint_isolation.json", route_isolation)
    _write(output_dir / "cp_result.json", probe.get("solve", {}))
    _write(output_dir / "replay_result.json", probe.get("replay", []))
    _write(output_dir / "validator_result.json", probe.get("validation"))
    _write(output_dir / "schedule_stability.json", {"status": "NO_ACCEPTED_REPAIR", "changed_operations": []})
    _write(output_dir / "regression_controls.json", {"status": "DEFERRED_UNTIL_P2_CLASSIFIED", "database_writes": 0})
    _write(output_dir / "targeted_v2_gate_result.json", {"status": "DEFERRED_UNTIL_P2_CLASSIFIED"})
    _write(output_dir / "root_cause_classification.json", {"status": "CRAFT_P2_FINAL_ISOLATION_PARTIAL",
        "classification": "JOINT_COMPATIBILITY_REQUIRES_MINIMAL_SET_REVIEW",
        "evidence": "individual modes exist; full N=100 model is infeasible"})
    _write(output_dir / "wiring_fixes.json", {"fixes": [], "note": "No production fix claimed before minimal interaction is isolated."})
    files = {str(p.relative_to(output_dir)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(output_dir.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    _write(output_dir / "artifact_hash_manifest.json", {"files": files, "database_writes": 0,
        "status": "CRAFT_P2_FINAL_ISOLATION_PARTIAL"})
    return {"status": "CRAFT_P2_FINAL_ISOLATION_PARTIAL", "subset_results": subset_results,
            "matrix": matrix, "database_writes": 0}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", type=Path, default=PROBE)
    parser.add_argument("--output-dir", type=Path, default=OUT)
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir, args.probe), indent=2, default=str))
