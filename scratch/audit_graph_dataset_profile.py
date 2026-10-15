"""Build the production graph for one database and persist exact structural/label counts."""
import argparse, json, os, sys
from pathlib import Path
import numpy as np
import torch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def stats(x):
    a=np.asarray(x,dtype=float).reshape(-1)
    if not len(a): return {"count":0}
    return {"count":int(len(a)),"min":float(a.min()),"q25":float(np.quantile(a,.25)),
      "median":float(np.median(a)),"mean":float(a.mean()),"q75":float(np.quantile(a,.75)),
      "max":float(a.max())}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("database"); ap.add_argument("--output",required=True)
    a=ap.parse_args(); os.environ["DB_NAME"]=a.database
    from graph.graph_builder import FactoryGraphBuilder
    b=FactoryGraphBuilder(); g,labels=b.build_graph()
    edges={str(k):int(g[k].edge_index.shape[1]) for k in g.edge_types}
    dv=labels["delay_valid"].detach().cpu().numpy().astype(bool)
    cv=labels["completion_valid"].detach().cpu().numpy().astype(bool)
    result={"database":a.database,"graph_fingerprint":b.graph_fingerprint(),
      "schema":b.feature_schema,"breakdown_scale":float(b.breakdown_scale),
      "nodes":{k:int(g[k].x.shape[0]) for k in g.node_types},"feature_widths":{k:int(g[k].x.shape[1]) for k in g.node_types},
      "edges":edges,"precedence_edges":edges.get("('operation', 'precedes', 'operation')",0),
      "contention_edges":edges.get("('operation', 'contends_with', 'operation')",0),
      "machine_operation_edges":edges.get("('machine', 'operates', 'operation')",0),
      "worker_operation_edges":edges.get("('worker', 'staffs', 'operation')",0),
      "split":{"train_operations":int(g["operation"].train_mask.sum()),"validation_operations":int(g["operation"].val_mask.sum()),
        "train_orders":int(len(set(b.order_ids_per_node[g["operation"].train_mask.cpu().numpy()]))),
        "validation_orders":int(len(set(b.order_ids_per_node[g["operation"].val_mask.cpu().numpy()])))},
      "label_meta":b.label_meta,
      "delay":{"valid":int(dv.sum()),"masked":int((~dv).sum()),"positive":int(labels["delay"][dv].sum()),"prevalence":float(labels["delay"][dv].float().mean()) if dv.any() else None},
      "completion":{"valid":int(cv.sum()),"masked":int((~cv).sum()),"distribution_minutes":stats(labels["completion"][cv].detach().cpu().numpy())},
      "priority":{"distribution":stats(labels["priority"].detach().cpu().numpy())}}
    p=Path(a.output);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(result,indent=2,default=str),encoding="utf-8")
    print(json.dumps(result,indent=2,default=str))
if __name__=="__main__":main()
