"""Event-driven causal closure independent of exact-time modes."""
from crafter.disruptions.benchmark import _neighborhood
from crafter.lns_probe.anchored_model import outside_machine_reservations, outside_worker_reservations
from .event_semantics import refresh_event_reservations

MOVABLE={'FUTURE_RELEASED','FUTURE_PLANNED','FUTURE','BLOCKED'}
IMMUTABLE={'COMPLETED','IN_PROGRESS','PAUSED'}

def _overlap(a,b,c,d): return int(a)<int(d) and int(c)<int(b)

def build_causal_repair_region(state, configurations):
    keys=set(state.get('keys',())); added=[]; hard=[]
    md=getattr(state.get('event'),'metadata',{}) or {}; delayed=str(md.get('predecessor_operation_key','')); delayed_end=int(md.get('delayed_end_seconds',0) or 0)
    candidates=set()
    for key in sorted(keys):
        row=state['after_index'].by_key[key]; start=int(round(float(row['start_minutes'])*60)); end=int(round(float(row['end_minutes'])*60))
        if state['after_index'].route_predecessor.get(key)==delayed and start<delayed_end:
            end += delayed_end-start; start=delayed_end
        pred=state['after_index'].route_predecessor.get(key); succ=state['after_index'].route_successor.get(key)
        if pred and pred not in keys:
            prow=state['after_index'].by_key[pred]; pend=int(round(float(prow['end_minutes'])*60))
            if pend>start: candidates.add((pred,key,'ROUTING_PREDECESSOR',None))
        if succ and succ not in keys:
            srow=state['after_index'].by_key[succ]; sstart=int(round(float(srow['start_minutes'])*60))
            if end>sstart: candidates.add((succ,key,'ROUTING_SUCCESSOR',None))
        incumbent_machines=tuple(sorted(int(x) for x in row.get('machine_ids',())))
        incumbent_workers=tuple(sorted(str(x) for x in row.get('employee_ids',())))
        # Causal admission must be proven by the authoritative incumbent
        # execution state.  A generated fallback configuration is a CP
        # alternative, not evidence that this operation currently occupies
        # its workers or machines; using one here manufactures conflicts
        # between arbitrary candidate crews.
        mids=set(incumbent_machines)
        wids=set(incumbent_workers)
        for other in state['after_index'].rows:
            owner=str(other['operation_key'])
            if owner in keys or owner==key: continue
            os=int(round(float(other['start_minutes'])*60)); oe=int(round(float(other['end_minutes'])*60))
            if not _overlap(start,end,os,oe): continue
            shared_m=mids & {int(x) for x in other.get('machine_ids',())}
            if shared_m: candidates.add((owner,key,'MACHINE_CONFLICT',str(min(shared_m))))
            shared_w=wids & {str(x) for x in other.get('employee_ids',())}
            if shared_w: candidates.add((owner,key,'WORKER_CONFLICT',str(min(shared_w))))
    for owner,trigger,reason,resource in sorted(candidates):
        lifecycle=state.get('classified',{}).get(owner,{}).get('state','UNKNOWN')
        row={'operation_key':owner,'trigger_operation':trigger,'conflict_type':reason,'resource_id':resource,'lifecycle_state':lifecycle}
        if lifecycle in MOVABLE:
            keys.add(owner); row['added']=True; added.append(row)
        else:
            row['added']=False; row['reason']='IMMUTABLE_BOUNDARY' if lifecycle in IMMUTABLE else 'POLICY_BOUNDARY'; hard.append(row)
    state['keys']=tuple(sorted(keys)); mutable=set(keys)
    refresh_event_reservations(state)
    state['neighborhood']=_neighborhood(state['after_index'],state['event'],state['keys'],getattr(state['event'],'metadata',{}).get('seed',42))
    return state, {'added_operations':added,'hard_boundaries':hard,'changed':bool(added)}
