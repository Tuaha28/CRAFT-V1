"""Real dataframe feature builders feed a full heterogeneous forward pass."""
import json
import pandas as pd
import pytest
import torch
from torch_geometric.data import HeteroData
from graph.node_features import build_machine_node_features, build_worker_node_features, build_operation_node_features
from graph.feature_schema import operation_schema, save_preprocessing, load_preprocessing
from models.gnn import MultiTaskFactoryGNN, prior_corrected_delay_probabilities


def test_real_features_forward_and_checkpoint_contract(tmp_path):
    machines = pd.DataFrame([{'asset_id': 1, 'current_status': 'Active', 'max_rpm': 4000}])
    workers = pd.DataFrame([{'worker_id': 1, 'status': 'Present', 'efficiency_score': 80, 'skill_breadth': 1}])
    routings = pd.DataFrame([{'production_order_id': '1', 'operation_id': '1',
        'sam': 3., 'target_quantity': 100, 'order_deadline': pd.Timestamp('2026-09-20'),
        'release_date': pd.Timestamp('2026-09-10'), 'operation_sequence_number': 1,
        'required_machine_type': 'single-needle lockstitch'}])
    data = HeteroData()
    data['machine'].x, _ = build_machine_node_features(machines)
    data['worker'].x, _ = build_worker_node_features(workers)
    data['operation'].x, _, _, types = build_operation_node_features(routings, pd.DataFrame(), breakdown_scale=2.)
    schema = operation_schema()
    assert data['operation'].x.shape[1] == schema['count'] == 12
    for edge in [('machine','operates','operation'), ('worker','staffs','operation'),
                 ('operation','runs_on','machine'), ('operation','staffed_by','worker'),
                 ('operation','precedes','operation')]:
        data[edge].edge_index = torch.tensor([[0],[0]], dtype=torch.long)
    model = MultiTaskFactoryGNN(data.metadata(), hidden_channels=8, operation_in_dim=schema['count']).eval()
    with torch.no_grad():
        output = model(data.x_dict, data.edge_index_dict, types)
    assert all(v.shape == (1,1) and torch.isfinite(v).all() for v in output.values())
    checkpoint = tmp_path/'model.pth'
    torch.save(model.state_dict(), checkpoint)
    save_preprocessing(checkpoint, schema, 2., 3.)
    loaded_preprocessing = load_preprocessing(checkpoint)
    assert loaded_preprocessing['operation_schema'] == schema
    assert loaded_preprocessing['delay_pos_weight'] == 3.
    checkpoint.write_bytes(b'changed')
    with pytest.raises(ValueError, match='does not belong'):
        load_preprocessing(checkpoint)


def test_missing_dimension_is_explicit_error():
    with pytest.raises(ValueError, match='must be supplied'):
        MultiTaskFactoryGNN(([], []))


def test_weighted_delay_logit_is_prior_corrected_for_inference():
    # q=0.5 is the exact constant optimum under inverse-prevalence weighting.
    # Undoing w=3 must recover p=q/(w*(1-q)+q)=0.25.
    corrected = prior_corrected_delay_probabilities(torch.tensor([[0.0]]), 3.0)
    assert corrected.item() == pytest.approx(0.25)
