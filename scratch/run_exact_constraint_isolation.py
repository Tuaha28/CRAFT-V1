import bisect
import gc
import hashlib
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, '.')
from ortools.sat.python import cp_model

from crafter.disruptions.benchmark import _neighborhood
from crafter.lns_probe.anchored_model import outside_machine_reservations, outside_worker_reservations
from crafter.repair.calendar_regimes import generate_calendar_regimes
from crafter.repair.causal_closure import MOVABLE
from crafter.repair.cp_repair import build_repair_model
from crafter.repair.engine import build_p2_state
from crafter.repair.resource_configs import ResourceConfiguration

SRC = Path('outputs/crafter/p2_adaptive_resource_expansion_v4')
OUT = Path('outputs/crafter/p2_exact_constraint_isolation_v2')
OUT.mkdir(parents=True, exist_ok=True)

def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')

def read(name):
    return json.loads((SRC / name).read_text())

def solve(art, seconds=35):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(seconds)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 42
    started = time.time(); code = solver.Solve(art['model'])
    return solver, {
        'status': solver.StatusName(code), 'code': int(code),
        'variables': art['size']['variables'], 'constraints': art['size']['constraints'],
        'optional_intervals': art['size']['optional_intervals'],
        'branches': solver.NumBranches(), 'conflicts': solver.NumConflicts(),
        'wall_time_seconds': solver.WallTime(), 'elapsed_seconds': time.time() - started,
        'presolve_infeasible': solver.StatusName(code) == 'INFEASIBLE' and solver.NumBranches() == 0,
    }

def fixed_state(keys):
    state = build_p2_state(); state['keys'] = tuple(sorted(keys)); mutable = set(keys)
    state['_machine_res'] = outside_machine_reservations(state['problem'], state['after_index'], mutable)
    state['_worker_res'] = outside_worker_reservations(state['problem'], state['after_index'], mutable)
    state['neighborhood'] = _neighborhood(state['after_index'], state['event'], state['keys'], state['event'].metadata.get('seed', 42))
    return state

def restore_configs(raw):
    out = {}
    for key, rows in raw.items():
        vals = []
        for row in rows:
            row = dict(row)
            row['machine_ids'] = tuple(int(x) for x in row['machine_ids'])
            row['worker_ids'] = tuple(str(x) for x in row['worker_ids'])
            row['provenance'] = tuple(row.get('provenance', ()))
            vals.append(ResourceConfiguration(**row))
        out[key] = tuple(vals)
    return out

def end_bounds(rows):
    values = []
    for row in rows:
        a, b = row['formula'][0]
        values.extend((int(a) * int(row['lo']) + int(b), int(a) * int(row['hi']) + int(b)))
    return (min(values), max(values)) if values else (None, None)

def start_bounds(rows):
    return (min(int(x['lo']) for x in rows), max(int(x['hi']) for x in rows)) if rows else (None, None)

