"""Read-only external worker-boundary audit for the current P2 mode probe."""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from crafter.disruptions.v2_recovery_wiring_audit import _load_state
from crafter.disruptions.v2_recovery_wiring_audit import _events
from crafter.disruptions.state_update import apply_event
from crafter.disruptions.v2_execution_diagnostics import _restore_finite_machine
from crafter.lns_probe.analysis import build_incumbent_index
from crafter.lns_probe.anchored_model import outside_worker_reservations

def run(probe_path, output_path):
    p,oi,rows,du,od,idx,base,chk,src=_load_state()
    ev=next(e for e in _events(p,rows,idx) if e.event_id=='P2_PREDECESSOR_DELAY')
    sh=_restore_finite_machine(apply_event(p,oi,ev),ev)
    ai=build_incumbent_index(sh.problem,rows,od)
    probe=json.loads(Path(probe_path).read_text())
    mode=next(x for x in probe['modes_by_operation']['1029_293'] if not x.get('incumbent',False))
    segs=[(datetime.fromisoformat(a),datetime.fromisoformat(b)) for a,b in mode['worker_segments']]
    workers=set(map(str,mode['worker_ids'])); mutable=set(probe['neighborhood'])
    res=outside_worker_reservations(sh.problem,ai,mutable)
    overlaps=[]
    for w in sorted(workers):
        for left,right in res.get(w,[]):
            for a,b in segs:
                if a<right and left<b:
                    overlaps.append({'worker_id':w,'mode_segment':[a.isoformat(),b.isoformat()],
                                     'reservation':[left.isoformat(),right.isoformat()],
                                     'overlap_seconds':max(0,int((min(b,right)-max(a,left)).total_seconds()))})
    fixed=[]
    for item in sh.problem.resources.fixed_worker_commitments:
        w=str(item.employee_id)
        if w not in workers: continue
        left=sh.problem.origin+__import__('datetime').timedelta(seconds=int(item.start_seconds))
        right=sh.problem.origin+__import__('datetime').timedelta(seconds=int(item.end_seconds))
        for a,b in segs:
            if a<right and left<b:
                fixed.append({'worker_id':w,'mode_segment':[a.isoformat(),b.isoformat()],
                              'reservation':[left.isoformat(),right.isoformat()],
                              'overlap_seconds':max(0,int((min(b,right)-max(a,left)).total_seconds())),
                              'production_order_id':str(item.production_order_id)})
    mode_external = {}
    for mutable_keys in ({'1029_293','595_3811'}, {'1029_293','61_1371'}, set(probe['neighborhood'])):
        label = '+'.join(sorted(mutable_keys)) if len(mutable_keys) < 10 else 'full_neighborhood'
        reservations = outside_worker_reservations(sh.problem, ai, mutable_keys)
        rows_mode=[]
        for key in ('1029_293','595_3811','61_1371'):
            for candidate in probe['modes_by_operation'].get(key,[]):
                csegs=[(datetime.fromisoformat(a),datetime.fromisoformat(b)) for a,b in candidate.get('worker_segments',())]
                cworkers=set(map(str,candidate.get('worker_ids',())))
                hits=[]
                for w in sorted(cworkers):
                    for left,right in reservations.get(w,[]):
                        for a,b in csegs:
                            if a<right and left<b:
                                hits.append({'worker_id':w,'segment':[a.isoformat(),b.isoformat()],
                                             'reservation':[left.isoformat(),right.isoformat()]})
                rows_mode.append({'mutable_context':label,'operation_key':key,'mode_id':candidate['mode_id'],
                                  'start_seconds':candidate['start_seconds'],'end_seconds':candidate['end_seconds'],
                                  'external_overlap_count':len(hits),'external_overlaps':hits[:8]})
        mode_external[label]=rows_mode
    out={'mutable_count':len(mutable),'target_mode':mode,'outside_worker_overlaps':overlaps,
         'fixed_commitment_overlaps':fixed,'worker_reservation_universe':{w:len(res.get(w,())) for w in sorted(workers)},
         'mode_external_reservation_audit':mode_external,
         'database_writes':0}
    Path(output_path).write_text(json.dumps(out,indent=2,sort_keys=True,default=str)+'\n')
    print(json.dumps({'outside_overlaps':len(overlaps),'fixed_overlaps':len(fixed),'output':str(output_path)}))
if __name__=='__main__':
    import argparse
    ap=argparse.ArgumentParser(); ap.add_argument('--probe',required=True); ap.add_argument('--output',required=True); a=ap.parse_args(); run(a.probe,a.output)
