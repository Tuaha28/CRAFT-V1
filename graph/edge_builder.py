import torch
import numpy as np
import pandas as pd
from collections import defaultdict
from utils.logger import get_logger
from utils.machine_taxonomy import machine_type_to_operation_type

logger = get_logger("edge_builder", "gnn_training.log")

def build_assignment_edges_into_operations(
    df_assignments: pd.DataFrame,
    op_mapping: dict,
    machine_mapping: dict,
    worker_mapping: dict,
) -> tuple:
    """
    Builds machine -> operation and worker -> operation edges from the REAL
    per-lot assignments in Job_Shop_Schedules (via
    database.queries.fetch_operation_assignments()).

    ---------------------------------------------------------------------
    WHY THIS EXISTS: the two functions below build their edges in the
    direction operation -> machine and operation -> worker. PyTorch
    Geometric propagates messages from SOURCE to DESTINATION, so with those
    edge types the only edges pointing INTO an 'operation' node are
    ('operation', 'precedes', 'operation'). Machine and worker state
    therefore never reached the operation embeddings that all three
    prediction heads read from.

    This was verified empirically rather than inferred: randomising every
    value in x_dict['machine'] and x_dict['worker'] changed the operation
    delay logits by exactly 0.0000000000. The model was computing embeddings
    for 3,000 machine nodes and 9,000 worker nodes across 20.8M edges, then
    discarding them — about 24 of every 25 seconds of each forward pass.

    WHY INSTANCE-BASED, NOT TYPE-BASED: simply reversing the old edges would
    not have fixed much. They connect an operation to EVERY machine of a
    matching type (~600) and EVERY worker holding a matching skill (~2,350),
    so all operations of the same type have byte-identical neighbourhoods —
    after aggregation they are indistinguishable, and the graph adds no
    information over the operation's own 3 features. It would also have cost
    ~5.3 GB of message tensors per layer, which does not fit this machine.
    Using the machines/workers actually assigned to each operation's lots
    gives 0.81M edges instead of 20.8M (25x fewer) and makes each
    operation's neighbourhood specific to it: this operation's machines, and
    whether any of them is currently in Breakdown.

    LEAKAGE: df_assignments MUST come from fetch_operation_assignments(),
    which reads all lots. Building these edges from the Done-filtered
    fetch_current_shop_schedule() would encode the delay label in the node
    DEGREE (~10 neighbours when delayed vs ~60 when not) — the same leak
    documented in graph/node_features.py, rebuilt in graph form.

    Returns (machine_to_op_edge_index, worker_to_op_edge_index).
    """
    logger.info("Building machine->operation and worker->operation assignment edges...")

    if df_assignments is None or df_assignments.empty:
        logger.warning("No assignment data supplied — machine/worker state will not reach operations.")
        empty = torch.zeros((2, 0), dtype=torch.long)
        return empty, empty.clone()

    df = df_assignments.copy()
    df['unique_op_key'] = (
        df['production_order_id'].astype(str) + "_" + df['operation_id'].astype(str)
    )

    m_src, m_dst = [], []
    w_src, w_dst = [], []
    for op_key, asset_id, worker_id in zip(df['unique_op_key'], df['asset_id'], df['employee_id']):
        op_idx = op_mapping.get(op_key)
        if op_idx is None:
            continue
        m_idx = machine_mapping.get(asset_id)
        if m_idx is not None:
            m_src.append(m_idx)
            m_dst.append(op_idx)
        w_idx = worker_mapping.get(worker_id)
        if w_idx is not None:
            w_src.append(w_idx)
            w_dst.append(op_idx)

    machine_edges = torch.tensor([m_src, m_dst], dtype=torch.long)
    worker_edges = torch.tensor([w_src, w_dst], dtype=torch.long)
    logger.info(
        f"Built {machine_edges.shape[1]} machine->operation and "
        f"{worker_edges.shape[1]} worker->operation edges."
    )
    return machine_edges, worker_edges


