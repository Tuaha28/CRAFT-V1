"""Read-only route graph inspection for the frozen Window-14 diagnosis."""
from __future__ import annotations
import contextlib, io, json
from pathlib import Path
import sys
sys.path.insert(0, ".")
from crafter.disruptions.benchmark import _load_state
from crafter.repair.planning import build_planning_state
from scratch.run_window14_step4b import W14_KEYS, RUN, context, make_boundary, rows_and_provenance, quiet

def main():
    loaded = quiet(_load_state)
    route = loaded[5]
    accepted, rows, provenance = rows_and_provenance()
    boundary, facts = make_boundary(route, rows)
    state = quiet(build_planning_state, loaded, context(W14_KEYS), boundary=boundary)
    selected = set(W14_KEYS)
    edges = []
    for key in sorted(selected):
        pred = route.route_predecessor.get(key)
        succ = route.route_successor.get(key)
        preds = [str(pred)] if isinstance(pred, str) else [str(x) for x in (pred or ())]
        succs = [str(succ)] if isinstance(succ, str) else [str(x) for x in (succ or ())]
        for item in preds:
            edges.append({"predecessor": item, "successor": key, "inside": item in selected})
        for item in succs:
            edges.append({"predecessor": key, "successor": item, "inside": item in selected})
    internal = [(x["predecessor"], x["successor"]) for x in edges if x["inside"] and x["predecessor"] in selected and x["successor"] in selected]
    readiness = []
    for key in sorted(selected):
        pred = route.route_predecessor.get(key)
        pred_keys = [str(pred)] if isinstance(pred, str) else [str(x) for x in (pred or ())]
        readiness.append({"operation_key": key,
                          "predecessors": pred_keys,
                          "committed_predecessor_ends": {p: (state.get("committed_routing_completion") or {}).get(p) for p in pred_keys if p in (state.get("committed_routing_completion") or {})},
                          "baseline_assignment_end": next((int(a.assignment.end_seconds) for a in state["baseline"].operations if str(a.assignment.operation_key) in pred_keys), None)})
    cycles = []
    for start in sorted(selected):
        path=[]; seen={}
        cur=start
        while cur in selected and cur not in seen:
            seen[cur]=len(path); path.append(cur)
            nxt=[b for a,b in internal if a==cur]
            cur=nxt[0] if nxt else None
        if cur in seen:
            cycle=path[seen[cur]:]
            canon=min(tuple(cycle[i:]+cycle[:i]) for i in range(len(cycle)))
            if canon not in cycles: cycles.append(canon)
    out={"window_id":"window-014","selected_operation_keys":sorted(selected),"edges":edges,
         "internal_edges":internal,"internal_cycles":[list(x) for x in sorted(cycles)],
         "readiness":readiness,
         "boundary_routing_keys":sorted(str(x) for x in boundary.routing_completion_state),"database_writes":0}
    outpath=Path("outputs/crafter/factory2_172_v1/window14_step4b/routing_conflict_analysis.json")
    outpath.write_text(json.dumps(out,indent=2)+"\n",encoding="utf-8")
if __name__ == "__main__": main()
