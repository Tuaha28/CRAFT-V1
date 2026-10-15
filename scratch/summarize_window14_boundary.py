"""Read-only summary of exact Window-14 boundary interactions."""
from __future__ import annotations
import contextlib, io, json, sys
from pathlib import Path
sys.path.insert(0, ".")
from crafter.disruptions.benchmark import _load_state
from scratch.diagnose_window14_v3 import _accepted_rows, _make_boundary

def main():
    item = json.loads(Path("outputs/crafter/resource_domain_v3/factory2_v3_run/window_014.json").read_text())
    selected = tuple(item["window_acceptance"]["scope"]["missing_required_operations"])
    with contextlib.redirect_stdout(io.StringIO()):
        loaded = _load_state()
    problem, _oi, _rows, _dur, _details, route, _base, _checked, _source = loaded
    windows, committed = _accepted_rows()
    boundary, facts = _make_boundary(route, committed, 2)
    selected_set = set(selected)
    route_rows = []
    for key in selected:
        pred = route.route_predecessor.get(key)
        preds = (pred,) if isinstance(pred, str) else tuple(pred or ())
        for p in preds:
            if str(p) in committed:
                row = committed[str(p)]
                route_rows.append({"predecessor": str(p), "successor": str(key),
                                   "predecessor_end_seconds": int(row["end_seconds"]),
                                   "window_start_seconds": 326400,
                                   "window_end_seconds": 412800,
                                   "crosses_window": int(row["end_seconds"]) > 326400})
    max_machine_end = {}
    for row in boundary.machine_commitments:
        mid = str(row["machine_id"])
        max_machine_end[mid] = max(max_machine_end.get(mid, 0), int(row["end_seconds"]))
    max_worker_end = {}
    for row in boundary.worker_commitments:
        wid = str(row["worker_id"])
        max_worker_end[wid] = max(max_worker_end.get(wid, 0), int(row["end_seconds"]))
    out = {"selected_count": len(selected), "committed_count": len(committed),
           "route_boundary_rows": route_rows,
           "route_boundary_violating_end_count": sum(x["predecessor_end_seconds"] > x["window_end_seconds"] for x in route_rows),
           "max_committed_machine_end_seconds": max(max_machine_end.values(), default=0),
           "max_committed_worker_segment_end_seconds": max(max_worker_end.values(), default=0),
           "boundary_machine_fact_count": len(boundary.machine_commitments),
           "boundary_worker_fact_count": len(boundary.worker_commitments),
           "boundary_routing_fact_count": len(boundary.routing_completion_state),
           "database_writes": 0}
    print(json.dumps(out, indent=2, sort_keys=True))

if __name__ == "__main__":
    main()
