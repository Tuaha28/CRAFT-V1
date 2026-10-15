"""Read-only causal probes for the frozen Window-14 K=16 exact domain."""
from __future__ import annotations
import contextlib, io, json, time
from pathlib import Path
import sys
sys.path.insert(0, ".")
from crafter.disruptions.benchmark import _load_state
from crafter.repair.planning import build_planning_state
from crafter.repair.calendar_regimes import CalendarRegimeCache, generate_calendar_regimes
from crafter.repair.boundary_aware_modes import generate_boundary_aware_configurations
from crafter.repair.cp_repair import build_repair_model
from ortools.sat.python import cp_model
from scratch.run_window14_step4b import W14_KEYS, context, make_boundary, rows_and_provenance, quiet

OUT=Path("outputs/crafter/factory2_172_v1/window14_step4b")

def solve_variant(state, regimes, **kwargs):
    started=time.perf_counter()
    try:
        artifact=build_repair_model(state, regimes, **kwargs)
        solver=cp_model.CpSolver(); solver.parameters.num_search_workers=8
        solver.parameters.random_seed=42; solver.parameters.max_time_in_seconds=30.0
        code=solver.Solve(artifact["model"])
        return {"status":solver.StatusName(code),"feasible":code in (cp_model.FEASIBLE,cp_model.OPTIMAL),
                "wall_time_seconds":solver.WallTime(),"elapsed_seconds":time.perf_counter()-started,
                "branches":solver.NumBranches(),"conflicts":solver.NumConflicts(),**artifact["size"],**kwargs}
    except Exception as exc:
        return {"status":"DIAGNOSTIC_ERROR","feasible":False,"error":f"{type(exc).__name__}: {exc}","elapsed_seconds":time.perf_counter()-started,**kwargs}

def main():
    loaded=quiet(_load_state); problem=loaded[0]; route=loaded[5]
    accepted, rows, _=rows_and_provenance(); boundary, facts=make_boundary(route, rows)
    state=quiet(build_planning_state, loaded, context(W14_KEYS), boundary=boundary)
    configs, funnel, stats, idx=quiet(generate_boundary_aware_configurations,state,boundary,crew_diversity=16,machine_set_cap=3,max_start_probes=4)
    cache=CalendarRegimeCache(); regimes, summary,total,equiv=quiet(generate_calendar_regimes,state,configs,cache=cache)
    variants={
        "exact": {},
        "changeover_disabled": {"include_changeover":False},
        "release_bounds_disabled": {"include_release_bounds":False},
        "event_bounds_disabled": {"include_event_bounds":False},
        "changeover_and_routing_disabled": {"include_changeover":False,"include_internal_routing":False,"include_outside_routing":False},
        "all_timing_semantics_disabled": {"include_changeover":False,"include_internal_routing":False,"include_outside_routing":False,"include_release_bounds":False,"include_event_bounds":False},
        "all_resource_overlap_disabled": {"include_internal_machine_no_overlap":False,"include_outside_machine_reservations":False,"include_internal_worker_no_overlap":False,"include_outside_worker_reservations":False},
        "all_resource_overlap_and_routing_disabled_keep_changeover": {"include_internal_machine_no_overlap":False,"include_outside_machine_reservations":False,"include_internal_worker_no_overlap":False,"include_outside_worker_reservations":False,"include_internal_routing":False,"include_outside_routing":False},
        "all_resource_overlap_internal_routing_disabled": {"include_internal_machine_no_overlap":False,"include_outside_machine_reservations":False,"include_internal_worker_no_overlap":False,"include_outside_worker_reservations":False,"include_internal_routing":False},
        "all_resource_overlap_outside_routing_disabled": {"include_internal_machine_no_overlap":False,"include_outside_machine_reservations":False,"include_internal_worker_no_overlap":False,"include_outside_worker_reservations":False,"include_outside_routing":False},
        "all_resource_and_routing_disabled": {"include_internal_machine_no_overlap":False,"include_outside_machine_reservations":False,"include_internal_worker_no_overlap":False,"include_outside_worker_reservations":False,"include_internal_routing":False,"include_outside_routing":False,"include_changeover":False},
    }
    results={name:solve_variant(state,regimes,**kwargs) for name,kwargs in variants.items()}
    out={"domain":{"crew_diversity":16,"resource_configurations":sum(len(v) for v in configs.values()),"calendar_regimes":total},"variants":results,"diagnostic_only":True,"database_writes":0}
    (OUT/"semantic_constraint_probes.json").write_text(json.dumps(out,indent=2,default=str)+"\n",encoding="utf-8")
if __name__=="__main__": main()
