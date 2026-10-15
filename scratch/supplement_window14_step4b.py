"""Supplemental Window-14 Step-4B probes: wider temporal scope and contention."""
from __future__ import annotations
import contextlib, io, json, sys, time
from pathlib import Path
sys.path.insert(0, '.')
from crafter.disruptions.benchmark import _load_state
from crafter.repair.planning import PlanningContextV1, build_planning_state
from crafter.repair.global_planner import WindowBoundaryState
from crafter.repair.resource_configs import generate_resource_configurations
from crafter.repair.calendar_regimes import CalendarRegimeCache, generate_calendar_regimes
from crafter.repair.temporal_escalation import TemporalEscalationPolicyV3
from crafter.repair.solve import run_canonical_schedule
from crafter.repair.resource_domain_v2 import _solve_probe
from crafter.repair.boundary_aware_modes import build_boundary_availability_index, generate_boundary_aware_configurations
from scratch.run_window14_step4b import rows_and_provenance, make_boundary, context, quiet, W14_KEYS, RUN, OUT, write

def cp_probe(state, boundary_facts, configs, label):
    cache=CalendarRegimeCache(); regimes, summary,total,equiv=quiet(generate_calendar_regimes,state,configs,cache=cache)
    zero=sorted(k for k,v in regimes.items() if not v)
    prepared={'state':state,'initial_keys':tuple(state['keys']),'required_window_keys':tuple(state['keys']),
              'closure':{'iterations':[]},'resource_configurations':configs,'resource_audit':[],
              'resource_summaries':[],'regimes':regimes,'regime_summary':summary,'regime_count':total,
              'calendar_equivalence':equiv,'calendar_cache_profile':cache.snapshot(),'model':None,
              'timing':{},'boundary_facts':tuple(boundary_facts)}
    if zero:
        return {'label':label,'status':'NOT_INVOKED_ZERO_MODE','zero_regime_operations':zero,
                'resource_configurations':sum(len(v) for v in configs.values()),'calendar_regimes':total,
                'database_writes':0}
    result=quiet(run_canonical_schedule,prepared,objective_mode=False,
                 plan=TemporalEscalationPolicyV3().plans[1],unrestricted_final_stage=True)
    acc=result.get('acceptance',{}) or {}; stages=acc.get('stages',()) or ()
    return {'label':label,'status':'CP_ATTEMPTED','cp_status':(acc.get('winning_stage') or {}).get('status'),
            'canonical_status':acc.get('status'),'candidate_exists':bool(acc.get('selected_schedule')),
            'resource_configurations':sum(len(v) for v in configs.values()),'calendar_regimes':total,
            'zero_regime_operations':zero,'stages':[{k:s.get(k) for k in ('stage','status','variables','constraints','optional_intervals','branches','conflicts','solve_seconds','model_build_seconds','window_days')} for s in stages],
            'database_writes':0}

def overlap_probes(state, regimes):
    """Diagnostic-only constraint projections; no relaxed result is accepted."""
    return {
        'exact': _solve_probe(state, regimes, seconds=30.0),
        'machine_overlap_disabled': _solve_probe(state, regimes, disable_machine=True, seconds=30.0),
        'worker_overlap_disabled': _solve_probe(state, regimes, disable_worker=True, seconds=30.0),
        'machine_worker_overlap_disabled': _solve_probe(state, regimes, disable_machine=True, disable_worker=True, seconds=30.0),
        'routing_disabled': _solve_probe(state, regimes, disable_routing=True, seconds=30.0),
    }

def main():
    loaded=quiet(_load_state); problem,_,_,_,_,route,_,_,_=loaded
    accepted,rows,prov=rows_and_provenance(); boundary,facts=make_boundary(route,rows)
    state=quiet(build_planning_state,loaded,context(W14_KEYS,'window14-step4b-supplement'),boundary=boundary)
    probes=[]; contention={}; overlap=None
    for k in (1,4,8,16):
        configs, funnel, stats, idx=quiet(generate_boundary_aware_configurations,state,boundary,crew_diversity=k,machine_set_cap=3,max_start_probes=4)
        probes.append(cp_probe(state,facts,configs,f'BOUNDARY_AWARE_K{k}_UNRESTRICTED'))
        if k==16:
            # Reuse the exact K=16 regimes for a causal diagnostic.  These
            # relaxed projections are never accepted as schedules.
            cache=CalendarRegimeCache(); regimes, summary,total,equiv=quiet(generate_calendar_regimes,state,configs,cache=cache)
            overlap=overlap_probes(state, regimes)
            machine={}; worker={}; op_machine={}; op_worker={}
            for key, values in configs.items():
                for cfg in values:
                    for mid in cfg.machine_ids:
                        machine.setdefault(str(mid),set()).add(str(key)); op_machine.setdefault(str(key),set()).add(str(mid))
                    for wid in cfg.worker_ids:
                        worker.setdefault(str(wid),set()).add(str(key)); op_worker.setdefault(str(key),set()).add(str(wid))
            skill={}
            for key in W14_KEYS:
                cap=str(problem.operation_by_key[key].required_capability)
                skill.setdefault(cap,[]).append(key)
            contention={'top_machine_candidate_contention':[{'machine_id':k,'operation_count':len(v),'operation_keys':sorted(v)} for k,v in sorted(machine.items(),key=lambda x:(-len(x[1]),x[0]))[:50]],
                        'top_worker_candidate_contention':[{'worker_id':k,'operation_count':len(v),'operation_keys':sorted(v)} for k,v in sorted(worker.items(),key=lambda x:(-len(x[1]),x[0]))[:50]],
                        'operation_machine_choice_counts':{k:len(v) for k,v in sorted(op_machine.items())},
                        'operation_worker_choice_counts':{k:len(v) for k,v in sorted(op_worker.items())},
                        'skill_operation_counts':{k:len(v) for k,v in sorted(skill.items())},
                        'crew_diversity':16}
    write('temporal_cp_probes.json',{'probes':probes,'database_writes':0})
    write('boundary_constraint_probes.json',{'k16': overlap,'diagnostic_only':True,'database_writes':0})
    write('resource_contention.json', {**contention,'database_writes':0})

if __name__=='__main__': main()
