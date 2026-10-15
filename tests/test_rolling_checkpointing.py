from pathlib import Path
import random
import pytest
from ga.checkpointing import save, load, CheckpointError

def payload():
 return {'phase':'pre_polish','identity':{'window':1,'origin':'o','commit_size':2,'lookahead_size':2,'seed':42,'snapshot':'s'},'rolling_state':{'position':0},'ga_state':{'selected_chromosome':['a',1,1],'best_fitness':1.0,'phenotype':[{'x':1}],'logbook':[{'gen':0,'nevals':2}]},'resource_state':{'machine_free':{'1':2},'machine_last_order':{'1':'a'},'operator_reservations':{'7':[['x','y']]}}}
def test_roundtrip_rng_and_resources(tmp_path):
 random.seed(9); p=save(tmp_path,'window_001_pre_polish_checkpoint',payload()); before=random.random(); d=load(p,payload()['identity']); assert d['resource_state']==payload()['resource_state']; assert d['ga_state']['selected_chromosome']==payload()['ga_state']['selected_chromosome']; assert (tmp_path/'LATEST_VALID_CHECKPOINT.json').exists(); assert before==random.Random(9).random()
def test_corrupt_and_identity_fail_closed(tmp_path):
 p=save(tmp_path,'c',payload()); p.write_text('{}')
 with pytest.raises(CheckpointError): load(p,payload()['identity'])
 p=save(tmp_path,'d',payload())
 with pytest.raises(CheckpointError): load(p,{**payload()['identity'],'snapshot':'wrong'})
def test_post_polish_roundtrip(tmp_path):
 q=payload(); q['phase']='post_polish'; q['ga_state'].update({'polished_chromosome':['b',1,1],'polished_fitness':.5,'polish_stats':{'evaluations':1}}); p=save(tmp_path,'window_001_post_polish_checkpoint',q); assert load(p,q['identity'])['phase']=='post_polish'

from ga.checkpointing import encode_rng_state, restore_rng_state, encode_evolution, decode_evolution, CheckpointError
from deap import base, creator
import random, pytest
if not hasattr(creator,'FitnessCheckpointTest'): creator.create('FitnessCheckpointTest', base.Fitness, weights=(-1.0,))
if not hasattr(creator,'IndividualCheckpointTest'): creator.create('IndividualCheckpointTest', list, fitness=creator.FitnessCheckpointTest)
def test_rng_roundtrip_lossless():
 random.seed(123); state=encode_rng_state(); a=[random.random() for _ in range(4)]; random.random(); restore_rng_state(state); assert a==[random.random() for _ in range(4)]
def test_deap_evolution_roundtrip():
 a=creator.IndividualCheckpointTest([1,2,3]); a.fitness.values=(4.0,); b=creator.IndividualCheckpointTest([3,2,1]); b.fitness.values=(5.0,)
 e=encode_evolution([a,b],[a],a,[{'gen':1,'nevals':2}],1,2); d=decode_evolution(e); assert list(d['selected'])==[1,2,3] and d['selected'].fitness.values==(4.0,) and len(d['population'])==2
def test_bad_evolution_fails_closed():
 with pytest.raises(CheckpointError): decode_evolution({'population':[]})
