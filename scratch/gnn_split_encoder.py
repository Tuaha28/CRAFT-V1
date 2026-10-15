"""
Architecture EXPERIMENT, not adopted -- lives in scratch/, not models/,
until a controlled comparison against the Option B baseline decides
whether to keep it (see chat).

Rationale (open question, not a predicted outcome -- see chat for the
pushback on overclaiming this before it's measured): the production
MultiTaskFactoryGNN (models/gnn.py) runs ONE shared 3-layer SAGEConv
encoder feeding all three heads. Two pieces of this project's own
history are AT LEAST CONSISTENT WITH delay's gradient interfering with
that shared backbone (Option B's original feature-level tradeoff;
0H.14's "weak task degrades shared backbone" finding under the OLD,
broken label) -- but 0H.14 is stale evidence (pre label-fix, pre
Option B), and today's fair-fight run showed completion CURRENTLY beats
RandomForest with the shared encoder in place, which argues against
strong ongoing interference right now. This experiment tests it directly
instead of arguing about it further.

SCOPE: 2-way split, not 3-way. Delay gets its own dedicated
projection+encoder branch. Completion and priority continue sharing one
branch -- no evidence they conflict with each other (Option B's
mean_breakdown_count_norm feature helping BOTH, per the just-completed
permutation importance run, if anything suggests they benefit from
staying together). This is 2x the encoder's message-passing cost (the
dominant cost, given 17.7M disjunctive edges), not 3x -- heads themselves
are cheap and there is still only one set of them.

Everything else (input feature construction, the op_type embedding
table, per-head architecture/activations, loss functions/weights) is
IDENTICAL to models/gnn.py's MultiTaskFactoryGNN, on purpose -- the
encoder split is the only variable being tested, matching this project's
own fair-fight discipline (models/train.py's compute_losses recipe is
reused unchanged by scratch/train_split_encoder.py).
"""
import torch
import torch.nn as nn
from torch_geometric.nn import to_hetero
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.logger import get_logger
from utils.machine_taxonomy import NUM_OPERATION_TYPES
from models.gnn import SharedGraphEncoder, COMPLETION_SCALE_MINUTES

logger = get_logger("gnn_split_encoder_model", "gnn_split_encoder_training.log")


class SplitEncoderFactoryGNN(nn.Module):
    """
    Same node types/heads/activations as MultiTaskFactoryGNN. The only
    difference: TWO separate projection/graph-encoder branches instead of one.
    They share a trainable op_type_embedding. The training workflow also shares
    one optimizer, one LR scheduler, and combined-loss checkpoint selection;
    the branches are not fully parameter-isolated.
    """
    def __init__(self, metadata, hidden_channels=64, dropout=0.3,
                 operation_in_dim=10, op_type_embed_dim=4):
        super().__init__()
        logger.info("Initializing Split-Encoder Multi-Task GNN (delay branch | completion+priority branch)...")

        # Shared node-feature construction (NOT the thing being tested --
        # both branches must see the identical input to isolate the
        # encoder itself as the only variable).
        self.op_type_embedding = nn.Embedding(NUM_OPERATION_TYPES, op_type_embed_dim)

        def make_branch():
            lin_dict = nn.ModuleDict({
                'machine': nn.Linear(5, hidden_channels),
                'operation': nn.Linear(operation_in_dim + op_type_embed_dim, hidden_channels),
                'worker': nn.Linear(4, hidden_channels),
            })
            base_encoder = SharedGraphEncoder(hidden_channels, dropout=dropout)
            encoder = to_hetero(base_encoder, metadata, aggr='sum')
            return lin_dict, encoder

        self.lin_dict_delay, self.encoder_delay = make_branch()
        self.lin_dict_cp, self.encoder_cp = make_branch()  # cp = completion + priority

        self.delay_head = nn.Sequential(
            nn.Linear(hidden_channels, 32),
            nn.ReLU(),
            nn.Dropout(p=dropout),
            nn.Linear(32, 1),
        )
        self.completion_head = nn.Sequential(
            nn.Linear(hidden_channels, 32),
            nn.ReLU(),
            nn.Dropout(p=dropout),
            nn.Linear(32, 1),
            nn.Softplus(),
        )
        self.priority_head = nn.Sequential(
            nn.Linear(hidden_channels, 32),
            nn.ReLU(),
            nn.Dropout(p=dropout),
            nn.Linear(32, 1),
            nn.Sigmoid(),
        )

    def _project(self, lin_dict, x_dict, op_type_idx):
        x_proj = {}
        for node_type, x in x_dict.items():
            if node_type not in lin_dict:
                continue
            if node_type == 'operation':
                op_type_emb = self.op_type_embedding(op_type_idx)
                x = torch.cat([x, op_type_emb], dim=-1)
            x_proj[node_type] = lin_dict[node_type](x)
        return x_proj

    def forward(self, x_dict, edge_index_dict, op_type_idx):
        x_proj_delay = self._project(self.lin_dict_delay, x_dict, op_type_idx)
        emb_delay = self.encoder_delay(x_proj_delay, edge_index_dict)['operation']
        delay_logits = self.delay_head(emb_delay)

        x_proj_cp = self._project(self.lin_dict_cp, x_dict, op_type_idx)
        emb_cp = self.encoder_cp(x_proj_cp, edge_index_dict)['operation']
        completion_time = self.completion_head(emb_cp) * COMPLETION_SCALE_MINUTES
        priority_score = self.priority_head(emb_cp)

        return {
            'delay': delay_logits,
            'completion': completion_time,
            'priority': priority_score,
        }
