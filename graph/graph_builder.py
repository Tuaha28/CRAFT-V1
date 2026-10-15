import hashlib
import torch
import numpy as np
from torch_geometric.data import HeteroData
from database.queries import (
    fetch_machine_state,
    fetch_active_orders_and_routings,
    fetch_current_shop_schedule,
    fetch_worker_state,
    fetch_operation_assignments,
    fetch_actual_vs_planned_timing,
    fetch_dispatch_context,
)
from graph.node_features import build_machine_node_features, build_operation_node_features, build_worker_node_features
from graph.edge_builder import build_precedence_edges, build_assignment_edges_into_operations, build_disjunctive_edges
from graph.labels import extract_gnn_labels
from utils.logger import get_logger

logger = get_logger("graph_builder", "gnn_training.log")

# Fixed seed so the same operation nodes land in train vs. val every time
# this graph is rebuilt (training, evaluation, and inference all call
# build_graph() independently against a live, changing DB — without a fixed
# seed the split wouldn't even be stable within a single day).
SPLIT_SEED = 42
VAL_FRACTION = 0.2


def _build_split_masks(order_ids, val_fraction: float = VAL_FRACTION, seed: int = SPLIT_SEED):
    """
    Reproducible ORDER-level train/val split for transductive GNN evaluation.
    IMPORTANT: without any split at all, train_gnn() and evaluate_gnn() both
    operate on 100% of the same nodes — evaluate_gnn() was previously
    reporting training performance, not generalization performance.

    order_ids: array-like, length num_nodes, giving each operation node's
        production_order_id (aligned to node index — same order as
        merged_ops_df/op_map, see the call site in build_graph()).

    FIXED from an earlier per-OPERATION split (a flat
    rng.permutation(num_nodes) with no grouping at all): precedence edges
    connect operations WITHIN one order only (routing step i -> i+1, see
    graph/edge_builder.py::build_precedence_edges), so an operation-level
    split let a validation node sit directly downstream, via message
    passing, of a same-order TRAINING node the model was optimised to fit.
    Measured against the real order/operation structure before this fix
    (PROJECT_STATUS.txt Section 0F): 66.3% of orders had operations split
    across both sides, and 72.8% of validation nodes had direct
    precedence-edge exposure to a same-order training sibling. Every
    operation of one order also shares an IDENTICAL deadline_budget_norm
    feature by construction (graph/node_features.py), compounding it.

    Splitting by unique production_order_id first, then assigning every
    operation of a chosen order to the SAME side, closes this: an order's
    operations are never split across train and val, so a validation node's
    same-order neighbours (if any exist in the graph at all) are always also
    validation nodes -- the precedence-edge leakage channel has nothing to
    carry across the boundary.
    """
    order_ids = np.asarray(order_ids)
    num_nodes = len(order_ids)
    unique_orders = np.unique(order_ids)
    rng = np.random.default_rng(seed)
    perm = rng.permutation(len(unique_orders))
    val_size = max(1, int(len(unique_orders) * val_fraction)) if len(unique_orders) > 0 else 0
    val_orders = unique_orders[perm[:val_size]]

    is_val = np.isin(order_ids, val_orders) if num_nodes > 0 else np.zeros(0, dtype=bool)
    val_mask = torch.from_numpy(is_val.copy())
    train_mask = torch.from_numpy(~is_val.copy()) if num_nodes > 0 else torch.zeros(0, dtype=torch.bool)
    return train_mask, val_mask

