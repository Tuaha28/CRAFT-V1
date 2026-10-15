"""Timing-independent deterministic disruption repair entry point."""
import time
from crafter.disruptions.benchmark import _load_state, _neighborhood
from crafter.disruptions.impact_analysis import direct_impact, propagate_impact
from crafter.disruptions.p2_release_fixpoint import _event_from_row
from crafter.disruptions.runtime.execution_state import classify_operations, FreezeParameters
from crafter.disruptions.state_update import apply_event
from crafter.disruptions.v2_execution_diagnostics import _restore_finite_machine
from crafter.lns_probe.analysis import build_incumbent_index
from crafter.lns_probe.anchored_model import outside_machine_reservations, outside_worker_reservations
from crafter.authoritative_semantics import project_instant_seconds
from .causal_closure import build_causal_repair_region, MOVABLE
from .calendar_regimes import CalendarRegimeCache, generate_calendar_regimes
from .resource_configs import generate_resource_configurations
from .cp_repair import build_repair_model
from .event_semantics import event_semantic, refresh_event_reservations


def _routing_aware_earliest(state, regimes):
    """Lower-bound each operation through the current internal route chain.

    Standalone minimum end is insufficient for closure: a downstream operation
    can have an early regime that is unreachable after an event-delayed
    predecessor.  This propagation uses the exact affine regime completion at
    the first legal start satisfying the predecessor bound.
    """
    keys=set(state['keys']); idx=state['after_index']; baseline={x.assignment.operation_key:x.assignment for x in state['baseline'].operations}
    event=state.get('event'); metadata=getattr(event,'metadata',{}) or {}
    delayed_pred=str(metadata.get('predecessor_operation_key',''))
    delayed_end=int(metadata.get('delayed_end_seconds',0) or 0)
    starts={}; ends={}; unresolved=set(keys)
    while unresolved:
        progressed=False
        for key in sorted(tuple(unresolved)):
            pred=idx.route_predecessor.get(key)
            if pred in unresolved: continue
            required=0
            if pred in keys:
                if pred not in ends: continue
                required=int(ends[pred])
            elif pred in baseline:
                required=project_instant_seconds(float(baseline[pred].end_seconds)).projected_seconds
            choices=[]
            for regime in regimes.get(key,()):
                start=max(int(regime['lo']),required)
                a,b=regime['formula'][0]
                if key==delayed_pred and delayed_end:
                    if int(a)==1:
                        start=max(start,delayed_end-int(b))
                    elif int(b)<delayed_end:
                        continue
                if start>int(regime['hi']): continue
                choices.append((int(a)*start+int(b),start))
            if choices:
                end,start=min(choices)
                starts[key]=int(start); ends[key]=int(end)
            unresolved.remove(key); progressed=True
        if not progressed: break
    return {'earliest_start':starts,'earliest_end':ends,'unresolved':sorted(unresolved)}

def _build_event_state_from_loaded(loaded, event):
    problem, operator_index, rows, _durations, op_details, index, baseline, checked, source = loaded
    direct=direct_impact(index,rows,event); impact=propagate_impact(index,direct)
    shadow=_restore_finite_machine(apply_event(problem,operator_index,event),event)
    after_index=build_incumbent_index(shadow.problem,rows,op_details)
    classified={x['operation_key']:x for x in classify_operations(rows,event.event_time_seconds,parameters=FreezeParameters(),blocked_operations=())}
    keys=tuple(sorted(direct)); neighborhood=_neighborhood(after_index,event,keys,event.metadata.get('seed',42))
    allocation_index=(operator_index if event_semantic(event)=='W2_WORKER_ABSENCE'
                      else shadow.allocation_index)
    state={'problem':shadow.problem,'operator_index':allocation_index,
           'validation_operator_index':operator_index,
           'rows':rows,'op_details':op_details,'after_index':after_index,'baseline':baseline,'checked':checked,'event':event,'keys':keys,'neighborhood':neighborhood,'source':source,'classified':classified,'impact':impact}
    return refresh_event_reservations(state)


def build_event_state(event):
    """Build the canonical in-memory repair state for any supported event."""
    return _build_event_state_from_loaded(_load_state(), event)


