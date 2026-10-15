"""Compact exact CalendarExecutionRegime prototype for P2."""
from __future__ import annotations
import hashlib, json, math
from collections import defaultdict
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from ortools.sat.python import cp_model
from crafter.disruptions.p2_core_guided_prototype import _prepare_state
from crafter.disruptions.benchmark import _candidate_domains, _neighborhood
from crafter.lns_probe.exact_machine_model import generate_machine_set_candidates
from crafter.lns_probe.joint_modes import generate_joint_modes
from crafter.disruptions.p2_final_repair_attempt import _config_key
from crafter.lns_probe.anchored_model import outside_machine_reservations, outside_worker_reservations
from crafter.lns_probe.joint_modes import _fixed_crew_execution
from crafter.lns_probe.joint_modes import JointExecutableMode
from crafter.lns_probe.run_e1_7_final_verified import _phenotype, _replay_mode
from crafter.lns_probe.transaction import _machine_accepts
from crafter.authoritative_semantics import authoritative_changeover_seconds
from crafter.phenotype import validate

OUT=Path('outputs/crafter/p2_compressed_timing_repair_v2')

def _write(p,obj): p.write_text(json.dumps(obj,indent=2,default=str),encoding='utf-8')
def _sec(problem,dt): return int(round((dt-problem.origin).total_seconds()))
def _sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()

def _execution(state, mode, start):
    dt=state['problem'].origin+timedelta(seconds=int(start+mode.setup_seconds))
    end_dt,segs=_fixed_crew_execution(state['problem'],state['operator_index'],mode.worker_ids,dt,float(mode.productive_minutes),state['_worker_res'])
    end=_sec(state['problem'],end_dt)
    return end,tuple((_sec(state['problem'],a),_sec(state['problem'],b)) for a,b in segs)

def _halo_mode(state, key, reservations):
    """Create one authoritative physical incumbent configuration for a causal halo op.

    Halo operations are not alternatives invented by this prototype: their line,
    machine IDs, crew, setup and productive work are copied from the validated
    incumbent row, while timing is subsequently owned by a CalendarExecutionRegime.
    """
    row=state['after_index'].by_key[key]; op=state['problem'].operation_by_key[key]
    workers=tuple(sorted(str(v) for v in row.get('employee_ids', ())))
    machines=tuple(sorted(int(v) for v in row.get('machine_ids', ())))
    start=int(round(float(row.get('start_minutes',0))*60)); setup=int(round(float(row.get('setup_minutes',op.setup_seconds/60))*60))
    prod_min=float(row.get('productive_minutes',op.serial_productive_seconds/60)); prod=int(round(prod_min*60))
    # Use the authoritative worker walk only to populate the incumbent reference;
    # regime generation will recompute it at each candidate start.
    try:
        end_dt,segs=_fixed_crew_execution(state['problem'],state['operator_index'],workers,
            state['problem'].origin+timedelta(seconds=start+setup),prod_min,reservations)
        end=_sec(state['problem'],end_dt)
        segs2=tuple((_sec(state['problem'],a),_sec(state['problem'],b)) for a,b in segs)
    except Exception:
        end=start+setup+prod; segs2=()
    return JointExecutableMode(
        mode_id=f'{key}|HALO_INCUMBENT', operation_key=key,
        factory_id=int(state['problem'].line_by_id[int(row['line_id'])].factory_id), line_id=int(row['line_id']),
        machine_count=len(machines), machine_ids=machines, worker_ids=workers,
        start_seconds=start, productive_start_seconds=start+setup,
        setup_seconds=setup, productive_seconds=prod, productive_minutes=prod_min,
        end_seconds=end, elapsed_seconds=end-start, pause_seconds=max(0,end-start-setup-prod),
        worker_segments=segs2, incoming_boundary=(), outgoing_boundary=(),
        provenance=('BOUNDARY_HALO_INCUMBENT',), incumbent=True,
        machine_occupancy_start_seconds=start, machine_occupancy_end_seconds=end,
        recovery_level='INCUMBENT_PRESERVATION', recovery_reason='BOUNDARY_HALO', source_event_id=None)

