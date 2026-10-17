"""Fixed-protocol, independently scoped Nominal/Busy GNN v3 experiment."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import time

# Executed as a file; retain repository imports.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in __import__('sys').path:
    __import__('sys').path.insert(0, str(REPOSITORY_ROOT))

import joblib
import numpy as np
import psutil
import torch
import torch.nn as nn
import torch.optim as optim
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, average_precision_score, brier_score_loss, confusion_matrix, f1_score, log_loss, precision_score, recall_score, roc_auc_score
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    'nominal': ('garment_fyp_db_seed_nominal_20260916_162459', '0bbf11be-3751-4e1f-a26a-2c1d06e6d2a9'),
    'busy': ('garment_fyp_db_seed_busy_20260916_162818', '5c169e09-7c9a-41da-a3b2-531227f993db'),
}
SCHEMA_SHA = 'fa3f199877f0d74cd4b0355b3f864ecb29f6b22344c92719abd96711ceea928b'
FINGERPRINTS = {
    'train': '335448bee04755e86db6327d401e8921360e6cb9c66292af0f262874fae4de48',
    'validation': '06e44b971ec419cc4b04351fd9b41314214d1bb30e1632ce5101503f891d2b08',
    'test': 'b9fc7a4303066aa99649480a0bf1cbfd0ad93d10f33d0be7ad9a0dcdd8f82f72',
}
THRESHOLD = 0.5


def h(path): return sha256(Path(path).read_bytes()).hexdigest()


def ece(y, p, bins=10):
    total = 0.0
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        mask = ((p >= lo) & (p < hi)) if i < bins - 1 else ((p >= lo) & (p <= hi))
        if mask.any(): total += float(mask.mean() * abs(p[mask].mean() - y[mask].mean()))
    return total


def metrics(y, p, train_prevalence):
    pred = (p >= THRESHOLD).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=(0, 1)).ravel()
    prevalence = float(y.mean()); ap = float(average_precision_score(y, p))
    return {
        'operation_count': int(len(y)), 'positive_count': int(y.sum()), 'negative_count': int(len(y)-y.sum()),
        'test_prevalence': prevalence, 'training_prevalence': float(train_prevalence), 'threshold': THRESHOLD,
        'bce': float(log_loss(y, p, labels=(0,1))), 'average_precision': ap,
        'ap_prevalence_baseline': prevalence, 'ap_lift_over_prevalence': ap-prevalence,
        'roc_auc': float(roc_auc_score(y,p)), 'brier_score': float(brier_score_loss(y,p)), 'ece_10': ece(y,p),
        'accuracy': float(accuracy_score(y,pred)), 'precision': float(precision_score(y,pred,zero_division=0)),
        'recall': float(recall_score(y,pred,zero_division=0)), 'f1': float(f1_score(y,pred,zero_division=0)),
        'confusion_matrix': {'tn':int(tn),'fp':int(fp),'fn':int(fn),'tp':int(tp)},
        'probability_distribution': {'min':float(p.min()),'max':float(p.max()),'mean':float(p.mean()),'std':float(p.std())},
    }


def main(regime):
    database, expected_uuid = EXPECTED[regime]
    os.environ['DB_NAME'] = database  # before any project DB import
    from graph.feature_schema_v3 import operation_schema_v3
    from graph.graph_builder_v3 import FactoryGraphBuilderV3, order_id_hash
    from models.gnn import MultiTaskFactoryGNN, node_input_dimensions_from_graph
    from models.train_v3 import V3TrainingConfig, V3_ENABLED_HEADS, _delay_loss, _delay_metrics, set_v3_seed

    out = ROOT/'outputs'/'gnn_v3_regimes'/regime
    if (out/'final_untouched_test_evaluation.json').exists():
        raise RuntimeError(f'{regime} one-time test artifact already exists')
    out.mkdir(parents=True, exist_ok=False)
    (out/'checkpoints').mkdir()
    (out/'baselines').mkdir()
    config = V3TrainingConfig(seed=42,max_epochs=150,learning_rate=.005,weight_decay=.0001,hidden_channels=64,dropout=.3,device='cpu')
    set_v3_seed(42)
    builder = FactoryGraphBuilderV3(seed=42)
    if builder.dataset_identity['dataset_version'] != expected_uuid:
        raise RuntimeError('Dataset UUID mismatch')
    if operation_schema_v3()['sha256'] != SCHEMA_SHA:
        raise RuntimeError('Schema hash mismatch')
    train, validation = builder.build_train_validation()
    if builder.label_access.test_accessed or [x for x,_ in builder.label_access.requests] != ['train','validation']:
        raise RuntimeError('Test-label or unexpected label access before training')
    for name,bundle in (('train',train),('validation',validation)):
        if bundle.graph_fingerprint != FINGERPRINTS[name]: raise RuntimeError(f'{name} fingerprint mismatch')
        if node_input_dimensions_from_graph(bundle.graph) != {'machine':1,'worker':4,'operation':7}: raise RuntimeError('7/1/4 input mismatch')
    pre = {
        'regime':regime,'database':database,'dataset_identity':builder.dataset_identity,'seed':42,
        'training_configuration':{'enabled_heads':['delay'],'loss':'BCEWithLogitsLoss() unweighted','pos_weight':1.0,'checkpoint_selection':'minimum validation delay BCE','optimizer':'Adam','initial_lr':.005,'weight_decay':.0001,'scheduler':'ReduceLROnPlateau(min,.5,10)','hidden_channels':64,'dropout':.3,'maximum_epochs':150,'early_stopping':None},
        'schema':operation_schema_v3(), 'train_validation_label_requests':[x for x,_ in builder.label_access.requests], 'test_labels_opened':False,
        'split_graph_fingerprints':{'train':train.graph_fingerprint,'validation':validation.graph_fingerprint,'test_expected':FINGERPRINTS['test']},
        'source_sha256':{'runner':h(__file__),'train_v3':h(ROOT/'models/train_v3.py'),'gnn':h(ROOT/'models/gnn.py')},
    }
    (out/'pretraining_manifest.json').write_text(json.dumps(pre,indent=2,sort_keys=True),encoding='utf-8')

    device=torch.device('cpu'); train_graph=train.graph.to(device); val_graph=validation.graph.to(device)
    train_labels={k:v.to(device) for k,v in train.labels.items()}; val_labels={k:v.to(device) for k,v in validation.labels.items()}
    model=MultiTaskFactoryGNN.from_graph(train_graph,hidden_channels=64,dropout=.3).to(device)
    criterion=nn.BCEWithLogitsLoss(); optimizer=optim.Adam(model.parameters(),lr=.005,weight_decay=.0001)
    scheduler=optim.lr_scheduler.ReduceLROnPlateau(optimizer,mode='min',factor=.5,patience=10)
    best=float('inf'); best_epoch=None; history=[]; start=time.perf_counter(); checkpoint_path=out/'checkpoints/gnn_v3_delay_only.pth'
    for epoch in range(1,151):
        tick=time.perf_counter(); lr=optimizer.param_groups[0]['lr']; model.train(); optimizer.zero_grad()
        pred=model(train_graph.x_dict,train_graph.edge_index_dict,train_graph['operation'].type_idx,enabled_heads=V3_ENABLED_HEADS)
        loss=_delay_loss(pred,train_labels,train_graph['operation'].delay_valid_mask,criterion)
        if not torch.isfinite(loss): raise RuntimeError(f'nonfinite training loss at {epoch}')
        loss.backward(); optimizer.step(); model.eval()
        with torch.no_grad():
            val_pred=model(val_graph.x_dict,val_graph.edge_index_dict,val_graph['operation'].type_idx,enabled_heads=V3_ENABLED_HEADS)
            val_loss=_delay_loss(val_pred,val_labels,val_graph['operation'].delay_valid_mask,criterion)
        value=float(val_loss.cpu()); scheduler.step(value)
        rec={'epoch':epoch,'train_delay_bce':float(loss.detach().cpu()),'validation_delay_bce':value,'learning_rate':lr,'epoch_runtime_seconds':time.perf_counter()-tick,'rss_bytes':psutil.Process().memory_info().rss,**_delay_metrics(val_pred['delay'],val_labels,val_graph['operation'].delay_valid_mask)}
        history.append(rec); (out/'training_history.json').write_text(json.dumps(history,indent=2),encoding='utf-8')
        if value < best:
            best,best_epoch=value,epoch
            torch.save({'model_state_dict':model.state_dict(),'best_epoch':best_epoch,'validation_delay_bce':best,'input_dimensions':model.input_dimensions,'enabled_heads':V3_ENABLED_HEADS,'delay_pos_weight':1.0,'training_config':asdict(config),'regime':regime,'dataset_uuid':expected_uuid},checkpoint_path)
    if builder.label_access.test_accessed: raise RuntimeError('test opened during training')
    checkpoint_sha=h(checkpoint_path)
    summary={'best_epoch':best_epoch,'best_validation_delay_bce':best,'checkpoint_sha256':checkpoint_sha,'runtime_seconds':time.perf_counter()-start,'test_labels_opened':False}
    (out/'training_summary.json').write_text(json.dumps(summary,indent=2,sort_keys=True),encoding='utf-8')

    # Freeze validation-only baselines: exactly the Stress candidate protocol.
    tm=train_graph['operation'].delay_valid_mask.cpu().numpy().ravel(); vm=val_graph['operation'].delay_valid_mask.cpu().numpy().ravel()
    xtr=train_graph['operation'].x.cpu().numpy()[tm]; xva=val_graph['operation'].x.cpu().numpy()[vm]
    ytr=train_labels['delay'].cpu().numpy().ravel()[tm].astype(int); yva=val_labels['delay'].cpu().numpy().ravel()[vm].astype(int); train_prev=float(ytr.mean())
    scaler=StandardScaler().fit(xtr); lr_trials=[]
    for c in (.01,.1,1.,10.):
        clf=LogisticRegression(C=c,class_weight=None,solver='lbfgs',max_iter=2000,random_state=42).fit(scaler.transform(xtr),ytr)
        lr_trials.append((metrics(yva,clf.predict_proba(scaler.transform(xva))[:,1],train_prev),c,clf))
    lr_m,lr_c,lr_model=min(lr_trials,key=lambda z:z[0]['bce']); lr_path=out/'baselines/logistic_regression_selected.joblib'; joblib.dump({'scaler':scaler,'model':lr_model,'feature_count':7},lr_path)
    rf_trials=[]
    for depth,leaf in ((None,1),(16,1),(None,10),(16,10)):
        clf=RandomForestClassifier(n_estimators=300,max_depth=depth,min_samples_leaf=leaf,max_features='sqrt',class_weight=None,random_state=42,n_jobs=1).fit(xtr,ytr)
        rf_trials.append((metrics(yva,clf.predict_proba(xva)[:,1],train_prev),{'n_estimators':300,'max_depth':depth,'min_samples_leaf':leaf,'max_features':'sqrt'},clf))
    rf_m,rf_c,rf_model=min(rf_trials,key=lambda z:z[0]['bce']); rf_path=out/'baselines/random_forest_selected.joblib'; joblib.dump({'model':rf_model,'feature_count':7},rf_path)
    # Frozen checkpoint validation metrics use the selected epoch, not last epoch.
    payload=torch.load(checkpoint_path,map_location='cpu',weights_only=False); frozen=MultiTaskFactoryGNN.from_graph(train.graph,hidden_channels=64,dropout=.3); frozen.load_state_dict(payload['model_state_dict']); frozen.eval()
    with torch.no_grad(): vp=torch.sigmoid(frozen(validation.graph.x_dict,validation.graph.edge_index_dict,validation.graph['operation'].type_idx,enabled_heads=('delay',))['delay'][validation.graph['operation'].delay_valid_mask]).numpy().ravel()
    gnn_val=metrics(yva,vp,train_prev)
    baseline={'selection_rule':'minimum validation BCE only; threshold .5 fixed before test','test_labels_opened':False,'train_prevalence':train_prev,'gnn':{'checkpoint_sha256':checkpoint_sha,'best_epoch':best_epoch,'validation':gnn_val},'constant_train_prevalence':{'validation':metrics(yva,np.full(len(yva),train_prev),train_prev)},'logistic_regression':{'trials':[{'C':c,'validation':m} for m,c,_ in lr_trials],'selected_config':{'C':lr_c,'class_weight':None,'solver':'lbfgs','max_iter':2000,'random_state':42},'validation':lr_m,'sha256':h(lr_path)},'random_forest':{'trials':[{'config':c,'validation':m} for m,c,_ in rf_trials],'selected_config':rf_c,'validation':rf_m,'sha256':h(rf_path)}}
    (out/'frozen_validation_baseline_protocol.json').write_text(json.dumps(baseline,indent=2,sort_keys=True),encoding='utf-8')
    if builder.label_access.test_accessed: raise RuntimeError('test opened before frozen hashes')
    hashes={'gnn_checkpoint':h(checkpoint_path),'logistic_regression':h(lr_path),'random_forest':h(rf_path)}
    if hashes['gnn_checkpoint'] != checkpoint_sha: raise RuntimeError('checkpoint changed before test')

    # One and only one test label request, after all artifact hashes are frozen.
    test=builder.build_test_after_checkpoint(priority_cutpoints=train.label_meta['priority_cutpoints'],checkpoint_sha256=checkpoint_sha)
    if test.graph_fingerprint != FINGERPRINTS['test']: raise RuntimeError('test fingerprint mismatch')
    if [x for x,_ in builder.label_access.requests] != ['train','validation','test']: raise RuntimeError('unexpected test access sequence')
    mask=test.graph['operation'].delay_valid_mask; xt=test.graph['operation'].x.cpu().numpy()[mask.cpu().numpy().ravel()]; yt=test.labels['delay'][mask].numpy().ravel().astype(int)
    with torch.no_grad(): gp=torch.sigmoid(frozen(test.graph.x_dict,test.graph.edge_index_dict,test.graph['operation'].type_idx,enabled_heads=('delay',))['delay'][mask]).numpy().ravel()
    lp=lr_model.predict_proba(scaler.transform(xt))[:,1]; rp=rf_model.predict_proba(xt)[:,1]; cp=np.full(len(yt),train_prev)
    final={'protocol_version':1,'regime':regime,'purpose':'one-time frozen test evaluation; no refit/retune','frozen_artifact_hashes':hashes,'checkpoint_best_epoch':best_epoch,'split':{'seed':42,'test_order_count':len(test.order_ids),'test_order_ids_sha256':order_id_hash(test.order_ids),'test_graph_fingerprint':test.graph_fingerprint,'test_valid_operation_count':len(yt)},'label_access_event':{'requests':[x for x,_ in builder.label_access.requests],'test_accessed':builder.label_access.test_accessed,'sealed_checkpoint_sha256':builder.label_access._checkpoint_sha256},'runtime_seconds':time.perf_counter()-start,'test_label_access_count':sum(x=='test' for x,_ in builder.label_access.requests),'gnn':metrics(yt,gp,train_prev),'logistic_regression':metrics(yt,lp,train_prev),'random_forest':metrics(yt,rp,train_prev),'constant_train_prevalence':metrics(yt,cp,train_prev)}
    if final['test_label_access_count'] != 1: raise RuntimeError('test access count not one')
    final_path=out/'final_untouched_test_evaluation.json'; final_path.write_text(json.dumps(final,indent=2,sort_keys=True),encoding='utf-8'); (out/'final_untouched_test_evaluation.sha256').write_text(h(final_path)+'\n',encoding='ascii')
    print(json.dumps({'regime':regime,'summary':summary,'baseline':{'lr':lr_c,'rf':rf_c},'test':final,'evaluation_sha256':h(final_path)},sort_keys=True))

if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('regime',choices=('nominal','busy')); args=parser.parse_args(); main(args.regime)