def build_p2_state():
    loaded = _load_state()
    problem, operator_index, rows, _durations, op_details, index, baseline, checked, source = loaded
    by_key={str(x['operation_key']):x for x in rows}
    predecessor=next(key for key in sorted(index.route_successor) if index.route_successor.get(key))
    event=_event_from_row(by_key[predecessor],'P2_PREDECESSOR_DELAY',duration_seconds=3600,predecessor=True)
    state = _build_event_state_from_loaded(loaded, event)
    # Preserve the accepted P2 seed contract: delayed predecessor plus its
    # direct successor.  Generic resource events seed from all direct owners.
    seed=set(event.operation_keys)
    for key in tuple(seed):
        succ=state['after_index'].route_successor.get(key)
        if succ: seed.add(succ)
    state['keys']=tuple(sorted(seed)); mutable=set(state['keys'])
    refresh_event_reservations(state)
    state['neighborhood']=_neighborhood(state['after_index'],event,state['keys'],event.metadata.get('seed',42))
    return state

def prepare_event_repair(event=None, *, state=None, max_closure_iterations=20,
                     max_local_operations=150, max_machine_sets=3,
                     max_worker_crews=3, progress=None):
    started=time.perf_counter()
    state = state if state is not None else build_event_state(event)
    initial=tuple(state['keys']); closure_rows=[]; audits=[]; summaries=[]
    configuration_seconds=0.0; closure_seconds=0.0; calendar_seconds=0.0
    # This cache is shared only within this prepared canonical state.  Its
    # content-addressed reservation/calendar keys prevent stale execution
    # reuse when closure changes outside reservations or an event overlay.
    calendar_cache=CalendarRegimeCache()
    for iteration in range(1,max_closure_iterations+1):
        mark=time.perf_counter()
        configs,audit,summary=generate_resource_configurations(state,max_machine_sets=max_machine_sets,max_worker_crews=max_worker_crews)
        configuration_seconds+=time.perf_counter()-mark
        if progress: progress({'phase':'closure_domain','iteration':iteration,'region':len(state['keys']),'configurations':summary['configurations']})
        audits=audit; summaries.append({'iteration':iteration,**summary})
        mark=time.perf_counter(); state,step=build_causal_repair_region(state,configs); closure_seconds+=time.perf_counter()-mark
        closure_rows.append({'iteration':iteration,**step})
        if progress: progress({'phase':'closure_step','iteration':iteration,'region':len(state['keys']),'added':len(step['added_operations'])})
        if not step['changed']: break
    mark=time.perf_counter(); configs,audit,summary=generate_resource_configurations(state,max_machine_sets=max_machine_sets,max_worker_crews=max_worker_crews); configuration_seconds+=time.perf_counter()-mark
    if progress: progress({'phase':'final_domain','region':len(state['keys']),'configurations':summary['configurations']})
    if len(state['keys']) > int(max_local_operations):
        return {'state':state,'initial_keys':initial,'closure':{'iterations':closure_rows,'added_operations':[x for it in closure_rows for x in it['added_operations']],'hard_boundaries':[x for it in closure_rows for x in it['hard_boundaries']]},'regimes':{},'regime_summary':[],'regime_count':0,'calendar_equivalence':{'checks':[],'mismatches':0,'all_equivalent':None,'status':'NOT_RUN_NONLOCAL_REGION'},'resource_configurations':configs,'resource_audit':audit,'resource_summaries':summaries+[summary],'model':None,'escalation':{'status':'CRAFT_P2_NONLOCAL_REBUILD_REQUIRED','region_size':len(state['keys']),'local_limit':int(max_local_operations)}}
    mark=time.perf_counter(); regimes,regime_summary,total,equivalence=generate_calendar_regimes(state,configs,cache=calendar_cache); calendar_seconds+=time.perf_counter()-mark
    if progress: progress({'phase':'regimes','region':len(state['keys']),'regimes':total,'mismatches':equivalence['mismatches']})
    # Exact post-regime routing closure.  A future outside neighbor is admitted
    # only when no authoritative timing regime can satisfy its frozen boundary.
    for boundary_iteration in range(1, int(max_closure_iterations) + 1):
        additions=[]; current=set(state['keys'])
        route_earliest=_routing_aware_earliest(state,regimes)
        for key in sorted(current):
            succ=state['after_index'].route_successor.get(key)
            if succ and succ not in current and regimes.get(key):
                fixed_start=int(round(float(state['after_index'].by_key[succ]['start_minutes'])*60))
                min_end=route_earliest['earliest_end'].get(key)
                lifecycle=state['classified'].get(succ,{}).get('state','UNKNOWN')
                if min_end is not None and min_end>fixed_start and lifecycle in MOVABLE:
                    additions.append({'operation_key':succ,'trigger_operation':key,'conflict_type':'ROUTE_AWARE_OUTSIDE_SUCCESSOR_DOMAIN_DEFICIT','required_min_end':min_end,'outside_start':fixed_start,'lifecycle_state':lifecycle,'added':True})
            pred=state['after_index'].route_predecessor.get(key)
            if pred and pred not in current and regimes.get(key):
                fixed_end=int(round(float(state['after_index'].by_key[pred]['end_minutes'])*60))
                latest_start=max(int(r['hi']) for r in regimes[key])
                lifecycle=state['classified'].get(pred,{}).get('state','UNKNOWN')
                if latest_start<fixed_end and lifecycle in MOVABLE:
                    additions.append({'operation_key':pred,'trigger_operation':key,'conflict_type':'OUTSIDE_PREDECESSOR_DOMAIN_DEFICIT','outside_end':fixed_end,'latest_start':latest_start,'lifecycle_state':lifecycle,'added':True})
        new={x['operation_key'] for x in additions}-current
        if not new: break
        state['keys']=tuple(sorted(current|new)); mutable=set(state['keys'])
        refresh_event_reservations(state)
        state['neighborhood']=_neighborhood(state['after_index'],state['event'],state['keys'],state['event'].metadata.get('seed',42))
        closure_rows.append({'iteration':f'boundary_{boundary_iteration}','added_operations':additions,'hard_boundaries':[],'changed':True})
        mark=time.perf_counter(); configs,audit,summary=generate_resource_configurations(state,max_machine_sets=max_machine_sets,max_worker_crews=max_worker_crews); configuration_seconds+=time.perf_counter()-mark
        mark=time.perf_counter(); state,step=build_causal_repair_region(state,configs); closure_seconds+=time.perf_counter()-mark; closure_rows.append({'iteration':f'boundary_resource_{boundary_iteration}',**step})
        mark=time.perf_counter(); configs,audit,summary=generate_resource_configurations(state,max_machine_sets=max_machine_sets,max_worker_crews=max_worker_crews); configuration_seconds+=time.perf_counter()-mark
        mark=time.perf_counter(); regimes,regime_summary,total,equivalence=generate_calendar_regimes(state,configs,cache=calendar_cache); calendar_seconds+=time.perf_counter()-mark
        if progress: progress({'phase':'boundary_regimes','iteration':boundary_iteration,'region':len(state['keys']),'regimes':total,'added':len(new)})
    # The canonical solver builds only its staged legal submodels.  Building a
    # second unused full-domain CP here can dominate runtime and is not part of
    # either feasibility or acceptance.
    return {'state':state,'initial_keys':initial,'closure':{'iterations':closure_rows,'added_operations':[x for it in closure_rows for x in it['added_operations']],'hard_boundaries':[x for it in closure_rows for x in it['hard_boundaries']]},'regimes':regimes,'regime_summary':regime_summary,'regime_count':total,'calendar_equivalence':equivalence,'resource_configurations':configs,'resource_audit':audit,'resource_summaries':summaries+[summary],'model':None,'timing':{'causal_closure_seconds':closure_seconds,'resource_configuration_seconds':configuration_seconds,'calendar_regime_seconds':calendar_seconds,'prepare_total_seconds':time.perf_counter()-started},'calendar_cache_profile':calendar_cache.snapshot()}