def _add_boundary_halo(state):
    """Add a bounded causal routing halo until no new future neighbor appears."""
    keys=set(state['keys']); added=[]; hard=[]; examined=set(); frontier=set(keys)
    # Resource-boundary owners are discovered from the CURRENT physical modes,
    # never from historical relaxed-model witnesses.
    current_machine_ids={int(mid) for k in keys for m in state['modes'].get(k,()) for mid in m.machine_ids}
    current_worker_ids={str(w) for k in keys for m in state['modes'].get(k,()) for w in m.worker_ids}
    provisional_mr=outside_machine_reservations(state['problem'],state['after_index'],keys)
    provisional_wr=outside_worker_reservations(state['problem'],state['after_index'],keys)
    resource_owners=set()
    for mid in current_machine_ids:
        for _a,_b,_order,owner in provisional_mr.get(mid,()):
            if owner in state['after_index'].by_key: resource_owners.add(owner)
    for row in state['after_index'].rows:
        owner=str(row['operation_key'])
        if owner in keys: continue
        row_workers={str(w) for w in row.get('employee_ids',())}
        if row_workers & current_worker_ids: resource_owners.add(owner)
    for depth in range(3):
      new_frontier=set()
      for key in sorted(frontier):
        if key in examined: continue
        examined.add(key)
        for neighbor in (state['after_index'].route_predecessor.get(key), state['after_index'].route_successor.get(key)):
            if not neighbor or neighbor in keys: continue
            info=state['classified'].get(neighbor,{})
            st=info.get('state','UNKNOWN')
            if st in ('COMPLETED','IN_PROGRESS','PAUSED'):
                hard.append({'operation_key':neighbor,'state':st,'reason':'IMMUTABLE_BOUNDARY'})
                continue
            if st not in ('FUTURE_RELEASED','FUTURE_PLANNED','FUTURE','BLOCKED'):
                hard.append({'operation_key':neighbor,'state':st,'reason':'POLICY_BOUNDARY'})
                continue
            keys.add(neighbor); new_frontier.add(neighbor); added.append({'operation_key':neighbor,'reason':'OUTSIDE_ROUTING_BOUNDARY','touching_operation':key,'state':st,'depth':depth+1})
      for owner in sorted(resource_owners):
        if owner in keys: continue
        st=state['classified'].get(owner,{}).get('state','UNKNOWN')
        if st in ('COMPLETED','IN_PROGRESS','PAUSED'):
            hard.append({'operation_key':owner,'state':st,'reason':'IMMUTABLE_RESOURCE_BOUNDARY'}); continue
        if st not in ('FUTURE_RELEASED','FUTURE_PLANNED','FUTURE','BLOCKED'):
            hard.append({'operation_key':owner,'state':st,'reason':'POLICY_RESOURCE_BOUNDARY'}); continue
        keys.add(owner); new_frontier.add(owner); added.append({'operation_key':owner,'reason':'OUTSIDE_RESOURCE_BOUNDARY','state':st,'depth':depth+1})
      frontier=new_frontier
      if not frontier: break
    if not added: return state,added,hard
    keys=tuple(sorted(keys)); state['keys']=keys
    mutable=set(keys)
    state['_machine_res']=outside_machine_reservations(state['problem'],state['after_index'],mutable)
    state['_worker_res']=outside_worker_reservations(state['problem'],state['after_index'],mutable)
    state['neighborhood']=_neighborhood(state['after_index'],state['event'],keys,state['event'].metadata.get('seed',42))
    state['relaxable']=tuple(sorted(set(state['relaxable']) | {a['operation_key'] for a in added}))
    domains=_candidate_domains(state['after_index'],state['problem'],keys,event=state['event'])
    machine_sets,_=generate_machine_set_candidates(state['problem'],state['after_index'],state['neighborhood'],state['relaxable'],domains,max_sets=3,machine_pool_limit=30)
    full_modes,failures,counts=generate_joint_modes(state['problem'],state['op_details'],state['operator_index'],state['after_index'],state['neighborhood'],state['relaxable'],domains,machine_sets,max_starts=10,event=state['event'])
    state['modes']=full_modes
    state['domain_failures']=failures; state['domain_counts']=counts
    return state,added,hard

def _affine(samples):
    # samples [(x, values)] and values are segment endpoint vectors.
    x0,v0=samples[0]; x1,v1=samples[1]
    slopes=[]; intercept=[]
    for a,b in zip(v0,v1):
        d=x1-x0; slope=(b-a)/d if d else 0
        if abs(slope-round(slope))>1e-9 or round(slope) not in (0,1): return None
        s=int(round(slope)); slopes.append(s); intercept.append(int(a-s*x0))
    for x,v in samples[2:]:
        if any(s*x+c!=int(y) for s,c,y in zip(slopes,intercept,v)): return None
    return tuple(zip(slopes,intercept))

