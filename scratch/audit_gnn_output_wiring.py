import hashlib
import json
import sys
from pathlib import Path
import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graph.feature_schema import load_preprocessing
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from models.evaluate import prior_corrected_probabilities
from ga.fitness import evaluate_schedule, HIGH_RISK_THRESHOLD
from ga.schedule_utils import RISK_BUFFER_AT_CERTAINTY

ROOT=Path(__file__).resolve().parents[1]
checkpoint=ROOT/"outputs/checkpoints/gnn_multitask_latest.pth"
manifest=json.loads((ROOT/"outputs/split_manifest.json").read_text(encoding="utf-8"))
pre=load_preprocessing(checkpoint)
builder=FactoryGraphBuilder()
graph, labels=builder.build_graph(
    priority_cutpoints=manifest["priority_cutpoints"],
    preprocessing=pre,
)
actual_fp=builder.graph_fingerprint()
actual_sha=hashlib.sha256(checkpoint.read_bytes()).hexdigest()
if actual_fp != manifest["graph_fingerprint"]:
    raise RuntimeError(f"graph fingerprint mismatch: {actual_fp} != {manifest['graph_fingerprint']}")
if actual_sha != manifest["checkpoint_sha256"]:
    raise RuntimeError("checkpoint hash mismatch")

model=MultiTaskFactoryGNN(
    metadata=graph.metadata(),
    operation_in_dim=pre["operation_schema"]["count"],
)
model.load_state_dict(torch.load(checkpoint,map_location="cpu"))
model.eval()
with torch.no_grad():
    out=model(graph.x_dict,graph.edge_index_dict,graph["operation"].type_idx)
raw=torch.sigmoid(out["delay"]).cpu().numpy().ravel()
corr=prior_corrected_probabilities(raw,manifest["delay_pos_weight"])
completion=out["completion"].cpu().numpy().ravel()
priority=out["priority"].cpu().numpy().ravel()

keys=["1_11","2_22"]
details={
 "1_11":{"production_order_id":"1","setup_mins":15.0,"serial_processing_mins":120.0},
 "2_22":{"production_order_id":"2","setup_mins":15.0,"serial_processing_mins":120.0},
}
common=dict(
 num_ops=2,op_details=details,all_line_ids=[1],
 order_deadlines={"1":10000.0,"2":10000.0},
 order_release_minutes={"1":0.0,"2":0.0},
 machine_pools={k:{1:[101]} for k in keys},
 machine_line_map={101:1},usable_machine_count=1,present_operator_count=10,
 op_sequence_map={"1_11":1,"2_22":1},return_details=True,
)
a=keys+[1,1]+[1,1]
b=list(reversed(keys))+[1,1]+[1,1]
def score(chrom,preds):
 return evaluate_schedule(chrom,gnn_predictions=preds,**common)[1]
base={
 "1_11":{"delay":0.2,"priority":0.9,"completion":10.0},
 "2_22":{"delay":0.2,"priority":0.1,"completion":20.0},
}
delay_low={k:{**v,"delay":0.01} for k,v in base.items()}
delay_high={k:{**v,"delay":0.99} for k,v in base.items()}
completion_low={k:{**v,"completion":1.0} for k,v in base.items()}
completion_high={k:{**v,"completion":1e9} for k,v in base.items()}
priority_swapped={
 "1_11":{**base["1_11"],"priority":0.1},
 "2_22":{**base["2_22"],"priority":0.9},
}
al,ah=score(a,delay_low),score(a,delay_high)
bl,bh=score(b,delay_low),score(b,delay_high)
cl,ch=score(a,completion_low),score(a,completion_high)
pa,pb=score(a,base),score(b,base)
sa,sb=score(a,priority_swapped),score(b,priority_swapped)
schedule_fields=["makespan_minutes","order_completion_minutes","final_machine_free","final_machine_last_order"]
component_fields=["makespan","load_balance","lateness","idle_labour"]
result={
 "artifact_identity":{
   "checkpoint_sha256":actual_sha,
   "checkpoint_manifest_match":True,
   "preprocessing_checkpoint_match":pre["checkpoint_sha256"]==actual_sha,
   "graph_fingerprint":actual_fp,
   "graph_manifest_match":True,
   "operation_nodes":int(graph["operation"].num_nodes),
   "feature_widths":{k:int(graph[k].x.shape[1]) for k in ("machine","worker","operation")},
   "edge_counts":{"|".join(k):int(graph[k].edge_index.shape[1]) for k in graph.edge_types},
 },
 "current_predictions":{
   "delay_raw_weighted_posterior":{"min":float(raw.min()),"max":float(raw.max()),"mean":float(raw.mean()),"std":float(raw.std())},
   "delay_prior_corrected":{"min":float(corr.min()),"max":float(corr.max()),"mean":float(corr.mean()),"std":float(corr.std())},
   "delay_pos_weight":float(manifest["delay_pos_weight"]),
   "raw_above_export_high_risk_threshold":int((raw>=HIGH_RISK_THRESHOLD).sum()),
   "corrected_above_export_high_risk_threshold":int((corr>=HIGH_RISK_THRESHOLD).sum()),
   "risk_buffer_at_certainty":float(RISK_BUFFER_AT_CERTAINTY),
   "completion_minutes":{"min":float(completion.min()),"max":float(completion.max()),"mean":float(completion.mean())},
   "priority":{"min":float(priority.min()),"max":float(priority.max()),"mean":float(priority.mean())},
 },
 "controlled_perturbations":{
   "delay":{
     "schedule_fields_equal":all(al[k]==ah[k] for k in schedule_fields),
     "non_delay_components_equal":all(abs(al["fitness_components"][k]-ah["fitness_components"][k])<1e-12 for k in component_fields),
     "delay_penalty_low":al["fitness_components"]["delay_penalty"],
     "delay_penalty_high":ah["fitness_components"]["delay_penalty"],
     "candidate_ranking_low":"A" if al["fitness"]<bl["fitness"] else "B",
     "candidate_ranking_high":"A" if ah["fitness"]<bh["fitness"] else "B",
     "interpretation":"candidate-invariant additive term; timing buffer is zero"
   },
   "completion":{
     "entire_details_equal":cl==ch,
     "interpretation":"ignored by evaluate_schedule and omitted from production gnn_preds mapping"
   },
   "priority":{
     "fitness_original":{"A":pa["fitness"],"B":pb["fitness"]},
     "fitness_swapped":{"A":sa["fitness"],"B":sb["fitness"]},
     "preferred_original":"A" if pa["fitness"]<pb["fitness"] else "B",
     "preferred_swapped":"A" if sa["fitness"]<sb["fitness"] else "B",
     "interpretation":"candidate-dependent through position-weighted reward"
   }
 },
 "production_mapping":{
   "decision_engine_fields":["delay","priority"],
   "completion_forward_output_mapped":False,
   "delay_probability_used_by_decision_engine":"raw sigmoid q, not prior-corrected p",
 }
}
outpath=ROOT/"outputs/integration_audit/gnn_output_wiring.json"
outpath.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
print(json.dumps(result,indent=2))