def build_precedence_edges(df_ops: pd.DataFrame, op_mapping: dict) -> torch.Tensor:
    """
    Creates directed edges: (Operation Step i) -> [PRECEDES] -> (Operation Step i+1)
    """
    logger.info("Building operation precedence edges...")
    sources, targets = [], []
    
    for _, group in df_ops.groupby('production_order_id'):
        sorted_ops = group.sort_values('operation_sequence_number')
        keys = sorted_ops['unique_op_key'].tolist()
        
        for i in range(len(keys) - 1):
            if keys[i] in op_mapping and keys[i+1] in op_mapping:
                sources.append(op_mapping[keys[i]])
                targets.append(op_mapping[keys[i+1]])
                
    edge_index = torch.tensor([sources, targets], dtype=torch.long)
    logger.info(f"Built {edge_index.shape[1]} precedence edges.")
    return edge_index

def build_disjunctive_edges(
    df_routings: pd.DataFrame,
    op_mapping: dict,
    *, ownership_snapshot=None, legacy_mode=None,
) -> torch.Tensor:
    """
    Disjunctive (contention) edges: direct operation<->operation edges
    between operations competing for the SAME resource pool.

    PROJECT_STATUS.txt Section 0H.15. A literature review found essentially
    every published GNN-for-job-shop-scheduling approach uses a disjunctive
    graph: operation nodes linked by two edge types -- conjunctive
    (precedence within an order, build_precedence_edges above) and
    disjunctive (operations that contend for the same resource). This graph
    had only the first. Contention previously reached an operation two hops
    away (operation -> machine -> operation), which standard message
    passing dilutes heavily -- this direct edge is meant to expose it in
    one hop instead.

    POOL DEFINITION -- operation_type ONLY, company-wide. REPLACES the
    original (majority historical line, operation_type) pooling from
    0H.15, which was measured directly during a later graph-structure
    re-audit (see chat, not this docstring's original writing) to not
    represent real contention at all:

      THE (LINE, TYPE) POOLING WAS WRONG, MEASURED NOT ASSUMED: an
      operation's lots touch a mean of 16.7 distinct lines (median 15) out
      of 99 total, and its "majority" (modal) line captures a mean of only
      11.8% of its own lots -- 99.9% of operations have a "majority" that
      is a bare plurality among ~15-17 near-tied lines, not a real
      majority. Root cause, confirmed directly in
      data_generation/seed_database_fixed.py: `machine_heaps[m_type]`
      pools every machine of a type across ALL 99 lines with NO
      line-scoping at all -- each lot is dispatched to whichever machine
      of the required type has the earliest company-wide free-time.
      "This operation's line" was never a real resource boundary in this
      simulation's own dispatch mechanism; operation_type alone,
      company-wide, is what the real heap actually constrains.

      TRACTABILITY, MEASURED BEFORE ADOPTING (not rejected on the old
      same-machine-candidate's cost reasoning -- that one was an
      open-ended ~37M-pair explosion from connecting operations sharing
      any ACTUAL asset_id; this is a known, finite, already-measured
      bound): 5 pools (one per canonical operation_type), sizes
      1,023-2,615 on the live database, 8,884,353 undirected pairs ->
      17,768,706 directed edges. Probed directly on this graph before
      committing to a full retrain: peak RSS 1,036 MB (trivial), 37.7s per
      training epoch vs. 4.3s/epoch for the old (line,type) pooling (8.8x,
      well under the 68x edge-count ratio -- SAGEConv's sparse aggregation
      scales sub-linearly here), ~94 minutes for a full 150-epoch retrain.
      Tractable, adopted.

    No longer needs df_assignments/df_machines -- the old pooling used
    real per-lot assignments to resolve each operation's majority LINE;
    with line dropped from the pool key entirely, only each operation's
    own required_machine_type (already in df_routings) is needed.

    Inherently undirected -- two operations either contend or they don't,
    there is no source/destination -- so both (i, j) and (j, i) are added
    under this ONE relation name rather than needing a separate reverse
    edge type the way the directed machine/worker<->operation edges do.

    Vectorized pair generation (np.triu_indices), not a Python double
    loop: pool sizes now run into the thousands (vs. tens under the old
    per-line pooling), where a pure-Python O(n^2) loop would be
    prohibitively slow.
    """
    logger.info("Building operation<->operation disjunctive (contention) edges...")

    optype_by_key = {}
    for _, op in df_routings.iterrows():
        key = f"{op['production_order_id']}_{op['operation_id']}"
        optype_by_key[key] = machine_type_to_operation_type(op.get('required_machine_type', ''))

    # FACTORY-SCOPED CONTENTION POOLS (Batch-4 fix 7).
    #
    # A disjunctive edge asserts "these two operations compete for the same
    # resource". Once one-factory-per-order is enforced (ga/chromosome.py),
    # two operations owned by different branches share NO eligible machine,
    # so a company-wide clique asserts contention that cannot occur -- and
    # pays for it twice: the model learns a false relation, and every epoch
    # carries the edges. Measured on the current order book: company-wide
    # gives 5 pools / 18,027,826 directed edges; (factory, optype) gives
    # 30 pools / 4,910,962 -- a 72.8% reduction, which is material on a
    # CPU-only retrain.
    #
    # Strict by default. Legacy company-wide pooling requires named opt-in.
    # The historical 4,910,962 edges describe one allocation; a randomized
    # capacity-weighted reseed will produce a different count.
    from utils.factory_ownership import resolve_ownership
    order_ids = df_routings['production_order_id'].astype(str).unique()
    snapshot = resolve_ownership(order_ids, ownership_snapshot, legacy_mode=legacy_mode)
    ownership = {} if snapshot is None else snapshot.for_orders(order_ids)
    pools = defaultdict(list)
    for op_key, idx in op_mapping.items():
        optype = optype_by_key.get(op_key)
        if optype is not None:
            pool = (ownership[op_key.split('_')[0]], optype) if snapshot is not None else optype
            pools[pool].append(idx)
    logger.info(f"Disjunctive pools: {len(pools)} ({'factory-scoped' if snapshot is not None else 'explicit legacy company-wide'})")

    sources_parts, targets_parts = [], []
    for indices in pools.values():
        n = len(indices)
        if n < 2:
            continue
        arr = np.array(indices, dtype=np.int64)
        i_idx, j_idx = np.triu_indices(n, k=1)
        sources_parts.append(arr[i_idx]); targets_parts.append(arr[j_idx])
        sources_parts.append(arr[j_idx]); targets_parts.append(arr[i_idx])

    if sources_parts:
        sources = np.concatenate(sources_parts)
        targets = np.concatenate(targets_parts)
        edge_index = torch.tensor(np.stack([sources, targets]), dtype=torch.long)
    else:
        edge_index = torch.zeros((2, 0), dtype=torch.long)

    logger.info(
        f"Built {edge_index.shape[1]} disjunctive (operation<->operation contention) "
        f"edges across {len(pools)} operation-type pools."
    )
    return edge_index


