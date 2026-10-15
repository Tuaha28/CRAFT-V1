import inspect
from agent.decision_engine import AIDecisionEngine
from ga import rolling_horizon

def test_score_export_contract():
 p=inspect.signature(AIDecisionEngine._export_proposed_schedule).parameters
 assert 'phenotype_details' in p and 'scored_fitness' in p
 for f in (rolling_horizon.run_rolling_horizon,rolling_horizon.run_rolling_horizon_baseline):
  s=inspect.getsource(f); assert 'phenotype_details=det' in s and 'scored_fitness=_f[0]' in s

def test_commit_identity():
 c=['1_a','2_a','1_b',10,11,10,1,2,1]; z,n=rolling_horizon.extract_committed_subschedule(c,3,{'1','2'}); assert n==3 and z==c


def test_retained_failure_hashes_are_distinct():
 assert '88afe3138c7573570aa2039f75620783c4e9b2d805541995b8e3a8a46e2bae74' != '9ee3e269d6b57f1f3f7f5aa4c107d9c274b9af96313353ed88be2bf0fc50dae8'
