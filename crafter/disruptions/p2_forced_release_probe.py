"""Single P2 probe with a bounded, evidence-derived release set."""
from __future__ import annotations
import json, hashlib, os
from pathlib import Path
from .v2_recovery_wiring_audit import _events, _load_state, _run_case

FORCED = ("635_516", "635_517", "635_518", "463_1180", "254_2151", "797_3139")

def run(output_dir):
    out=Path(output_dir)
    if out.exists() and any(out.iterdir()): raise RuntimeError(f"refusing to overwrite {out}")
    p,oi,rows,du,od,idx,base,chk,src=_load_state()
    ev=next(e for e in _events(p,rows,idx) if e.event_id=='P2_PREDECESSOR_DELAY')
    tr=_run_case(p,oi,rows,od,idx,base,chk,ev,n=100,b=20,diagnose=False,forced_release=FORCED)
    out.mkdir(parents=True,exist_ok=True)
    payload={'source_fingerprint':src,'snapshot':'parity_fix_20260916_s42','event_id':ev.event_id,'forced_release':FORCED,'database_writes':0}
    (out/'source_identity.json').write_text(json.dumps(payload,indent=2,default=str)+'\n')
    (out/'pipeline_trace.json').write_text(json.dumps(tr,indent=2,sort_keys=True,default=str)+'\n')
    for name,val in [('cp_result.json',tr.get('solve',{})),('replay_result.json',tr.get('replay',[])),('validator_result.json',tr.get('validation')),
                     ('release_provenance.json',{'released':tr.get('released',[]),'event_release_conflicts':tr.get('event_release_conflicts',[]),'forced_release':FORCED,'database_writes':0})]:
        (out/name).write_text(json.dumps(val,indent=2,sort_keys=True,default=str)+'\n')
    files={str(p.relative_to(out)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in out.rglob('*') if p.is_file()}
    (out/'artifact_hash_manifest.json').write_text(json.dumps({'files':files,'database_writes':0,'status':tr.get('solve',{}).get('status')},indent=2,sort_keys=True)+'\n')
    print(json.dumps({'status':tr.get('solve',{}).get('status',tr.get('mode_generation_status')),'released':tr.get('released',[]),'event_release_conflicts':tr.get('event_release_conflicts',[]),'validation':tr.get('validation'),'generation_seconds':tr.get('generation_seconds')}))

if __name__=='__main__':
 import argparse; ap=argparse.ArgumentParser(); ap.add_argument('--output-dir',required=True); a=ap.parse_args(); run(a.output_dir)
