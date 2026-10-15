from datetime import datetime, timezone
from types import SimpleNamespace

from crafter.repair.calendar_regimes import _execution, generate_calendar_regimes
from crafter.disruptions.events import DisruptionEvent
from crafter.repair.event_semantics import refresh_event_reservations, with_m2_capacity_contract
from crafter.repair.resource_configs import ResourceConfiguration


def test_m3_and_w2_are_authoritative_reservations_not_ad_hoc_cp_rules(monkeypatch):
    monkeypatch.setattr('crafter.repair.event_semantics.outside_machine_reservations',lambda *a:{})
    monkeypatch.setattr('crafter.repair.event_semantics.outside_worker_reservations',lambda *a:{})
    origin=datetime(2026,1,1,tzinfo=timezone.utc)
    common={'problem':SimpleNamespace(origin=origin),'after_index':object(),'keys':('a',)}
    m3={**common,'event':SimpleNamespace(event_id='M3',machine_ids=(7,),worker_ids=(),event_time_seconds=12,end_time_seconds=18,metadata={'repair_semantics':'M3_TEMPORARY_MACHINE_OUTAGE'})}
    refresh_event_reservations(m3)
    assert m3['_machine_res'][7] == [(12,18,'__EVENT__','event:M3')]
    w2={**common,'event':SimpleNamespace(event_id='W2',machine_ids=(),worker_ids=('9',),event_time_seconds=20,end_time_seconds=30,metadata={'repair_semantics':'W2_WORKER_ABSENCE'})}
    refresh_event_reservations(w2)
    assert [(x-origin).total_seconds() for x in w2['_worker_res']['9'][0]] == [20,30]


def _calendar_state(semantic, *, machine_ids=(), worker_ids=(), start=1200, end=1800):
    origin=datetime(2026,1,5,6)
    operation=SimpleNamespace(order_id='o')
    problem=SimpleNamespace(
        origin=origin,horizon_seconds=7200,
        operation_by_key={'op':operation},
        order_by_id={'o':SimpleNamespace(release_seconds=0)},
        resources=SimpleNamespace(fixed_machine_commitments=(),fixed_worker_commitments=()),
    )
    state={'problem':problem,'after_index':SimpleNamespace(
               by_key={'op':{'start_minutes':0}},route_predecessor={},rows=()),
           'operator_index':{},'keys':('op',),
           'event':SimpleNamespace(event_id=semantic,machine_ids=machine_ids,
               worker_ids=worker_ids,event_time_seconds=start,end_time_seconds=end,
               metadata={'repair_semantics':semantic})}
    refresh_event_reservations(state)
    config=ResourceConfiguration('op',1,1,1,tuple(machine_ids or (7,)),
        tuple(str(x) for x in (worker_ids or ('9',))),0,600,(),10.0)
    return state,config


def _formula_segments(regime,start):
    f=regime['formula']
    return tuple((int(f[1+2*i][0])*start+int(f[1+2*i][1]),
                  int(f[2+2*i][0])*start+int(f[2+2*i][1]))
                 for i in range(int(regime['segments'])))


def test_m3_outage_is_consumed_by_calendar_regimes_and_machine_returns():
    state,config=_calendar_state('M3_TEMPORARY_MACHINE_OUTAGE',machine_ids=(7,))
    regimes,summary,total,equivalence=generate_calendar_regimes(state,{'op':(config,)})
    assert total and equivalence['mismatches']==0
    for regime in regimes['op']:
        for start in {int(regime['lo']),int(regime['hi']),(int(regime['lo'])+int(regime['hi']))//2}:
            end=int(regime['formula'][0][0])*start+int(regime['formula'][0][1])
            assert not (start < 1800 and end > 1200)
    assert any(int(r['lo']) <= 1800 <= int(r['hi']) for r in regimes['op'])


def test_w2_absence_is_consumed_by_productive_segments_and_worker_returns():
    state,config=_calendar_state('W2_WORKER_ABSENCE',worker_ids=('9',))
    # Authoritative execution pauses at the absence and resumes afterward.
    end,segments=_execution(state,config,900)
    assert segments == ((900,1200),(1800,2100))
    assert end == 2100
    regimes,summary,total,equivalence=generate_calendar_regimes(state,{'op':(config,)})
    assert total and equivalence['mismatches']==0
    for regime in regimes['op']:
        for start in {int(regime['lo']),int(regime['hi']),(int(regime['lo'])+int(regime['hi']))//2}:
            assert all(not (a < 1800 and b > 1200) for a,b in _formula_segments(regime,start))
    assert any(int(r['lo']) <= 0 <= int(r['hi']) for r in regimes['op'])
    assert any(int(r['lo']) <= 1800 <= int(r['hi']) for r in regimes['op'])


def test_m2_contract_is_exact_screened_k_not_incumbent_heuristic(monkeypatch):
    op=SimpleNamespace(operation_key='target')
    problem=SimpleNamespace(operation_by_key={'target':op})
    row={'operation_key':'target','line_id':52,'machine_ids':(891,1049,1479)}
    loaded=(problem,{},(row,),{}, {},object(),None,None,None)
    monkeypatch.setattr('crafter.disruptions.benchmark._candidate_domains',
        lambda *a,**k:{'target':({'line_id':52,'k':1},{'line_id':52,'k':2},
                                  {'line_id':52,'k':3},{'line_id':53,'k':3})})
    monkeypatch.setattr('crafter.lns_probe.line_k_model._available_machine_pool',
        lambda *a,**k:(891,1049,1479))
    event=DisruptionEvent('M2','MACHINE_BREAKDOWN',0,0,machine_ids=(891,),
        duration_seconds=1800,metadata={'repair_semantics':'M2_CAPACITY_REDUCTION',
                                       'target_operation_key':'target'})
    contracted=with_m2_capacity_contract(loaded,event)
    assert contracted.metadata['capacity_line_id']==52
    assert contracted.metadata['capacity_incumbent_k']==3
    assert contracted.metadata['capacity_legal_k_values']==[1,2]
    assert contracted.metadata['capacity_max_k']==2
    assert contracted.metadata['unavailable_machine_ids']==[891]
    assert contracted.metadata['remaining_same_line_machine_ids']==[1049,1479]
