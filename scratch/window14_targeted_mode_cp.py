"""Diagnostic strict-superset crew substitution for W14 target operation."""
from __future__ import annotations
import contextlib, io, json, time
from dataclasses import replace
from pathlib import Path
import sys
sys.path.insert(0, ".")
from crafter.disruptions.benchmark import _load_state
from crafter.repair.planning import build_planning_state
from crafter.repair.calendar_regimes import CalendarRegimeCache, generate_calendar_regimes
from crafter.repair.boundary_aware_modes import generate_boundary_aware_configurations, build_boundary_availability_index, _crew_candidates
from crafter.repair.solve import run_canonical_schedule
from crafter.repair.temporal_escalation import TemporalEscalationPolicyV3
from scratch.run_window14_step4b import W14_KEYS, context, make_boundary, rows_and_provenance, quiet, OUT, write
import scratch.diagnose_window14_v3 as diag

def main():
    loaded=quiet(_load_state); route=loaded[5]; accepted,rows,_=rows_and_provenance(); boundary,facts=make_boundary(route,rows)
    state=quiet(build_planning_state,loaded,context(W14_KEYS),boundary=boundary)
    configs, funnel, stats, idx=quiet(generate_boundary_aware_configurations,state,boundary,crew_diversity=16,machine_set_cap=3,max_start_probes=4)
    target=list(configs['251_2065']); base=target[0]
    crews=_crew_candidates(state,idx,'251_2065',21,11,base.machine_ids,528572,max_crews=8)
    additions=[replace(base,worker_ids=tuple(crew),provenance=tuple(base.provenance)+('TARGETED_MATCHING',)) for crew in crews]
    configs=dict(configs); configs['251_2065']=tuple(target+additions)
    cache=CalendarRegimeCache(); regimes,summary,total,equiv=quiet(generate_calendar_regimes,state,configs,cache=cache)
    zero=sorted(k for k,v in regimes.items() if not v)
    prepared={'state':state,'initial_keys':tuple(state['keys']),'required_window_keys':tuple(state['keys']),'closure':{'iterations':[]},'resource_configurations':configs,'resource_audit':[],'resource_summaries':[],'regimes':regimes,'regime_summary':summary,'regime_count':total,'calendar_equivalence':equiv,'calendar_cache_profile':cache.snapshot(),'model':None,'timing':{},'boundary_facts':tuple(facts)}
    result=quiet(run_canonical_schedule,prepared,objective_mode=False,plan=TemporalEscalationPolicyV3().plans[1],unrestricted_final_stage=True)
    acc=result.get('acceptance',{}) or {}; candidate=bool(acc.get('selected_schedule'))
    validation=None
    if candidate:
        validation=diag._scoped_acceptance(result,state,facts,W14_KEYS)
    out={'target_operation':'251_2065','base_configuration_count':len(target),'targeted_crew_count':len(additions),'targeted_crews':[list(x.worker_ids) for x in additions],'resource_configuration_count':sum(len(v) for v in configs.values()),'calendar_regime_count':total,'zero_regime_operation_keys':zero,'cp_statuses':[x.get('status') for x in (acc.get('stages') or ())],'canonical_status':acc.get('status'),'candidate_exists':candidate,'validation':validation,'database_writes':0}
    write('targeted_mode_cp_probe.json',out)
if __name__=='__main__': main()