class FactoryGraphBuilder:
    def __init__(self, *, ownership_snapshot=None, legacy_mode=None):
        self.ownership_snapshot = ownership_snapshot
        self.legacy_mode = legacy_mode
        logger.info("Initializing FactoryGraphBuilder...")
        self.df_machines = fetch_machine_state()
        self.df_workers = fetch_worker_state() # Added Worker Data Fetch
        self.df_routings = fetch_active_orders_and_routings()
        self.df_schedule = fetch_current_shop_schedule()
        # Leak-free, all-lots assignments — feeds BOTH the parallelism feature
        # and the machine/worker -> operation edges. See the LEAKAGE notes in
        # database/queries.py::fetch_operation_assignments.
        self.df_assignments = fetch_operation_assignments()
        # LABEL-ONLY. Feeds extract_gnn_labels() exclusively — never merged
        # into node features (see fetch_actual_vs_planned_timing's docstring).
        self.df_actual_vs_planned = fetch_actual_vs_planned_timing()
        # FEATURE-ONLY, the mirror image of the above: machine/employee/queue
        # conditions as of each lot's dispatch, feeding operation node
        # features exclusively — never the label. See
        # database.queries.fetch_dispatch_context and PROJECT_STATUS.txt
        # Section 0E.
        self.df_dispatch_context = fetch_dispatch_context()
        self.data = HeteroData()

    def build_graph(self, priority_cutpoints=None, breakdown_scale=None, preprocessing=None) -> tuple:
        """
        priority_cutpoints: an already-frozen slack->percentile curve from a
        training run's split manifest. Evaluation and inference pass this so
        Head 3's labels are scored against exactly the curve training used,
        instead of re-deriving a new one from whatever split this rebuild
        happens to produce. None (the default) derives a fresh curve from
        this build's own training split -- correct for a training run, and
        what every non-label caller (which discards labels anyway) gets.
        """
        from graph.feature_schema import operation_schema, validate_schema
        self.feature_schema = operation_schema()
        if preprocessing is not None:
            validate_schema(preprocessing['operation_schema'])
            breakdown_scale = preprocessing['breakdown_scale']
        from utils.factory_ownership import resolve_ownership
        self.ownership_snapshot = resolve_ownership(self.df_routings['production_order_id'], self.ownership_snapshot, legacy_mode=self.legacy_mode)
        logger.info("Building Node Feature Matrices...")
        
        # 1. Build Node Tensors
        mach_x, mach_map = build_machine_node_features(self.df_machines)
        self.data['machine'].x = mach_x
        
        worker_x, worker_map = build_worker_node_features(self.df_workers)
        self.data['worker'].x = worker_x
        
        # breakdown_scale: derived from the TRAIN split at build time and
        # persisted in the split manifest (audit #13), so evaluation and
        # inference normalise on the training run's scale instead of
        # refitting per batch. None derives a fresh one.
        op_x, op_map, merged_ops_df, op_type_idx = build_operation_node_features(
            self.df_routings, self.df_schedule, self.df_assignments,
            self.df_dispatch_context, breakdown_scale=breakdown_scale,
        )
        self.breakdown_scale = getattr(
            build_operation_node_features, '_last_scale', breakdown_scale)
        if op_x.shape[1] != self.feature_schema['count']:
            raise ValueError('Feature builder output width differs from its declared schema')
        self.data['operation'].x = op_x
        # Operation type as an integer index, NOT part of .x -- consumed as
        # a learned nn.Embedding lookup (models/gnn.py), not a continuous
        # feature. Not part of PyG's auto-built x_dict, so callers must
        # pass it into the model explicitly alongside x_dict/edge_index_dict.
        self.data['operation'].type_idx = op_type_idx
        self.op_map = op_map

        logger.info("Building Edge Indices...")

        # 2. Build Edge Tensors
        prec_edge = build_precedence_edges(merged_ops_df, op_map)
        self.data['operation', 'precedes', 'operation'].edge_index = prec_edge

        # Disjunctive (contention) edges -- the second half of a standard
        # job-shop disjunctive graph, alongside precedence. Pools are scoped
        # by the persisted (owned factory, operation type), the same factory
        # boundary the scheduler enforces. The explicitly opted-in legacy
        # mode alone permits old company-wide pools. Edges are already built
        # in both directions, so one relation name is sufficient.
        disjunctive_edge = build_disjunctive_edges(self.df_routings, op_map, ownership_snapshot=self.ownership_snapshot, legacy_mode=self.legacy_mode)
        self.data['operation', 'contends_with', 'operation'].edge_index = disjunctive_edge

        # Machine/worker state must flow INTO operation nodes — the heads all
        # read operation embeddings, and PyG propagates source -> destination.
        # The previous ('operation','eligible_for','machine') and
        # ('operation','assigned_to','worker') edge types pointed the wrong
        # way, so machine and worker features provably had ZERO effect on any
        # prediction (randomising them moved the delay logits by 0.0). See
        # graph/edge_builder.py::build_assignment_edges_into_operations.
        mach_edge, work_edge = build_assignment_edges_into_operations(
            self.df_assignments, op_map, mach_map, worker_map
        )
        self.data['machine', 'operates', 'operation'].edge_index = mach_edge
        self.data['worker', 'staffs', 'operation'].edge_index = work_edge

        # Reverse edges. Two reasons, one mandatory and one substantive:
        #  1. to_hetero() requires EVERY node type to be updated by message
        #     passing. With machine/worker acting purely as sources it raises
        #     "Cannot generate a graph node 'relu' for type 'machine'",
        #     because layers 2 and 3 have no representation to consume.
        #  2. They are useful in their own right: a machine node aggregating
        #     the operations queued on it develops a notion of its own load,
        #     which layer 3 then feeds back to those operations.
        self.data['operation', 'runs_on', 'machine'].edge_index = mach_edge.flip(0)
        self.data['operation', 'staffed_by', 'worker'].edge_index = work_edge.flip(0)
        
        # 3. Attach a reproducible ORDER-LEVEL train/val split over operation
        # nodes -- see _build_split_masks for why this must be grouped by
        # production_order_id, not a flat per-operation shuffle (Section 0F).
        # The graph stays whole for message passing (transductive setting) —
        # only the LOSS/METRIC computation in train.py / evaluate.py should
        # be masked to the appropriate split.
        #
        # Looked up explicitly via op_map rather than assumed positional
        # (merged_ops_df row order happens to match node index today, but
        # that's an internal detail of build_operation_node_features, not a
        # contract worth depending on silently here).
        num_op_nodes = op_x.shape[0]
        key_to_order_id = merged_ops_df.set_index('unique_op_key')['production_order_id']
        order_ids_per_node = np.empty(num_op_nodes, dtype=object)
        for key, idx in op_map.items():
            order_ids_per_node[idx] = key_to_order_id[key]
        # Exposed so training can persist the exact validation order IDs in
        # the checkpoint-bound split manifest. Evaluation verifies that
        # manifest's graph fingerprint and checkpoint hash; the superseded
        # standalone val_order_ids.json is not an authority.
        self.order_ids_per_node = order_ids_per_node

        train_mask, val_mask = _build_split_masks(order_ids_per_node)
        self.data['operation'].train_mask = train_mask
        self.data['operation'].val_mask = val_mask

        # 4. Extract Ground Truth Labels for GNN Multi-Task Training.
        #
        # ORDER MATTERS, and this is why the split above was hoisted to run
        # FIRST (label-audit fix 1): Head 3's slack->percentile curve must be
        # derived from the TRAINING operations only. Deriving it here, after
        # the split exists, is the whole fix -- previously labels were built
        # before any split existed, so every validation operation's priority
        # label was its rank among the training operations too.
        #
        # Two passes over the data were the alternative; reordering is
        # cleaner because nothing in the split depends on the labels, only
        # the reverse, so there was never a genuine circular dependency --
        # just an accident of the original call order.
        labels, self.label_meta = extract_gnn_labels(
            merged_ops_df, op_map, self.df_actual_vs_planned,
            train_mask=train_mask, priority_cutpoints=priority_cutpoints,
        )
        self.data['operation'].delay_valid_mask = labels['delay_valid']

        logger.info(
            f"HeteroData assembly complete: {self.data} | "
            f"operation split: {int(train_mask.sum())} train / {int(val_mask.sum())} val"
        )
        logger.info(
            f"Delay label validity: {self.label_meta['delay_valid_count']} usable, "
            f"{self.label_meta['delay_masked_no_outcome']} masked (no recorded outcome), "
            f"{self.label_meta['delay_masked_partial']} masked (partially observed lots). "
            f"Priority cut-points: {self.label_meta['priority_cutpoints_source']}."
        )
        if self.label_meta.get('completion_fallback_count'):
            logger.warning(
                f"{self.label_meta['completion_fallback_count']} operation(s) had no planned "
                f"span and were masked out of completion loss/metrics -- see "
                f"graph/labels.py's completion-validity note."
            )
        return self.data, labels

    def graph_fingerprint(self) -> str:
        """
        Identity of the graph a split was derived from, for the split
        manifest (label-audit fix 5).

        Covers the operation node set and the split sizes -- the things that
        change when the database is reseeded or the order book moves, and
        exactly what makes a recorded val split stale. A reseed that happens
        to regenerate the same production_order_id range (1-1029) is NOT
        caught by order IDs alone, which is precisely how a stale
        val_order_ids.json loaded silently before this existed; the operation
        keys and counts do change, so they are what gets hashed.
        """
        op_keys = sorted(self.op_map.keys())
        train_mask = self.data['operation'].train_mask
        val_mask = self.data['operation'].val_mask
        payload = "|".join([
            str(len(op_keys)),
            str(int(train_mask.sum())),
            str(int(val_mask.sum())),
            ",".join(op_keys),
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

if __name__ == "__main__":
    print("Constructing PyTorch Geometric HeteroData Graph...")
    builder = FactoryGraphBuilder()
    hetero_graph, labels = builder.build_graph()
    
    print("\n--- GRAPH STRUCTURE ---")
    print(hetero_graph)
    print("\n--- TARGET LABELS ---")
    for task, tensor in labels.items():
        print(f"Task: {task:<12} | Tensor Shape: {tensor.shape}")