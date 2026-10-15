import random
from ga.operators import custom_mutate, custom_crossover

def args(): return dict(num_ops=3, eligibility_map={'1_1':[1,2],'1_2':[1,2],'1_3':[1,2]}, machines_per_line_map={'1_1':{1:2,2:2},'1_2':{1:2,2:2},'1_3':{1:2,2:2}}, op_work={'1_1':1,'1_2':1,'1_3':1}, op_optype={'1_1':'x','1_2':'x','1_3':'x'}, op_sequence_map={'1_1':1,'1_2':2,'1_3':3})
def test_mutation_trace_has_raw_and_repair_without_rng_change():
 a=['1_1','1_2','1_3',1,1,1,1,1,1]; b=list(a); events=[]; random.seed(4); custom_mutate(a,indpb=.8,trace_callback=events.append,**args()); state_a=random.getstate(); random.seed(4); custom_mutate(b,indpb=.8,**args()); assert a==b and state_a==random.getstate(); assert events and set(events[0]) >= {'before','raw_after_operator','final_after_repair','operator_diff','repair_diff'}
def test_crossover_trace_preserves_rng_and_children():
 a=['1_1','1_2','1_3',1,1,1,1,1,1]; b=['1_3','1_2','1_1',2,2,2,2,2,2]; x,y=list(a),list(b); events=[]; random.seed(7); custom_crossover(a,b,trace_callback=events.append,**args()); state=random.getstate(); random.seed(7); custom_crossover(x,y,**args()); assert (a,b)==(x,y) and state==random.getstate() and len(events)==2
