import torch
import torch.nn as nn
import math
from torch_geometric.nn import SAGEConv, to_hetero
import sys
import os

# Add root to path for imports when running as a standalone script
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.logger import get_logger
from utils.config import GNN_HIDDEN_CHANNELS, GNN_DROPOUT
from utils.machine_taxonomy import NUM_OPERATION_TYPES

logger = get_logger("gnn_model", "gnn_training.log")

# Head 2's label lives at raw-minutes scale (mean ~896, up to ~2975) --
# training the Huber loss directly at that scale, alongside delay's BCE
# and priority's MSE-on-[0,1], needs SOME rescaling to keep the three
# heads' losses/gradients comparable. This constant is that rescale,
# shared with models/train.py's loss computation so the two stay in sync.
# The model's PUBLIC output (out['completion']) is still always raw
# minutes -- see MultiTaskFactoryGNN.forward()'s final multiply -- so
# every existing consumer (models/evaluate.py, agent/decision_engine.py --
# models/inference.py was a dead, never-imported duplicate of
# decision_engine.py's inference path and was deleted during the
# model-module audit, see chat) is completely unaffected; only the internal
# training-time scale changed. See PROJECT_STATUS.txt Section 0H.13/0H.14
# for why this replaced the previous `0.001 * completion_loss` weight,
# which (measured, not assumed) crushed this head's gradient into the
# shared encoder to ~1/1000th of the other two heads', every epoch.
COMPLETION_SCALE_MINUTES = 1000.0


def prior_corrected_delay_probabilities(delay_logits, pos_weight):
    """Convert weighted-BCE logits into population delay probabilities.

    BCEWithLogitsLoss(pos_weight=w) learns a posterior tilted toward the
    positive class. Undo that known training tilt analytically:
    logit(p) = logit(q) - log(w). This changes calibration, not ranking.
    """
    w = float(pos_weight)
    if not math.isfinite(w) or w <= 0:
        raise ValueError('delay pos_weight must be finite and positive')
    return torch.sigmoid(delay_logits - math.log(w))


class SharedGraphEncoder(nn.Module):
    """
    3-Layer GraphSAGE message-passing network.
    Aggregates information across operation precedence, machine capabilities, 
    and worker assignment topologies.
    """
    def __init__(self, hidden_channels, dropout=0.3):
        super().__init__()
        self.conv1 = SAGEConv((-1, -1), hidden_channels)
        self.conv2 = SAGEConv((hidden_channels, hidden_channels), hidden_channels)
        self.conv3 = SAGEConv((hidden_channels, hidden_channels), hidden_channels)
        self.dropout_layer = nn.Dropout(p=dropout)

    def forward(self, x, edge_index):
        x = self.conv1(x, edge_index).relu()
        x = self.dropout_layer(x)
        
        x = self.conv2(x, edge_index).relu()
        x = self.dropout_layer(x)
        
        x = self.conv3(x, edge_index)
        return x

def node_input_dimensions_from_graph(hetero_graph) -> dict:
    """Return validated node-feature widths from a HeteroData graph."""
    required = ("machine", "worker", "operation")
    missing = [name for name in required if name not in hetero_graph.node_types]
    if missing:
        raise ValueError(f"Graph missing node types: {missing}")
    dimensions = {
        name: int(hetero_graph[name].x.shape[1])
        for name in required
    }
    if any(value <= 0 for value in dimensions.values()):
        raise ValueError(f"Invalid node feature dimensions: {dimensions}")
    return dimensions