def _breakpoints(state,key,mode,lower):
    p=state['problem']; horizon=int(p.horizon_seconds); out={int(lower),horizon-int(mode.setup_seconds+mode.productive_seconds)}
    for m in state['modes'].get(key,()): out.add(int(m.start_seconds))
    for mid in mode.machine_ids:
        for a,b,_,_ in state['_machine_res'].get(int(mid),()): out.update((max(int(lower),int(a)),min(horizon,int(b))))
    for wid in mode.worker_ids:
        for a,b in state['_worker_res'].get(str(wid),()): out.update((_sec(p,a),_sec(p,b)))
    # Shift boundaries adjacent to existing timing evidence.
    for x in list(out):
        d=p.origin+timedelta(seconds=int(x)); day=d.replace(hour=0,minute=0,second=0,microsecond=0)
        for h in (6,14,22):
            q=_sec(p,day+timedelta(hours=h))
            for z in (q-int(mode.setup_seconds),q-int(mode.setup_seconds)-1,q-int(mode.setup_seconds)+1):
                if lower<=z<=horizon-int(mode.setup_seconds+mode.productive_seconds): out.add(z)
    return sorted(x for x in out if lower<=x<=horizon-int(mode.setup_seconds))

def _generate_regimes(state):
    regimes={}; rows=[]; total=0
    for key in sorted(state['keys']):
        uniq={}
        for m in state['modes'].get(key,()): uniq.setdefault(_config_key(m),m)
        regs=[]
        for config,mode in sorted(uniq.items(),key=lambda x:str(x[0])):
            lower=0
            pred=state['after_index'].route_predecessor.get(key)
            ev=state.get('event')
            delayed=str(ev.metadata.get('predecessor_operation_key','')) if ev is not None else ''
            if ev is not None and pred==delayed:
                lower=int(ev.metadata.get('delayed_end_seconds',0))
            bp=_breakpoints(state,key,mode,lower)
            for a,b0 in zip(bp,bp[1:]):
                b=b0
                if b<a: continue
                xs=sorted(set([a,b,(a+b)//2,a+1 if a<b else a,b-1 if b>a else b]))
                vals=[]; ok=True
                for x in xs:
                    try: e,s=_execution(state,mode,x)
                    except Exception: ok=False; break
                    if e>int(state['problem'].horizon_seconds) or not _machine_accepts(state['_machine_res'].get(int(mode.machine_ids[0]),()),x,e,state['problem'].operation_by_key[key].order_id): ok=False; break
                    vals.append((x,(e,)+tuple(v for pair in s for v in pair)))
                if not ok or not vals: continue
                af=_affine(vals)
                if af is None: continue
                # Require exact equivalence at all sampled points and retain
                # the authoritative segment count/pattern.
                nseg=(len(vals[0][1])-1)//2
                regs.append({'key':key,'config':config,'base':mode,'lo':a,'base_lo':0,'hi':b,'formula':af,'segments':nseg})
        # Deduplicate identical regimes.
        seen=set(); clean=[]
        for r in regs:
            sig=(r['config'],r['lo'],r['hi'],r['formula'])
            if sig not in seen: seen.add(sig); clean.append(r)
        regimes[key]=clean; total+=len(clean)
        rows.append({'operation_key':key,'physical_configurations':len(uniq),'regime_count':len(clean),'status':'OK' if clean else 'ZERO_REGIME_DOMAIN'})
    return regimes,rows,total

def _build(state,regimes, *, include_outside=True, include_reservations=True,
           include_routing=True, include_machine_internal=True,
           include_worker_internal=True, include_changeover=True):
    p=state['problem']; idx=state['after_index']; keys=sorted(state['keys']); hor=int(p.horizon_seconds)
    model=cp_model.CpModel(); starts={}; ends={}; cfg_lit={}; reg_lit={}; reg_info={}; seg_info={}
    machine_int=defaultdict(list); worker_int=defaultdict(list); optional=0
    for key in keys:
        rs=regimes[key]; starts[key]=model.NewIntVar(0,hor,f'cr_start_{key}'); ends[key]=model.NewIntVar(0,hor,f'cr_end_{key}')
        rl=[]; cfgs={}
        for i,r in enumerate(rs):
            l=model.NewBoolVar(f'cr_reg_{key}_{i}'); rl.append(l); reg_info[(key,i)]=r
            cfgs.setdefault(r['config'],[]).append(l)
            model.Add(starts[key]>=r['lo']).OnlyEnforceIf(l); model.Add(starts[key]<=r['hi']).OnlyEnforceIf(l)
            formula=r['formula']; model.Add(ends[key]==formula[0][0]*starts[key]+formula[0][1]).OnlyEnforceIf(l)
            segs=[]; base=r['base']
            for si in range(r['segments']):
                sl=model.NewBoolVar(f'cr_seg_{key}_{i}_{si}'); model.Add(sl==l)
                st=model.NewIntVar(0,hor,f'cr_ss_{key}_{i}_{si}'); en=model.NewIntVar(0,hor,f'cr_se_{key}_{i}_{si}')
                fs=formula[1+2*si]; fe=formula[1+2*si+1]
                model.Add(st==fs[0]*starts[key]+fs[1]).OnlyEnforceIf(l); model.Add(en==fe[0]*starts[key]+fe[1]).OnlyEnforceIf(l)
                seg_size=model.NewIntVar(0,hor,f'cr_ws_{key}_{i}_{si}')
                model.Add(seg_size==en-st)
                for wid in base.worker_ids:
                    worker_int[str(wid)].append(model.NewOptionalIntervalVar(st,seg_size,en,sl,f'cr_w_{key}_{i}_{si}_{wid}')); optional+=1
                segs.append({'start':st,'end':en,'lit':sl})
            seg_info[(key,i)]=segs
            for mid in base.machine_ids:
                mach_size=model.NewIntVar(0,hor,f'cr_ms_{key}_{i}_{mid}')
                model.Add(mach_size==ends[key]-starts[key])
                machine_int[int(mid)].append(model.NewOptionalIntervalVar(starts[key],mach_size,ends[key],l,f'cr_m_{key}_{i}_{mid}')); optional+=1
        model.AddExactlyOne(rl); reg_lit[key]=rl
        for cfg,ls in cfgs.items():
            c=model.NewBoolVar(f'cr_cfg_{key}_{len(cfg_lit.get(key,{}))}'); model.Add(sum(ls)==c); cfg_lit.setdefault(key,{})[cfg]=c
        if not rs: raise ValueError(f'zero regime domain {key}')
    if include_routing:
        for key in keys:
            succ=idx.route_successor.get(key); pred=idx.route_predecessor.get(key)
            if succ in keys: model.Add(ends[key]<=starts[succ])
            elif include_outside and succ is not None:
                a=next((x.assignment for x in state['baseline'].operations if x.assignment.operation_key==succ),None)
                if a is not None: model.Add(ends[key]<=int(a.start_seconds))
            if pred not in keys and pred is not None and include_outside:
                a=next((x.assignment for x in state['baseline'].operations if x.assignment.operation_key==pred),None)
                if a is not None: model.Add(int(a.end_seconds)<=starts[key])
    if include_reservations:
        for mid,rows in state['_machine_res'].items():
            for i,(a,b,_,_) in enumerate(rows):
                if b>a: machine_int[int(mid)].append(model.NewFixedSizeIntervalVar(int(a),int(b-a),f'cr_fm_{mid}_{i}'))
        for wid,rows in state['_worker_res'].items():
            for i,(a,b) in enumerate(rows):
                s=_sec(p,a);e=_sec(p,b)
                if e>s: worker_int[str(wid)].append(model.NewFixedSizeIntervalVar(s,e-s,f'cr_fw_{wid}_{i}'))
    if include_machine_internal:
        for vals in machine_int.values(): model.AddNoOverlap(vals)
    if include_worker_internal:
        for vals in worker_int.values(): model.AddNoOverlap(vals)
    # Sequence-dependent changeover using config-level choices and CP timing.
    if include_changeover:
        for ai,lk in enumerate(keys):
            for rk in keys[ai+1:]:
                for lc,ll in cfg_lit[lk].items():
                    for rc,rr in cfg_lit[rk].items():
                        shared=set(lc[2])&set(rc[2])
                        if not shared: continue
                        bef=model.NewBoolVar(f'cr_b_{lk}_{rk}_{len(shared)}'); aft=model.NewBoolVar(f'cr_a_{lk}_{rk}_{len(shared)}')
                        model.Add(bef+aft>=ll+rr-1); model.Add(bef<=ll);model.Add(bef<=rr);model.Add(aft<=ll);model.Add(aft<=rr)
                        ch=authoritative_changeover_seconds(lk.split('_')[0],rk.split('_')[0])
                        model.Add(ends[lk]+int(ch)<=starts[rk]).OnlyEnforceIf(bef); model.Add(ends[rk]+int(ch)<=starts[lk]).OnlyEnforceIf(aft)
    model.AddMaxEquality(model.NewIntVar(0,hor,'cr_max_end'),list(ends.values()))
    model.ClearObjective()
    return {'model':model,'starts':starts,'ends':ends,'reg_lit':reg_lit,'reg_info':reg_info,'seg_info':seg_info,'size':{'operations':len(keys),'regimes':sum(len(v) for v in regimes.values()),'variables':len(model.Proto().variables),'constraints':len(model.Proto().constraints),'optional_intervals':optional}}

def run(outdir=OUT):
    out=Path(outdir); out.mkdir(parents=True,exist_ok=True); state=_prepare_state(); mutable=set(state['relaxable'])
    state['_machine_res']=outside_machine_reservations(state['problem'],state['after_index'],mutable); state['_worker_res']=outside_worker_reservations(state['problem'],state['after_index'],mutable)
    state,halo_added,halo_hard=_add_boundary_halo(state)
    _write(out/'boundary_halo_expansion.json',{'added_operations':halo_added,'hard_boundaries':halo_hard,'database_writes':0})
    regimes,rows,total=_generate_regimes(state)
    _write(out/'calendar_regime_summary.json',{'rows':rows,'total_regimes':total,'operations':len(state['keys']),'database_writes':0})
    eq=[]
    for key,rs in regimes.items():
        for i,r in enumerate(rs):
            checks=[]
            for x in sorted(set([r['lo'],r['hi'],(r['lo']+r['hi'])//2,r['lo']+1 if r['lo']<r['hi'] else r['lo'],r['hi']-1 if r['hi']>r['lo'] else r['hi']])):
                pred_end,segs=_execution(state,r['base'],x); f=r['formula']; vals=[f[0][0]*x+f[0][1]]+[v for j in range(r['segments']) for v in (f[1+2*j][0]*x+f[1+2*j][1],f[1+2*j+1][0]*x+f[1+2*j+1][1])]
                checks.append({'start':x,'exact_end':pred_end,'predicted_end':vals[0],'exact_segments':list(segs),'predicted_segments':[vals[1+2*j:3+2*j] for j in range(r['segments'])],'equal':pred_end==vals[0] and list(segs)==[tuple(vals[1+2*j:3+2*j]) for j in range(r['segments'])]})
            eq.append({'operation_key':key,'regime':i,'checks':checks,'equivalent':all(x['equal'] for x in checks)})
    _write(out/'calendar_regime_equivalence.json',{'regimes':eq,'all_equivalent':all(x['equivalent'] for x in eq),'database_writes':0})
    boundaries=[]
    for key in sorted(state['keys']):
        pred=state['after_index'].route_predecessor.get(key); succ=state['after_index'].route_successor.get(key)
        if pred not in state['keys'] or succ not in state['keys']: boundaries.append({'operation_key':key,'outside_predecessor':pred,'outside_successor':succ})
    _write(out/'outside_routing_boundary_inventory.json',{'boundaries':boundaries,'database_writes':0})
    if not all(x['equivalent'] for x in eq):
        result={'status':'REGIME_EQUIVALENCE_FAILURE'}; _write(out/'cp_model_summary.json',{}); _write(out/'cp_result.json',result)
    else:
        art=_build(state,regimes); solver=cp_model.CpSolver(); solver.parameters.max_time_in_seconds=300; solver.parameters.num_search_workers=8; solver.parameters.randomize_search=True
        st=solver.Solve(art['model']); result={'status':solver.StatusName(st),'status_code':int(st),'wall_time_seconds':solver.WallTime(),'branches':solver.NumBranches(),'conflicts':solver.NumConflicts(),'model':art['size']}; _write(out/'cp_model_summary.json',art['size']); _write(out/'cp_result.json',result)
    _write(out/'selected_schedule.json',{'status':'NOT_RUN'}); _write(out/'cp_materialization_equivalence.json',{'status':'NOT_RUN'}); _write(out/'replay_result.json',{'status':'NOT_RUN'}); _write(out/'validator_result.json',{'status':'NOT_RUN'}); _write(out/'hard_boundary_witness.json',{'status':'NOT_PROVEN','database_writes':0})
    root='CRAFT_P2_REPAIR_UNRESOLVED' if result.get('status')!='OPTIMAL' and result.get('status')!='FEASIBLE' else 'CRAFT_P2_SEARCH_LIMIT'
    _write(out/'root_cause_classification.json',{'status':root,'database_writes':0,'physical_boundary_proven':False})
    (out/'final_report.md').write_text(f'Primary status: `{root}`. Regimes={total}; CP={result.get("status")}. No production semantics or DB state changed.\n',encoding='utf-8')
    files={str(p.relative_to(out)).replace('\\','/'):_sha(p) for p in sorted(out.rglob('*')) if p.is_file() and p.name!='artifact_hash_manifest.json'}; _write(out/'artifact_hash_manifest.json',{'files':files,'artifact_count':len(files),'status':root,'database_writes':0})
    return {'status':root,'output_dir':str(out),'regimes':total,'cp':result.get('status'),'database_writes':0}

if __name__=='__main__': print(json.dumps(run(),indent=2))