def build_machine_eligibility_edges(
    df_ops: pd.DataFrame, 
    df_machines: pd.DataFrame, 
    op_mapping: dict, 
    machine_mapping: dict
) -> torch.Tensor:
    """
    NO LONGER CALLED by graph/graph_builder.py — kept for reference and for
    anyone wanting to reinstate type-based eligibility edges. Superseded by
    build_assignment_edges_into_operations() above, for two reasons: these
    edges point operation -> machine, the direction that never reaches the
    prediction heads, and they connect each operation to ~600 interchangeable
    machines of a matching type, giving every operation of that type an
    identical neighbourhood (4.23M edges carrying no per-operation
    information). If you do reinstate them, add the reverse direction too or
    they will silently contribute nothing.

    Creates bipartite edges using the canonical machine-type -> operation-type
    mapping (utils/machine_taxonomy.py) against df_machines['operation_type']
    (from Machine_Capabilities, already present in fetch_machine_state()'s
    output).

    Previously this matched a 4-character prefix of required_machine_type
    against the machine_model string (e.g. "sing" in "juki-sing-83"). That
    happened to work for every machine type in this factory's data purely
    because machine_model names coincidentally embed a type abbreviation —
    but it's fragile (breaks silently if model naming ever changes) and
    ignores the operation_type column that was already being fetched for
    exactly this purpose. Exact category matching is both more correct and
    more robust.
    """
    logger.info("Building machine capability edges (via machine-type -> operation-type mapping)...")
    sources, targets = [], []
    
    # Pre-group machine indices by their (normalized) operation_type
    optype_to_indices = defaultdict(list)
    for _, row in df_machines.iterrows():
        asset_id = row['asset_id']
        if asset_id in machine_mapping and pd.notna(row.get('operation_type')):
            optype_to_indices[str(row['operation_type']).strip().lower()].append(machine_mapping[asset_id])

    unmapped_types_seen = set()
    for _, op in df_ops.iterrows():
        op_key = op['unique_op_key']
        if op_key not in op_mapping:
            continue
            
        op_idx = op_mapping[op_key]
        required_type = op.get('required_machine_type', '')
        canonical_optype = machine_type_to_operation_type(required_type)
        
        if canonical_optype is None:
            unmapped_types_seen.add(str(required_type))
            continue
        
        for machine_idx in optype_to_indices.get(canonical_optype, []):
            sources.append(op_idx)
            targets.append(machine_idx)

    if unmapped_types_seen:
        logger.warning(
            f"required_machine_type value(s) with no entry in MACHINE_TYPE_TO_OPERATION_TYPE "
            f"(utils/machine_taxonomy.py), skipped: {unmapped_types_seen}"
        )
                
    edge_index = torch.tensor([sources, targets], dtype=torch.long)
    logger.info(f"Built {edge_index.shape[1]} machine capability edges.")
    return edge_index

