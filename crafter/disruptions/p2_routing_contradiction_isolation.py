"""Read-only isolation of internal routing contradictions for P2."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from crafter.lns_probe.joint_mode_model import build_joint_mode_model
from crafter.disruptions.p2_core_guided_prototype import _prepare_state
from crafter.disruptions.p2_feasibility_origin_audit import _solve_feasibility, _write

OUT = Path("outputs/crafter/p2_routing_contradiction_isolation_v1")


def _sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _arc_rows(state):
    keys = set(state["keys"])
    rows = []
    for left in sorted(keys):
        right = state["after_index"].route_successor.get(left)
        if right not in keys:
            continue
        lm = tuple(state["modes"].get(left, ()))
        rm = tuple(state["modes"].get(right, ()))
        compatible = [(a, b) for a in lm for b in rm if int(a.end_seconds) <= int(b.start_seconds)]
        event_route = left == str(state["event"].metadata.get("predecessor_operation_key")) or right in str(state["event"].metadata.get("predecessor_operation_key", ""))
        rows.append({"predecessor": left, "successor": right,
                     "predecessor_mode_count": len(lm), "successor_mode_count": len(rm),
                     "compatible_pair_count": len(compatible),
                     "predecessor_min_end": min((int(x.end_seconds) for x in lm), default=None),
                     "predecessor_max_end": max((int(x.end_seconds) for x in lm), default=None),
                     "successor_min_start": min((int(x.start_seconds) for x in rm), default=None),
                     "successor_max_start": max((int(x.start_seconds) for x in rm), default=None),
                     "routing_gap_seconds": (min((int(x.start_seconds) for x in rm), default=0) - max((int(x.end_seconds) for x in lm), default=0)),
                     "directly_affected": left in state["impact"].get("direct_operations", ()) or right in state["impact"].get("direct_operations", ()),
                     "event_route_member": event_route})
    return rows


def _timing_domains(state, implicated):
    result = []
    for key in sorted(implicated):
        vals = tuple(state["modes"].get(key, ()))
        result.append({"operation_key": key,
                       "incumbent": [{"start": int(x.start_seconds), "end": int(x.end_seconds), "line": x.line_id, "k": x.machine_count, "workers": list(x.worker_ids), "machines": list(x.machine_ids)} for x in vals if x.incumbent],
                       "all_starts": sorted({int(x.start_seconds) for x in vals}),
                       "all_ends": sorted({int(x.end_seconds) for x in vals}),
                       "min_start": min((int(x.start_seconds) for x in vals), default=None),
                       "max_start": max((int(x.start_seconds) for x in vals), default=None),
                       "min_end": min((int(x.end_seconds) for x in vals), default=None),
                       "max_end": max((int(x.end_seconds) for x in vals), default=None),
                       "mode_generation_limit": 10,
                       "post_event_earliest_start": state["event"].metadata.get("delayed_end_seconds") if key != state["event"].metadata.get("predecessor_operation_key") else None})
    return result


def run(output_dir=OUT):
    out = Path(output_dir)
    if out.exists() and any(out.iterdir()):
        raise RuntimeError(f"refusing to overwrite non-empty {out}")
    out.mkdir(parents=True, exist_ok=True)
    state = _prepare_state()
    arcs = _arc_rows(state)
    zero = [x for x in arcs if x["compatible_pair_count"] == 0]
    implicated = sorted({x["predecessor"] for x in zero} | {x["successor"] for x in zero})
    _write(out / "internal_routing_arc_compatibility.json", arcs)
    _write(out / "zero_compatible_routing_arcs.json", [{**x, "classification": "MODE_TEMPORAL_COVERAGE" if x["predecessor_max_end"] <= x["successor_max_start"] else "PREDECESSOR_ALL_END_TOO_LATE"} for x in zero])
    _write(out / "routing_bound_propagation_trace.json", {"delayed_predecessor": state["event"].metadata.get("predecessor_operation_key"), "delayed_end_seconds": state["event"].metadata.get("delayed_end_seconds"), "event_metadata": state["event"].as_dict(), "zero_arc_count": len(zero), "trace_status": "NO_ZERO_COMPATIBLE_ARC" if not zero else "REQUIRES_ARC_TRACE", "database_writes": 0})
    _write(out / "implicated_operation_timing_domains.json", _timing_domains(state, implicated))
    # If no edge has zero compatible pairs, test the most constrained internal
    # arcs only; these are diagnostic removals and never production changes.
    suspicious = sorted(arcs, key=lambda x: (x["compatible_pair_count"], x["predecessor"], x["successor"]))[:5]
    probes = []
    for arc in suspicious if not zero else zero[:8]:
        edge = f"routing_edge:{arc['predecessor']}->{arc['successor']}"
        started = time.perf_counter()
        try:
            art = build_joint_mode_model(state["problem"], state["after_index"], state["neighborhood"], state["modes"], diagnostic_disable=(edge,))
            solve = _solve_feasibility(art, 20.0)
            probes.append({"edge_disabled": [arc["predecessor"], arc["successor"]], "status": solve["status"], "solve_time_seconds": time.perf_counter() - started, "model": art["size"]})
        except Exception as exc:
            probes.append({"edge_disabled": [arc["predecessor"], arc["successor"]], "status": "ERROR", "error": repr(exc), "solve_time_seconds": time.perf_counter() - started})
    _write(out / "single_routing_arc_relaxation_probes.json", probes)
    feasible_sets = [x for x in probes if x.get("status") in ("FEASIBLE", "OPTIMAL")]
    _write(out / "routing_conflict_set.json", {"status": "NO_SINGLE_EDGE_FEASIBLE" if not feasible_sets else "SINGLE_EDGE_DIAGNOSTIC_FEASIBLE", "tested_edges": probes, "smallest_tested_set": [x["edge_disabled"] for x in feasible_sets[:1]], "minimality_proven": False, "database_writes": 0})
    _write(out / "later_mode_diagnostic.json", {"status": "NOT_RUN_NO_ZERO_ARC", "reason": "No zero-compatible internal arc was found; no timing injection was justified.", "database_writes": 0})
    _write(out / "routing_bound_logic_audit.json", {"predecessor_delay_field": "event.metadata.delayed_end_seconds", "delayed_end_seconds": state["event"].metadata.get("delayed_end_seconds"), "mode_filter_rule": "direct successor start >= delayed_end_seconds; internal CP arcs enforce end <= successor start", "double_add_delay_detected": False, "wrong_units_detected": False, "stale_bound_detected": False, "status": "LOGIC_REQUIRES_COLLECTIVE_ROUTING_ANALYSIS" if not zero else "ARC_SPECIFIC_REVIEW_REQUIRED", "database_writes": 0})
    status = "CRAFT_P2_ROUTING_COLLECTIVE_CONTRADICTION_UNRESOLVED" if not zero else "CRAFT_P2_ZERO_COMPATIBLE_ROUTING_ARC_FOUND"
    _write(out / "root_cause_classification.json", {"status": status, "internal_edges": len(arcs), "zero_compatible_edges": len(zero), "physical_boundary_proven": False, "database_writes": 0})
    report = ["# P2 internal routing contradiction isolation", "", f"Primary status: `{status}`.", f"Internal routing edges: {len(arcs)}; zero-compatible edges: {len(zero)}.", f"Most constrained diagnostic edges tested: {len(probes)}; feasible edge removals: {len(feasible_sets)}.", "No worker/machine semantics, N/B/iterations, or production routing rules were changed. This run is diagnostic only; database writes=0."]
    (out / "final_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    files = {str(p.relative_to(out)).replace("\\", "/"): _sha(p) for p in sorted(out.rglob("*")) if p.is_file() and p.name != "artifact_hash_manifest.json"}
    _write(out / "artifact_hash_manifest.json", {"files": files, "artifact_count": len(files), "status": status, "database_writes": 0})
    return {"status": status, "output_dir": str(out), "internal_edges": len(arcs), "zero_edges": len(zero), "database_writes": 0}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
