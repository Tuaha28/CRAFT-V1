"""Extract selected productive-worker witnesses from a diagnostic relaxed CP model."""
from __future__ import annotations
import json
from pathlib import Path
from .p2_final_isolation import _mode
from .v2_recovery_wiring_audit import _load_state, _events
from .v2_execution_diagnostics import _restore_finite_machine
from .state_update import apply_event
from crafter.lns_probe.analysis import build_incumbent_index
from crafter.lns_probe.joint_mode_model import build_joint_mode_model, solve_joint_mode_model
from crafter.disruptions.benchmark import _neighborhood

def overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]

def run(probe_path, out_path):
    d = json.loads(Path(probe_path).read_text())
    p, oi, rows, du, od, idx, base, chk, src = _load_state()
    ev = next(e for e in _events(p, rows, idx) if e.event_id == 'P2_PREDECESSOR_DELAY')
    sh = _restore_finite_machine(apply_event(p, oi, ev), ev)
    ai = build_incumbent_index(sh.problem, rows, od)
    modes = {k: tuple(_mode(x) for x in v) for k, v in d['modes_by_operation'].items()}
    keys = tuple(d['neighborhood'])
    nb = _neighborhood(ai, ev, keys, 42)
    art = build_joint_mode_model(sh.problem, ai, nb, modes, diagnostic_disable=('worker_no_overlap',))
    sol = solve_joint_mode_model(art, max_time_seconds=30, seed=42)
    selected = sol.get('selected', {})
    rows_out = []
    items = sorted(selected.items())
    for i, (ka, a) in enumerate(items):
        for kb, b in items[i + 1:]:
            shared = sorted(set(map(str, a.worker_ids)) & set(map(str, b.worker_ids)))
            if not shared:
                continue
            for la, lb in a.worker_segments:
                for ra, rb in b.worker_segments:
                    if overlap((la, lb), (ra, rb)):
                        rows_out.append({
                            'left_operation': ka, 'right_operation': kb,
                            'worker_ids': shared, 'left_mode': a.mode_id,
                            'right_mode': b.mode_id,
                            'left_segments': [[la.isoformat(), lb.isoformat()]],
                            'right_segments': [[ra.isoformat(), rb.isoformat()]],
                            'overlap_seconds': int((min(lb, rb) - max(la, ra)).total_seconds()),
                            'left_released': ka in set(d.get('released', [])),
                            'right_released': kb in set(d.get('released', [])),
                        })
    out = {'solve': sol, 'witnesses': rows_out,
           'selected_modes': {k: m.as_dict() for k, m in selected.items()},
           'database_writes': 0}
    Path(out_path).write_text(json.dumps(out, indent=2, sort_keys=True, default=str) + '\n')
    print(json.dumps({'status': sol['status'], 'selected': len(selected), 'witnesses': len(rows_out)}))

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument('--probe', required=True); ap.add_argument('--output', required=True)
    a = ap.parse_args(); run(a.probe, a.output)
