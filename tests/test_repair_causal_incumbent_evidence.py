from types import SimpleNamespace

from crafter.repair.causal_closure import build_causal_repair_region
from crafter.repair.resource_configs import ResourceConfiguration


def test_closure_never_treats_an_arbitrary_candidate_as_incumbent(monkeypatch):
    monkeypatch.setattr('crafter.repair.causal_closure.refresh_event_reservations',lambda state: state)
    monkeypatch.setattr('crafter.repair.causal_closure._neighborhood',lambda *a,**k: ())
    rows=(
        {'operation_key':'inside','start_minutes':10,'end_minutes':20,
         'machine_ids':(1,), 'employee_ids':('real_worker',)},
        {'operation_key':'outside','start_minutes':12,'end_minutes':18,
         'machine_ids':(9,), 'employee_ids':('candidate_only_worker',)},
    )
    index=SimpleNamespace(by_key={x['operation_key']:x for x in rows},rows=rows,
        route_predecessor={},route_successor={})
    state={'keys':('inside',),'after_index':index,'classified':{'outside':{'state':'FUTURE_RELEASED'}},
           'event':SimpleNamespace(metadata={}), 'problem':object()}
    # The incumbent configuration is intentionally absent.  The old closure
    # incorrectly used this candidate and admitted ``outside`` on a fake
    # worker conflict.
    candidate=ResourceConfiguration('inside',1,1,1,(1,),('candidate_only_worker',),0,60)
    updated,step=build_causal_repair_region(state,{'inside':(candidate,)})
    assert updated['keys']==('inside',)
    assert step['added_operations']==[]