class MultiTaskFactoryGNN(nn.Module):
    """
    SPLIT-ENCODER ARCHITECTURE (adopted 2026-08-23, replacing the single
    shared-encoder design this class used before). Delay gets its own
    dedicated (projection + 3-layer SAGEConv) branch; completion and
    priority continue sharing one branch, since no evidence was ever found
    that they conflict with each other (if anything, Option B's
    mean_breakdown_count_norm feature helping both unexpectedly suggested
    the opposite). This was a genuinely open question, not a predicted
    outcome, going in: two pieces of this project's history were AT MOST
    consistent with delay's gradient interfering with the old shared
    backbone (Option B's original feature-level tradeoff; 0H.14's
    "weak task degrades shared backbone" finding, measured under the OLD,
    since-fixed label) -- but 0H.14 was stale evidence, and the shared
    architecture's own fair-fight run (models/gnn.py's OLD design) had
    JUST beaten RandomForest on completion, which argued against strong
    ongoing interference. Settled by controlled experiment, not argument:
    identical features/data/loss/epoch-budget as the old shared design,
    the split tested across TWO independent seeds (see chat/
    DELAY_LABEL_FIX_PROPOSAL.md) both showed every one of the three heads
    improving over the shared baseline -- delay AP +0.009/+0.013, completion
    R2 +0.012/+0.024, priority R2 +0.032/+0.006 (seed 123 / seed 42
    respectively). Full replication, not a single-run artifact -- see
    DELAY_LABEL_FIX_PROPOSAL.md for the numbers this promotion is based on.

    Cost: this doubles the encoder's message-passing compute (two full
    3-layer SAGEConv passes over the graph instead of one) -- the dominant
    cost, given 17.7M disjunctive edges. Everything else (input feature
    construction, the op_type embedding table, per-head architecture/
    activations, loss functions/weights in models/train.py) is unchanged
    from the shared-encoder design.
    """
    def __init__(self, metadata, hidden_channels=GNN_HIDDEN_CHANNELS, dropout=GNN_DROPOUT,
                 operation_in_dim=None, op_type_embed_dim=4,
                 machine_in_dim=5, worker_in_dim=4):
        super().__init__()
        if operation_in_dim is None:
            raise ValueError('operation_in_dim must be supplied from the graph/checkpoint feature schema')
        dimensions = {
            "machine": machine_in_dim,
            "worker": worker_in_dim,
            "operation": operation_in_dim,
        }
        if any(
            not isinstance(value, int) or value <= 0
            for value in dimensions.values()
        ):
            raise ValueError(f"Invalid node feature dimensions: {dimensions}")
        self.input_dimensions = dimensions.copy()
        logger.info("Initializing Multi-Task Heterogeneous GNN (split encoder: delay branch | completion+priority branch)...")

        # 1. Input Projections for Heterogeneous Node Types
        # Explicit dimensions default to legacy machine=5, worker=4; v3 derives them from its graph.
        # Operation width is explicitly supplied from the persisted schema: sam_norm, qty_norm,
        # machine_parallelism_norm (is_running/is_delayed/lot_delay_frac were
        # removed: all three leaked the delay label directly, being derived
        # from the same execution_status column the label used to come
        # from), 3 more from the Section 0E causal-feature work —
        # deadline_budget_norm, mean_breakdown_count_norm,
        # mean_queue_delay_norm — sourced from information available BEFORE
        # each lot executed, never from its own outcome, routing_step_norm/
        # steps_remaining_norm (graph-module review step 3 -- how far into
        # its own order's routing this operation sits, now a real variable
        # quantity since routing length varies 6-10 steps by garment type),
        # PLUS pool_occupancy_norm (model/feature audit step 3 -- how many
        # other operations of this operation's own type actually shared a
        # physical machine with it, a structural real-contention COUNT,
        # distinct from mean_queue_delay_norm's historical AVERAGE wait
        # time; see graph/node_features.py::_compute_pool_occupancy for why
        # it's built from the leak-free per-lot assignment data rather than
        # planned-time overlap).
        # WAS 9 once before too, briefly, including frac_dispatched_
        # employee_on_leave -- REMOVED (model/feature audit step 1, see
        # chat). Two independent methods on the live database confirmed
        # genuine dead weight, not a measurement artifact: real variance
        # (71.2% of operations nonzero, 88 distinct values, std 0.073) but
        # ~zero permutation importance on the trained model across all
        # three heads (delay 0.0001+-0.0004, completion -0.0009+-0.0009,
        # priority 0.0002+-0.0005). Back to 9 with pool_occupancy_norm's
        # addition -- coincidence of counts, not a reversal.
        # decisive_lot_breakdown_count_norm replaced an original
        # frac_dispatched_machine_down (an instantaneous "is the machine down
        # right now" flag), measured to be identically 0.0 for every
        # operation: this seeder's free-time bookkeeping means a broken
        # machine can never be re-dispatched to before its repair check
        # resets it to 'Active'. decisive_lot_breakdown_count_norm instead
        # reads that machine's PRIOR breakdown-count history, which
        # genuinely varies.
        # See graph/node_features.py and PROJECT_STATUS.txt Section 0E /
        # reversal 8.
        #
        # 10th feature, mean_breakdown_count_norm (delay-label fix trade-off
        # decision, option (b) -- see DELAY_LABEL_FIX_PROPOSAL.md's
        # "IMPLEMENTATION STATUS"): the decisive-lot swap above was required
        # for delay's MAX-based label but cost completion (a SPAN across all
        # ~19 lots, not single-lot-driven) real signal -- a same-data RF
        # ablation measured completion R2 dropping ~0.06-0.11 when the old
        # mean-based breakdown feature (42.6% of completion's RF importance)
        # was swapped for the decisive-lot one (only 9.5%). Rather than force
        # one feature slot to serve both heads' different aggregation needs,
        # the mean-based version is reinstated ADDITIONALLY, alongside (not
        # instead of) the decisive-lot column -- each head's own
        # message-passing learns which of the two it actually needs; delay's
        # decisive-lot signal is untouched by this addition.
        #
        # PLUS a learned operation_type embedding (op_type_embed_dim wide,
        # 4 by default), concatenated onto those 10 continuous features
        # before projection -- see forward(). An embedding, not one-hot,
        # per instruction: a one-hot's width is fixed at construction, an
        # embedding table can grow (new rows) if a 6th canonical type is
        # ever added without reshaping every other one-hot-keyed input in
        # the codebase. Index count comes from
        # utils/machine_taxonomy.py::NUM_OPERATION_TYPES (currently 6: the
        # 5 real types plus one reserved "unknown" bucket) -- the single
        # shared vocabulary, not a second independently-computed one.
        #
        # operation_in_dim is a parameter (not just hardcoded 10) so the
        # Section 0F three-way comparison (label-fix-only 3-dim vs.
        # dead-feature 7-dim vs. fixed 7-dim, all under the corrected
        # order-level split) can vary ONLY the operation feature content
        # while sharing one graph/edges/labels/split -- every production
        # caller still gets 10, the current real continuous-feature count,
        # by default.
        # Shared node-feature construction: both branches below see the
        # IDENTICAL input (same op_type embedding table, same continuous
        # features) -- only their own projection + encoder WEIGHTS differ.
        # This is what isolates "does a separate encoder help" as the only
        # variable the split architecture actually tests.
        # Both branches backpropagate through this shared trainable embedding.
        self.op_type_embedding = nn.Embedding(NUM_OPERATION_TYPES, op_type_embed_dim)

        def _make_branch():
            lin_dict = nn.ModuleDict({
                'machine': nn.Linear(machine_in_dim, hidden_channels),
                'operation': nn.Linear(operation_in_dim + op_type_embed_dim, hidden_channels),
                'worker': nn.Linear(worker_in_dim, hidden_channels),
            })
            base_encoder = SharedGraphEncoder(hidden_channels, dropout=dropout)
            encoder = to_hetero(base_encoder, metadata, aggr='sum')
            return lin_dict, encoder

        # 2. Separate (projection + 3-layer SAGEConv) branches --
        # delay's own, and one shared by completion+priority.
        self.lin_dict_delay, self.encoder_delay = _make_branch()
        self.lin_dict_cp, self.encoder_cp = _make_branch()  # cp = completion + priority

        # 3. Multi-Task Prediction Heads (Processing Operation embeddings)
        
        # Head 1: Delay Probability (Raw Logits for BCEWithLogitsLoss stability)
        self.delay_head = nn.Sequential(
            nn.Linear(hidden_channels, 32),
            nn.ReLU(),
            nn.Dropout(p=dropout),
            nn.Linear(32, 1)
        )
        
        # Head 2: Expected Completion Time (Minutes >= 0).
        #
        # Final activation is Softplus, not ReLU (0H.13/0H.14): measured
        # directly, a plain ReLU here suffered a DEAD-UNIT COLLAPSE once
        # the loss-weight fix below gave this head a properly-sized
        # gradient -- every operation's pre-activation went negative
        # within the first ~25 epochs, a ReLU clipped there has EXACTLY
        # zero gradient, and the head never recovered for the remaining
        # 225 epochs (val loss identical to 4 decimal places the whole
        # time). Softplus is smooth and strictly positive everywhere, with
        # a gradient that only ever approaches zero, never reaches it --
        # same non-negative output contract as the ReLU it replaces, same
        # width/depth (no added capacity), just a different final
        # activation.
        self.completion_head = nn.Sequential(
            nn.Linear(hidden_channels, 32),
            nn.ReLU(),
            nn.Dropout(p=dropout),
            nn.Linear(32, 1),
            nn.Softplus()
        )
        
        # Head 3: Sequence Priority Score ([0.0 to 1.0])
        self.priority_head = nn.Sequential(
            nn.Linear(hidden_channels, 32),
            nn.ReLU(),
            nn.Dropout(p=dropout),
            nn.Linear(32, 1),
            nn.Sigmoid()
        )

    @classmethod
    def from_graph(cls, hetero_graph, **kwargs):
        """Construct using all node widths inferred from a graph artifact."""
        dimensions = node_input_dimensions_from_graph(hetero_graph)
        arguments = {
            "operation_in_dim": "operation",
            "machine_in_dim": "machine",
            "worker_in_dim": "worker",
        }
        for argument, node_name in arguments.items():
            supplied = kwargs.pop(argument, None)
            if supplied is not None and supplied != dimensions[node_name]:
                raise ValueError(
                    f"{argument} conflicts with graph feature width "
                    f"{dimensions[node_name]}"
                )
        return cls(
            metadata=hetero_graph.metadata(),
            operation_in_dim=dimensions["operation"],
            machine_in_dim=dimensions["machine"],
            worker_in_dim=dimensions["worker"],
            **kwargs,
        )

    def _project(self, lin_dict, x_dict, op_type_idx):
        # 'operation' is special-cased: its continuous features get the
        # learned operation_type embedding concatenated on BEFORE
        # projection, so lin_dict['operation'] learns from the combined
        # (continuous + type-identity) representation rather than the type
        # embedding being added post-hoc. op_type_idx is REQUIRED, not
        # optional-with-a-silent-default -- every call site was updated
        # deliberately (graph_builder.py stores it as
        # hetero_graph['operation'].type_idx, not part of x_dict, since
        # PyG's auto-built x_dict only bundles .x tensors) rather than risk
        # a caller silently getting an unset/wrong embedding.
        x_proj = {}
        for node_type, x in x_dict.items():
            if node_type not in lin_dict:
                continue
            if node_type == 'operation':
                op_type_emb = self.op_type_embedding(op_type_idx)
                x = torch.cat([x, op_type_emb], dim=-1)
            x_proj[node_type] = lin_dict[node_type](x)
        return x_proj

    def forward(self, x_dict, edge_index_dict, op_type_idx, enabled_heads=None):
        """Run the requested heads; default preserves legacy three-head output."""
        allowed = {"delay", "completion", "priority"}
        heads = allowed if enabled_heads is None else set(enabled_heads)
        if not heads or not heads.issubset(allowed):
            raise ValueError(f"Invalid enabled_heads: {enabled_heads}")
        output = {}

        if "delay" in heads:
            x_proj_delay = self._project(
                self.lin_dict_delay, x_dict, op_type_idx
            )
            emb_delay = self.encoder_delay(
                x_proj_delay, edge_index_dict
            )["operation"]
            output["delay"] = self.delay_head(emb_delay)

        if {"completion", "priority"} & heads:
            x_proj_cp = self._project(self.lin_dict_cp, x_dict, op_type_idx)
            emb_cp = self.encoder_cp(x_proj_cp, edge_index_dict)["operation"]
            if "completion" in heads:
                output["completion"] = (
                    self.completion_head(emb_cp) * COMPLETION_SCALE_MINUTES
                )
            if "priority" in heads:
                output["priority"] = self.priority_head(emb_cp)
        return output

if __name__ == "__main__":
    from graph.graph_builder import FactoryGraphBuilder
    
    print("1. Building graph from database...")
    builder = FactoryGraphBuilder()
    hetero_graph, labels = builder.build_graph()
    
    print("\n2. Initializing GNN Model...")
    model = MultiTaskFactoryGNN(metadata=hetero_graph.metadata())
    
    print("\n3. Running Forward Pass Test...")
    model.eval()
    with torch.no_grad():
        predictions = model(hetero_graph.x_dict, hetero_graph.edge_index_dict,
                             hetero_graph['operation'].type_idx)
    
    print("\n--- PREDICTION OUTPUT SHAPES ---")
    for task, tensor in predictions.items():
        print(f"Task: {task:<12} | Output Shape: {tensor.shape}")
        
    print("\n✅ Multi-Task GNN forward pass successful!")