def run_event_repair(event=None, *, state=None, max_closure_iterations=20,
                     max_local_operations=150, max_machine_sets=3,
                     max_worker_crews=3, progress=None, solve_plan=None,
                     objective_mode=True, manager_overrides=(),
                     objective_time_limit_seconds=30):
    """Canonical event-to-validator entry point."""
    prepared=prepare_event_repair(event,state=state,
        max_closure_iterations=max_closure_iterations,max_local_operations=max_local_operations,
        max_machine_sets=max_machine_sets,max_worker_crews=max_worker_crews,progress=progress)
    from .solve import run_canonical_schedule
    kwargs={'objective_mode':objective_mode, 'manager_overrides':manager_overrides,
            'objective_time_limit_seconds':objective_time_limit_seconds}
    if solve_plan is not None:
        kwargs['plan']=solve_plan
    return run_canonical_schedule(prepared,**kwargs)


def run_p2_repair(*, max_closure_iterations=20, max_local_operations=150,
                  max_machine_sets=3, max_worker_crews=3, progress=None):
    return run_event_repair(
        state=build_p2_state(), max_closure_iterations=max_closure_iterations,
        max_local_operations=max_local_operations,
        max_machine_sets=max_machine_sets, max_worker_crews=max_worker_crews,
        progress=progress)
