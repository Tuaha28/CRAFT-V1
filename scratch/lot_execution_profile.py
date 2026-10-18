"""How the captured factories actually execute operations: lot sizes, operators
per operation, parallel lots, and overlap between consecutive operations.
Read-only over the pinned capture."""
from collections import defaultdict
from datetime import datetime
import json, statistics, sys
from pathlib import Path

CAP = Path('web_v1/audits/factory_source_integration_v1/capture_20261012T223420800478Z')

def t(v):
    return datetime.fromisoformat(v) if v else None

def q(xs):
    xs = sorted(xs)
    return {'n': len(xs), 'p10': xs[len(xs)//10], 'median': statistics.median(xs), 'p90': xs[9*len(xs)//10], 'max': xs[-1]} if xs else None

for f in map(int, sys.argv[1:] or range(1, 7)):
    s = json.loads((CAP / f'factory{f}_source.json').read_text())
    seq = {(str(o['production_order_id']), str(o['operation_id'])): int(o['operation_sequence_number']) for o in s['operations']}
    by_op = defaultdict(list)
    status = defaultdict(int)
    for l in s['lots']:
        status[l['execution_status']] += 1
        by_op[(str(l['production_order_id']), str(l['operation_id']))].append(l)
    lots_per_op = [len(v) for v in by_op.values()]
    lot_qty = [int(l['lot_quantity']) for l in s['lots']]
    workers_per_op = [len({l['employee_id'] for l in v}) for v in by_op.values()]
    machines_per_op = [len({l['asset_id'] for l in v}) for v in by_op.values()]
    # Parallel: max number of lots of one operation in progress at the same time (planned times).
    par = []
    for v in by_op.values():
        ev = sorted([(t(l['planned_start_time']), 1) for l in v if l['planned_start_time']] +
                    [(t(l['planned_end_time']), -1) for l in v if l['planned_end_time']], key=lambda e: (e[0], e[1]))
        cur = best = 0
        for _, d in ev:
            cur += d; best = max(best, cur)
        par.append(best)
    # Overlap: does op n+1 start (planned) before op n finishes?
    by_order = defaultdict(dict)
    for (o, op), v in by_op.items():
        st = [t(l['planned_start_time']) for l in v if l['planned_start_time']]
        en = [t(l['planned_end_time']) for l in v if l['planned_end_time']]
        if st and en and (o, op) in seq:
            by_order[o][seq[(o, op)]] = (min(st), max(en))
    overlap = total = 0
    for ops in by_order.values():
        ks = sorted(ops)
        for a, b in zip(ks, ks[1:]):
            total += 1
            overlap += ops[b][0] < ops[a][1]
    print(json.dumps({'factory': f, 'lots': len(s['lots']), 'status': dict(status),
                      'lots_per_operation': q(lots_per_op), 'lot_quantity': q(lot_qty),
                      'distinct_workers_per_operation': q(workers_per_op),
                      'distinct_machines_per_operation': q(machines_per_op),
                      'max_parallel_lots_per_operation': q(par),
                      'consecutive_ops_overlapping_pct': round(100 * overlap / total, 1) if total else None,
                      'consecutive_pairs': total}))