def main():
    raw = read('expanded_resource_domain.json'); expected = {
        '1029_292':84, '1029_293':30, '1029_294':30, '1029_295':30,
        '1029_297':30, '128_1833':30, '595_3811':30, '61_1370':120, '61_1371':30}
    counts = {key: len(vals) for key, vals in raw.items()}
    if counts != expected or sum(counts.values()) != 414:
        raise RuntimeError(f'frozen domain mismatch: {counts}')
    configs = restore_configs(raw); state = fixed_state(configs)
    write('source_identity.json', {'source_artifact': str(SRC), 'operation_count':len(configs),
          'configuration_count':sum(counts.values()), 'configuration_counts':counts,
          'source':state.get('source'), 'database_writes':0})
    started=time.time(); regimes, summary, total, eq = generate_calendar_regimes(state, configs)
    if total != 34786 or eq['mismatches'] != 0:
        raise RuntimeError(f'frozen regime mismatch total={total} mismatches={eq["mismatches"]}')
    print(json.dumps({'phase':'regimes_fixed','regimes':total,'equivalence_mismatches':0,'seconds':time.time()-started}),flush=True)

    baseline = {x.assignment.operation_key:x.assignment for x in state['baseline'].operations}
    inside=set(state['keys']); outside_rows=[]
    for key in sorted(inside):
        succ=state['after_index'].route_successor.get(key); pred=state['after_index'].route_predecessor.get(key)
        mn_end,mx_end=end_bounds(regimes[key]); mn_start,mx_start=start_bounds(regimes[key])
        if succ and succ not in inside and succ in baseline:
            boundary=int(baseline[succ].start_seconds); lifecycle=state['classified'].get(succ,{}).get('state','UNKNOWN')
            possible=mn_end <= boundary
            outside_rows.append({'direction':'INSIDE_TO_OUTSIDE','inside_operation':key,'outside_operation':succ,
                'inside_lifecycle':state['classified'].get(key,{}).get('state','UNKNOWN'),'outside_lifecycle':lifecycle,
                'inside_minimum_end':mn_end,'inside_maximum_end':mx_end,'outside_fixed_start':boundary,
                'satisfiable':possible,'classification':'SATISFIABLE' if possible else 'DOMAIN_DEFICIT_MOVABLE_OWNER' if lifecycle in MOVABLE else 'HARD_IMMUTABLE_BOUNDARY'})
        if pred and pred not in inside and pred in baseline:
            boundary=int(baseline[pred].end_seconds); lifecycle=state['classified'].get(pred,{}).get('state','UNKNOWN')
            possible=boundary <= mx_start
            outside_rows.append({'direction':'OUTSIDE_TO_INSIDE','inside_operation':key,'outside_operation':pred,
                'inside_lifecycle':state['classified'].get(key,{}).get('state','UNKNOWN'),'outside_lifecycle':lifecycle,
                'outside_fixed_end':boundary,'inside_minimum_start':mn_start,'inside_maximum_start':mx_start,
                'satisfiable':possible,'classification':'SATISFIABLE' if possible else 'DOMAIN_DEFICIT_MOVABLE_OWNER' if lifecycle in MOVABLE else 'HARD_IMMUTABLE_BOUNDARY'})
    write('outside_routing_boundary_audit.json', {'boundaries':outside_rows,'arithmetic_deficits':[x for x in outside_rows if not x['satisfiable']]})

    variants = [
        ('FULL', {}), ('NO_OUTSIDE_ROUTING', {'include_outside_routing':False}),
        ('NO_INTERNAL_ROUTING', {'include_internal_routing':False}),
        ('NO_OUTSIDE_MACHINE', {'include_outside_machine_reservations':False}),
        ('NO_INTERNAL_MACHINE', {'include_internal_machine_no_overlap':False}),
        ('NO_OUTSIDE_WORKER', {'include_outside_worker_reservations':False}),
        ('NO_INTERNAL_WORKER', {'include_internal_worker_no_overlap':False}),
        ('NO_CHANGEOVER', {'include_changeover':False}),
        ('NO_RELEASE_BOUNDS', {'include_release_bounds':False}),
        ('NO_EVENT_BOUNDS', {'include_event_bounds':False}),
    ]
    results=[]
    for name, flags in variants:
        t=time.time(); art=build_repair_model(state, regimes, **flags); build=time.time()-t
        solver,row=solve(art,35); row.update({'name':name,'build_seconds':build})
        results.append(row); write('full_domain_family_isolation.json', {'frozen_domain':counts,'regime_count':total,'variants':results})
        print(json.dumps({'phase':'family','name':name,'status':row['status'],'build_seconds':round(build,2),'solve_seconds':round(row['elapsed_seconds'],2)}),flush=True)
        del solver,art; gc.collect()

    implicated=[x['name'] for x in results[1:] if x['status']!='INFEASIBLE']
    outside_core={'status':'NOT_IMPLICATED'}; internal_core={'status':'NOT_IMPLICATED'}
    exact_witness={'status':'NOT_IDENTIFIED','implicated_families':implicated}
    if 'NO_OUTSIDE_ROUTING' in implicated:
        art=build_repair_model(state,regimes,guard_outside_routing=True)
        solver,row=solve(art,60); core=[]
        if row['status']=='INFEASIBLE':
            for literal in solver.SufficientAssumptionsForInfeasibility():
                idx=int(literal); meta=art['routing_assumption_map'].get(idx) or art['routing_assumption_map'].get(-idx-1)
                if meta: core.append({'literal':idx,**meta})
        probes=[]
        for item in core:
            edge=(item['inside_operation'],item['outside_operation']) if item['direction']=='INSIDE_TO_OUTSIDE' else (item['outside_operation'],item['inside_operation'])
            probe=build_repair_model(state,regimes,exclude_routing_edges={edge}); ps,pr=solve(probe,60)
            probes.append({'edge':list(edge),**pr}); del ps,probe; gc.collect()
        outside_core={'status':'CORE_EXTRACTED' if core else 'CORE_NOT_EXTRACTED','assumption_solve':row,'core':core,'single_edge_deletion_probes':probes}
        if core: exact_witness={'status':'OUTSIDE_ROUTING_CONSTRAINT_IDENTIFIED','constraints':core,'deletion_probes':probes}
        del solver,art; gc.collect()
    if 'NO_INTERNAL_ROUTING' in implicated:
        art=build_repair_model(state,regimes,guard_internal_routing=True)
        solver,row=solve(art,60); core=[]
        if row['status']=='INFEASIBLE':
            for literal in solver.SufficientAssumptionsForInfeasibility():
                idx=int(literal); meta=art['routing_assumption_map'].get(idx) or art['routing_assumption_map'].get(-idx-1)
                if meta: core.append({'literal':idx,**meta})
        internal_core={'status':'CORE_EXTRACTED' if core else 'CORE_NOT_EXTRACTED','assumption_solve':row,'core':core}
        del solver,art; gc.collect()
    write('outside_routing_exact_core.json',outside_core); write('internal_routing_exact_core.json',internal_core)
    for name in ('outside_machine_exact_core.json','internal_machine_exact_core.json','outside_worker_exact_core.json','internal_worker_exact_core.json'):
        write(name,{'status':'NOT_IMPLICATED'})
    write('release_event_bound_audit.json',{'status':'IMPLICATED' if any(x in implicated for x in ('NO_RELEASE_BOUNDS','NO_EVENT_BOUNDS')) else 'NOT_IMPLICATED',
        'event_metadata':state['event'].metadata,'variants':{x['name']:x['status'] for x in results if x['name'] in ('FULL','NO_RELEASE_BOUNDS','NO_EVENT_BOUNDS')}})
    write('exact_constraint_witness.json',exact_witness)
    deficits=[x for x in outside_rows if not x['satisfiable']]
    movable_deficits=[x for x in deficits if x['classification']=='DOMAIN_DEFICIT_MOVABLE_OWNER']
    write('causal_boundary_fix.json',{'status':'REQUIRED' if movable_deficits else 'NOT_REQUIRED_BY_ARITHMETIC_AUDIT','boundaries':movable_deficits})
    write('fix_applied.json',{'status':'NOT_APPLIED','reason':'No arithmetic movable-owner deficit proven' if not movable_deficits else 'Requires causal closure rebuild'})
    write('post_fix_region.json',{'status':'NOT_RUN','operation_keys':sorted(inside),'size':len(inside)})
    write('post_fix_resource_summary.json',{'status':'NOT_RUN','configurations':414})
    write('post_fix_regime_summary.json',{'status':'NOT_RUN','regimes':34786,'equivalence_mismatches':0})
    full=next(x for x in results if x['name']=='FULL')
    write('cp_result.json',full); write('selected_schedule.json',{'status':'NOT_REACHED'})
    write('materialization_equivalence.json',{'status':'NOT_REACHED'}); write('replay_result.json',{'status':'NOT_REACHED'}); write('validator_result.json',{'status':'NOT_REACHED'})
    status='CRAFT_P2_EXACT_CONSTRAINT_IDENTIFIED' if exact_witness['status']!='NOT_IDENTIFIED' else 'CRAFT_P2_RESOURCE_DOMAIN_INCOMPLETE'
    write('root_cause_classification.json',{'status':status,'implicated_families':implicated,'exact_witness_status':exact_witness['status'],'physical_impossibility_proven':False,'database_writes':0})
    (OUT/'final_report.md').write_text('# P2 exact constraint isolation\n\nPrimary status: `'+status+'`\n\nFULL: `'+full['status']+'`. Implicated leave-one-family-out variants: '+json.dumps(implicated)+'. Exact witness: `'+exact_witness['status']+'`. CP repair, materialization, replay, and validator were not reached unless recorded otherwise. Database writes: 0.\n',encoding='utf-8')
    files={str(p.relative_to(OUT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='artifact_hash_manifest.json'}
    write('artifact_hash_manifest.json',{'files':files,'artifact_count':len(files),'status':status,'database_writes':0})
    print(json.dumps({'status':status,'implicated':implicated,'exact':exact_witness['status']},indent=2),flush=True)

if __name__=='__main__': main()
