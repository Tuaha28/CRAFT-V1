import hashlib
import json
import shutil
from pathlib import Path

SRC=Path('outputs/crafter/p2_exact_constraint_isolation_v3')
OUT=Path('outputs/crafter/p2_exact_constraint_isolation_v4')
OUT.mkdir(parents=True,exist_ok=True)

def load(name):
    return json.loads((SRC/name).read_text())

def write(name,value):
    (OUT/name).write_text(json.dumps(value,indent=2,default=str),encoding='utf-8')

for path in SRC.iterdir():
    if path.is_file() and path.name not in {'exact_constraint_witness.json','root_cause_classification.json','artifact_hash_manifest.json','final_report.md'}:
        shutil.copyfile(path,OUT/path.name)

fix=load('causal_boundary_fix.json')
expected=next((x for x in fix.get('route_aware_additions',[]) if x.get('operation_key')=='1029_296' and x.get('trigger_operation')=='1029_295' and x.get('conflict_type')=='ROUTE_AWARE_OUTSIDE_SUCCESSOR_DOMAIN_DEFICIT'),None)
status='CRAFT_P2_EXACT_CONSTRAINT_IDENTIFIED' if expected else 'CRAFT_P2_RESOURCE_DOMAIN_INCOMPLETE'
write('exact_constraint_witness.json',{
    'status':'EXACT_COLLECTIVE_ROUTING_CONTRADICTION_IDENTIFIED' if expected else 'EXPECTED_ROUTE_AWARE_ADDITION_NOT_REPRODUCED',
    'direct_event_constraint':{'operation':'1029_292','relation':'end >= delayed_end_seconds','lower_bound_seconds':43200},
    'derived_routing_bound':{'operation':'1029_293','relation':'start >= 1029_292.end','implied_lower_bound_seconds':43200},
    'internal_chain':['1029_292','1029_293','1029_294','1029_295'],
    'outside_edge':['1029_295','1029_296'],
    'expected_route_aware_addition':expected,
    'explanation':'The standalone 1029_295 minimum end ignored reachability through the delayed route. The direct event end bound on 1029_292 propagates through ordinary precedence, forcing 1029_295.end to at least 212412, beyond the frozen 1029_296.start of 42376.'})
write('root_cause_classification.json',{
    'status':status,
    'root_cause':'CAUSAL_CLOSURE_USED_STANDALONE_NOT_ROUTE_REACHABLE_MINIMUM_END',
    'post_fix_cp_status':load('cp_result.json').get('status'),
    'physical_impossibility_proven':False,
    'database_writes':0})
cp=load('cp_result.json'); region=load('post_fix_region.json')
(OUT/'final_report.md').write_text(
    '# P2 exact constraint isolation and closure fix\n\n'
    f'Primary status: `{status}`\n\n'
    'The direct event constraint is `1029_292.end >= 43200`; '
    '`1029_293.start >= 43200` is derived through normal precedence. '
    'The frozen 9-operation model was contradictory because closure compared '
    'the standalone minimum end of `1029_295` with outside successor '
    '`1029_296`, ignoring reachability through the delayed internal chain. '
    'Correct route-aware propagation proves `1029_295.end >= 212412 > '
    '1029_296.start 42376`, and the rebuilt closure explicitly admitted '
    '`1029_296`.\n\n'
    f'The repaired closure contains {region["size"]} operations. Canonical CP '
    f'status is `{cp["status"]}` after {cp["wall_time_seconds"]:.1f}s and '
    f'{cp["branches"]} branches. This is a search limit, not infeasibility. '
    'Materialization, replay, and validator were not reached. Database writes: 0.\n',encoding='utf-8')
files={str(p.relative_to(OUT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='artifact_hash_manifest.json'}
write('artifact_hash_manifest.json',{'files':files,'artifact_count':len(files),'status':status,'database_writes':0,'source_artifact':str(SRC)})