def build_worker_assignment_edges(
    df_ops: pd.DataFrame, 
    df_workers: pd.DataFrame, 
    op_mapping: dict, 
    worker_mapping: dict
) -> torch.Tensor:
    """
    NO LONGER CALLED by graph/graph_builder.py — superseded by
    build_assignment_edges_into_operations(), same reasoning as the machine
    eligibility builder above (wrong direction, plus ~2,350 interchangeable
    workers per operation = 16.58M edges with no per-operation signal).

    Creates bipartite edges matching operations to present workers via the
    canonical machine-type -> operation-type mapping
    (utils/machine_taxonomy.py) against each worker's skill_type (a
    comma-separated list of operation_type values from Operator_Skill_Matrix,
    aggregated per-employee in database/queries.py::fetch_worker_state).

    Previously this matched a 4-character prefix of required_machine_type
    directly against skill_type — but those are different vocabularies (see
    utils/machine_taxonomy.py's docstring), so the prefix almost never
    aligned: 3 of this factory's 5 machine types ("Single-Needle Lockstitch",
    "Automated Pattern Tacker", "Feed-off-the-Arm Special") never matched ANY
    worker skill category at all, meaning 60% of all operations had zero
    eligible workers in the graph. Confirmed by running the old logic against
    real exported data before this fix.
    """
    logger.info("Building worker assignment edges (via machine-type -> operation-type mapping)...")
    sources, targets = [], []
    
    # Pre-compute present workers by each individual skill they hold
    # (skill_type may be a comma-separated list per worker).
    skill_to_indices = defaultdict(list)
    for _, row in df_workers.iterrows():
        w_id = row['worker_id']
        if w_id in worker_mapping and row.get('status', 'Present') == 'Present':
            raw_skills = str(row.get('skill_type', '') or '')
            for skill in raw_skills.split(','):
                skill = skill.strip().lower()
                if skill:
                    skill_to_indices[skill].append(worker_mapping[w_id])

    unmapped_types_seen = set()
    for _, op in df_ops.iterrows():
        op_key = op['unique_op_key']
        if op_key not in op_mapping:
            continue
            
        op_idx = op_mapping[op_key]
        required_type = op.get('required_machine_type', '')
        canonical_optype = machine_type_to_operation_type(required_type)
        
        if canonical_optype is None:
            unmapped_types_seen.add(str(required_type))
            continue
            
        for w_idx in skill_to_indices.get(canonical_optype, []):
            sources.append(op_idx)
            targets.append(w_idx)

    if unmapped_types_seen:
        logger.warning(
            f"required_machine_type value(s) with no entry in MACHINE_TYPE_TO_OPERATION_TYPE "
            f"(utils/machine_taxonomy.py), skipped: {unmapped_types_seen}"
        )
                
    edge_index = torch.tensor([sources, targets], dtype=torch.long)
    logger.info(f"Built {edge_index.shape[1]} worker assignment edges.")
    return edge_index