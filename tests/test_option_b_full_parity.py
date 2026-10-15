import random, numpy as np
from ga.fitness import evaluate_schedule
from test_alns_refinement import make_scheduler

def run(flag):
 random.seed(777); np.random.seed(777); s=make_scheduler(option_b_logging=flag)
 sols, log=s.optimize(pop_size=6,generations=2,n_solutions=1,random_seed=42)
 ind,fit=sols[0]; f,d=evaluate_schedule(ind,s.num_ops,s.op_details,s.gnn_predictions,all_line_ids=s.active_line_ids,order_deadlines=s.order_deadlines_minutes,machine_pools=s.factory.machine_ids_per_line_map,machine_line_map=s.machine_line_map,usable_machine_count=s.usable_machine_count,present_operator_count=10,op_sequence_map=s.seq_lookup,initial_machine_free=s.initial_machine_free,initial_machine_last_order=s.initial_machine_last_order,op_optype=s.factory.optype_for_op,protected_machine_type=s.protected_machine_type,base_time=s.now,return_details=True)
 return [list(ind),fit,[dict(x) for x in log],d['phenotype_hash'],random.getstate(),np.random.get_state(),s.option_b_events]
def test_full_ga_logging_parity():
 a=run(False); b=run(True); assert a[0:4]==b[0:4]; assert repr(a[4])==repr(b[4]); assert repr(a[5])==repr(b[5]); assert b[6]
