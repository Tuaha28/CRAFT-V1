import torch
import pandas as pd
import numpy as np
import scipy.sparse as sp
from utils.config import GA_LOTS_PER_OPERATION
from utils.logger import get_logger
from utils.machine_taxonomy import machine_type_to_operation_type, operation_type_index

logger = get_logger("node_features", "gnn_training.log")

def build_machine_node_features(df_machines: pd.DataFrame) -> tuple:
    logger.info(f"Processing {len(df_machines)} machine records into tensors...")
    status_dummies = pd.get_dummies(df_machines['current_status'])
    
    expected_statuses = ['Active', 'Under_Maintenance', 'Idle', 'Breakdown']
    for col in expected_statuses:
        if col not in status_dummies.columns:
            status_dummies[col] = 0

    # DTYPE FIX. get_dummies returns BOOL columns; the padding above adds
    # int64 ones for any status absent from this batch. np.hstack over a
    # mixed bool/int/float set produces an OBJECT array, and torch.tensor
    # then raises "can't convert np.ndarray of type numpy.object_".
    # Latent, not theoretical: it fires whenever a status category happens
    # to be missing from the machine table -- which is entirely possible in
    # production (e.g. no machine currently in 'Breakdown') and would take
    # down the whole graph build. Cast explicitly so the result is float
    # regardless of which categories appear.
    status_block = status_dummies[expected_statuses].to_numpy(dtype=np.float64)
    rpm_norm = (df_machines['max_rpm'] / 5000.0).fillna(0).values.reshape(-1, 1).astype(np.float64)
    features = np.hstack((status_block, rpm_norm))
    mapping = {asset_id: idx for idx, asset_id in enumerate(df_machines['asset_id'].values)}
    
    return torch.tensor(features, dtype=torch.float), mapping

def build_worker_node_features(df_workers: pd.DataFrame) -> tuple:
    """
    Extracts features for the new Worker nodes to model efficiency and absenteeism.
    """
    logger.info(f"Processing {len(df_workers)} worker records into tensors...")
    status_dummies = pd.get_dummies(df_workers['status']) # e.g., 'Present', 'Absent'
    
    expected_statuses = ['Present', 'Absent']
    for col in expected_statuses:
        if col not in status_dummies.columns:
            status_dummies[col] = 0
            
    # efficiency_score is real (Operator_Skill_Matrix.efficiency_rating via
    # fetch_worker_state's AVG, 70-98 raw scale) -- normalized by /100 here
    # to match this feature vector's other roughly-0-1 columns.
    #
    # HISTORY, kept because both prior versions were wrong in instructive
    # ways. (1) The original fallback was the LITERAL 0.8, applied BEFORE
    # normalization -- a 0-1 value mixed into a column whose real values run
    # 70-98, hitting every worker with no Operator_Skill_Matrix row (by
    # construction every Line_Supervisor and Mechanic). 0.8 sat ~83 points
    # below the real population minimum, so those nodes looked like by far
    # the least efficient workers in the graph. (2) That was replaced by a
    # correctly-scaled but HARDCODED 0.84, measured once and frozen -- which
    # then went stale the moment the database was reseeded from 15,066
    # workers to 9,000.
    #
    # WHAT THIS ACTUALLY IS (Batch-4 fix 9 -- stated precisely, because it
    # was previously described as something it is not).
    #
    # It is a PER-BUILD population mean, recomputed from whatever workers this
    # build sees. It is NOT:
    #   * train-split-derived -- the train/val split is over OPERATIONS;
    #     worker nodes are not split by it, so there is no "training subset
    #     of workers" this could be restricted to; and
    #   * frozen or persisted -- unlike the breakdown scale, it is not
    #     written to the preprocessing artifact and not reloaded at
    #     inference, so a build against a different workforce will impute a
    #     different value.
    #
    # That is defensible for what it does -- imputing a missing value in the
    # SAME column, for workers who structurally have no skill-matrix row
    # (every Line_Supervisor and Mechanic) -- because no label is involved
    # and there is nothing to leak. It is NOT equivalent to the frozen
    # normaliser pattern, and should not be cited as one. Making it frozen
    # would require persisting it alongside the breakdown scale and is a
    # deliberate non-change, not an oversight.
    #
    # The literal 0.84 remains ONLY as a last resort when this build has no
    # observed efficiency at all; on any real dataset the observed mean wins.
    #
    # DERIVED FROM THIS BUILD'S OWN WORKERS (audit #13), not a frozen 0.84.
    # That literal came with statistics ("mean 83.97, n=14,229, 837 of 15,066
    # workers") measured on a database that no longer exists -- the reseed to
    # 9,000 employees invalidated both the value and every number in the
    # comment above. Recomputed per build so it can never go stale again.
    #
    # NOT train-split-scoped, deliberately: the train/val split is over
    # OPERATIONS, and worker nodes are not split by it at all, so there is no
    # "training subset of workers" to restrict to. This is an imputation for
    # workers missing a skill-matrix row (structurally every Line_Supervisor
    # and Mechanic), using the observed mean of the same column it fills --
    # no label is involved and there is nothing to leak.
    if 'efficiency_score' in df_workers.columns:
        efficiency_norm = df_workers['efficiency_score'] / 100.0
        observed = efficiency_norm.dropna()
        efficiency_fallback = float(observed.mean()) if not observed.empty else 0.84
    else:
        efficiency_fallback = 0.84
        efficiency_norm = pd.Series([efficiency_fallback] * len(df_workers))
    efficiency = efficiency_norm.fillna(efficiency_fallback).values.reshape(-1, 1)
    logger.info(
        f"Worker efficiency fallback derived from this build: {efficiency_fallback:.4f} "
        f"({int(efficiency_norm.isna().sum())} worker(s) imputed)."
    )
    # Skill breadth: how many distinct operation types this worker is skilled
    # in (from Operator_Skill_Matrix, aggregated in fetch_worker_state).
    # NOTE: this used to read a 'skill_level' column that fetch_worker_state()
    # never actually returned, so every worker silently got the same constant
    # 0.6 here — a dead, uninformative feature. skill_breadth is real signal.
    # Normalized by /5.0 as a soft cap (most operators hold a handful of skills).
    skill = (df_workers.get('skill_breadth', pd.Series([1.0]*len(df_workers))) / 5.0).fillna(0.2).clip(upper=1.0).values.reshape(-1, 1)
    
    # Same explicit float cast as build_machine_node_features -- see the
    # dtype note there. A status category missing from this batch otherwise
    # yields an object array that torch.tensor cannot convert.
    status_block = status_dummies[expected_statuses].to_numpy(dtype=np.float64)
    features = np.hstack((status_block, efficiency.astype(np.float64), skill.astype(np.float64)))
    mapping = {w_id: idx for idx, w_id in enumerate(df_workers['worker_id'].values)}
    
    return torch.tensor(features, dtype=torch.float), mapping

def _aggregate_dispatch_context(df_dispatch_context: pd.DataFrame) -> pd.DataFrame:
    """
    Per-operation dispatch-context features from
    database.queries.fetch_dispatch_context() -- machine health, employee
    availability and queue congestion as they stood at the moment each lot
    was dispatched (see that function's docstring for why this is causally
    upstream of the lot's own outcome, not derived from it).

    AGGREGATION-MISMATCH FIX (see chat, follows directly from the delay-label
    causal-mechanism fix): machine-breakdown history and leave status are now
    read from each operation's DECISIVE LOT only, not averaged across all
    ~19 lots. Previously both were MEANs, same as queue delay below -- but a
    per-lot-vs-operation-level correlation check against the live database
    (after the delay-label fix made these features causally connected to the
    label for the first time) showed the mean-based breakdown feature's real,
    highly significant per-lot signal (r=0.0406, p=3.7e-62) collapsed to
    ~zero at the operation level (r=-0.0019, p=0.86): the label is MAX-based
    (driven by whichever single lot finishes last), so averaging breakdown
    exposure across ~19 lots -- most of them on different physical machines,
    run by different employees, and NOT the lot that determines the label --
    dilutes a real effect via the same class of CLT-averaging mistake this
    project already fixed once for the label's own construction (Section 0D).

    SEPARATE MEAN-BASED BREAKDOWN COLUMN RESTORED (delay-label fix trade-off
    decision, option (b), see chat/DELAY_LABEL_FIX_PROPOSAL.md): the
    decisive-lot swap above was necessary for delay (a single-lot MAX-based
    label) but cost completion (R2 dropped ~0.06-0.11 in a same-data RF
    ablation) -- completion's label is a SPAN across all ~19 lots, not
    single-lot-driven, so it needs the OPPOSITE aggregation. The old
    mean-based breakdown feature was 42.6% of completion's RF importance
    (a good proxy for an operation's overall multi-lot congestion) vs. only
    9.5% for the decisive-lot version (correctly focused on delay's one
    decisive lot instead, but a poor fit for completion's whole-span
    target). Rather than force one feature slot to serve two heads with
    genuinely different structure, `mean_breakdown_count_at_dispatch` is
    reinstated ADDITIONALLY, alongside (not instead of) the decisive-lot
    column -- each head's own attention/message-passing can learn which of
    the two it actually needs; delay's decisive-lot signal is untouched.

    The DECISIVE LOT is identified as the row with the MAX planned_end_time
    within each (production_order_id, operation_id) group. This is a
    leak-safe proxy -- planned_end_time is fixed at scheduling time, long
    before any lot executes, so choosing by it can never leak the outcome
    being predicted (unlike choosing by MAX actual_end_time, which would be
    a subtle selection leak: which lot actually finished last is itself part
    of the thing the label measures).

    PROXY STRENGTH -- RE-MEASURED, AND WEAKER THAN IT WAS.
    Measured on the current dataset: the MAX-planned-end lot agrees with the
    true MAX-actual-end lot in 6,871/8,799 operations (78.1%). The figure
    previously recorded here was 8,497/8,802 (96.5%) on the pre-cascade
    database, and that number is no longer representative.
    NOTE the original justification rested on an assumption that has since
    stopped holding: that actual lot timings closely track their plan, so
    the planned-last lot is almost always the actual-last one. The seeder's
    actual timeline is now independently resource-constrained and therefore
    CASCADING (see PROJECT_STATUS.txt Section 0J), so a late lot pushes the
    next one and the actual finishing ORDER within an operation reshuffles
    far more. Measured cause: within-operation per-lot lateness SD moved
    6.9 -> 68.7 minutes (median) at unchanged lot count, driven by queueing
    rather than by work taking longer.
    The proxy is still leak-safe -- that property depends only on
    planned_end_time being fixed before execution, which is unchanged -- and
    it is still right for the clear majority of operations. But it is a
    WEAKER stand-in than the 96% figure implied, and roughly one operation
    in five now attributes its decisive-lot features to a lot that was not
    actually the last to finish. Re-measure this before relying on it.

    queue_delay_mins_at_dispatch is DELIBERATELY LEFT AS A MEAN, not
    realigned to the decisive lot -- out of scope for this fix. Its
    mechanism (sample_execution_variance_multiplier's queue-driven SD
    widening) is a symmetric spread effect, not a mean shift, so it was never
    expected to show a per-lot linear correlation with signed lateness in
    the first place (measured r=0.0007, p=0.77 at the per-lot level -- by
    design, not diluted). Averaging it also still reduces per-lot sampling
    noise without erasing a mean-shift signal it was never carrying (both
    non-negative, monotonic-or-zero quantities -- no zero-centred noise for
    the CLT to wash out the way it does for lateness or a 0/1 leave flag).

    frac_dispatched_machine_down (an is-it-'Breakdown'-right-now flag) is
    DELIBERATELY NOT built here despite machine_status_at_dispatch being
    available -- measured to be identically 0.0 across all 7,500 operations
    in this database (a scheduled machine can structurally never be observed
    mid-repair at dispatch; see fetch_dispatch_context's docstring).
    decisive_lot_breakdown_count_at_dispatch replaces it: a machine's
    cumulative PRIOR breakdown history, which does genuinely vary.
    """
    cols = ['production_order_id', 'operation_id']
    if df_dispatch_context is None or df_dispatch_context.empty:
        # Explicit float dtype on the numeric columns -- see the matching
        # note on agg_schedule's empty fallback above; an object-dtype empty
        # frame here poisons the merge/hstack downstream in
        # build_operation_node_features exactly the same way.
        return pd.DataFrame({
            'production_order_id': pd.Series(dtype='object'),
            'operation_id': pd.Series(dtype='object'),
            'decisive_lot_employee_on_leave': pd.Series(dtype='float64'),
            'mean_queue_delay_mins': pd.Series(dtype='float64'),
            'decisive_lot_breakdown_count_at_dispatch': pd.Series(dtype='float64'),
            'mean_breakdown_count_at_dispatch': pd.Series(dtype='float64'),
            'context_observed': pd.Series(dtype='float64'),
        })

    df = df_dispatch_context.copy()
    # Conservative completeness contract: all lots must have finite queue
    # and breakdown context and a planned end identifying the decisive lot.
    # Missing context is not a measured healthy/zero observation.
    df['_context_observed'] = (
        np.isfinite(pd.to_numeric(df['queue_delay_mins_at_dispatch'], errors='coerce'))
        & np.isfinite(pd.to_numeric(df['machine_breakdown_count_at_dispatch'], errors='coerce'))
        & df['planned_end_time'].notna()
    ).astype(float)
    df['employee_on_leave_at_dispatch'] = df['employee_on_leave_at_dispatch'].fillna(0).astype(float)

    # queue_delay_mins_at_dispatch: mean across ALL lots, as before.
    # mean_breakdown_count_at_dispatch: also a mean across ALL lots --
    # completion's own aggregation, restored alongside (not instead of) the
    # decisive-lot version below (see docstring's "SEPARATE MEAN-BASED
    # BREAKDOWN COLUMN RESTORED" note).
    queue_agg = df.groupby(cols).agg(
        mean_queue_delay_mins=('queue_delay_mins_at_dispatch', 'mean'),
        mean_breakdown_count_at_dispatch=('machine_breakdown_count_at_dispatch', 'mean'),
        # One only when EVERY lot supplies both raw context fields.
        context_observed=('_context_observed', 'min'),
    ).reset_index()

    # employee_on_leave_at_dispatch / machine_breakdown_count_at_dispatch:
    # pulled from the DECISIVE LOT (max planned_end_time) only. Rows with a
    # NULL planned_end_time can't be ranked and are excluded from the
    # idxmax selection -- if an operation's every lot lacks planned_end_time
    # (no evidence at all), it falls out of this groupby entirely and gets
    # the standard 0-fill "no evidence" default from the left-merge in
    # build_operation_node_features, consistent with every other
    # missing-data case in this file.
    df_ranked = df.dropna(subset=['planned_end_time'])
    if df_ranked.empty:
        decisive_agg = pd.DataFrame({
            'production_order_id': pd.Series(dtype='object'),
            'operation_id': pd.Series(dtype='object'),
            'decisive_lot_employee_on_leave': pd.Series(dtype='float64'),
            'decisive_lot_breakdown_count_at_dispatch': pd.Series(dtype='float64'),
        })
    else:
        decisive_idx = df_ranked.groupby(cols)['planned_end_time'].idxmax()
        decisive_rows = df_ranked.loc[decisive_idx]
        decisive_agg = decisive_rows[cols + ['employee_on_leave_at_dispatch', 'machine_breakdown_count_at_dispatch']].rename(
            columns={
                'employee_on_leave_at_dispatch': 'decisive_lot_employee_on_leave',
                'machine_breakdown_count_at_dispatch': 'decisive_lot_breakdown_count_at_dispatch',
            }
        )

    agg = pd.merge(queue_agg, decisive_agg, on=cols, how='left')
    incomplete = agg['context_observed'] != 1
    agg.loc[incomplete, ['mean_queue_delay_mins', 'mean_breakdown_count_at_dispatch',
                         'decisive_lot_breakdown_count_at_dispatch']] = np.nan
    return agg


def _compute_pool_occupancy(df_assignments: pd.DataFrame, optype_by_key: dict) -> dict:
    """
    Model/feature audit step 3 (see chat): for each operation, how many
    OTHER operations of the SAME operation_type (its disjunctive-
    contention pool, company-wide -- see edge_builder.py::
    build_disjunctive_edges) actually shared at least one PHYSICAL MACHINE
    with it, across all of either operation's real lots.

    Motivated directly by mean_queue_delay_norm proving itself completion's
    dominant signal (permutation R2 drop 0.82): that feature is a
    HISTORICAL AVERAGE of how long lots actually waited. Pool occupancy is
    a different kind of signal -- a structural, REAL-CONTENTION count of
    how many distinct competitors this operation actually had, not how
    long waiting took. The two could turn out redundant (same underlying
    congestion, different summary statistic) or additive (frequency of
    contention vs. cost of contention are genuinely different things) --
    that's exactly what this step's permutation-importance comparison
    needs to resolve, not assumed either way here.

    LEAK-FREE BY CONSTRUCTION: built from fetch_operation_assignments()
    (df_assignments), the SAME already-proven-safe source
    machine_parallelism_norm already uses -- all lots, no execution_status
    filter. Deliberately NOT built from planned_start_time/planned_end_time
    (which would have been the more literal "concurrent right now" reading):
    those come via df_schedule's Done-filtered fetch_current_shop_schedule(),
    the exact source already documented (0H.4) to understate planned spans
    for partially-done operations. The leak-free alternative
    (df_actual_vs_planned) is explicitly reserved LABEL-ONLY
    (graph_builder.py) -- reusing it here, even just for its planned_
    columns, would blur that boundary for a comparatively minor gain in
    literalness. A shared-machine count is a real, defensible reading of
    "competing for the same specific resource pool" without touching
    either compromise.

    Implemented as a sparse (operations x assets) boolean matrix M; M @ M.T
    gives shared-asset counts between every operation pair in ~0.3s
    (measured on the live database, 8,745 operations x 2,729 assets) --
    not the O(n^2) python-loop cost the disjunctive-edge design doc warned
    about, because this only needs a per-operation COUNT, not a materialized
    edge list.

    Returns {unique_op_key: occupancy_count}, an integer >= 0 (0 for an
    operation with no other operation of its type sharing any machine, or
    if it's altogether absent from df_assignments).
    """
    if df_assignments is None or df_assignments.empty:
        return {}

    df = df_assignments.copy()
    df['unique_op_key'] = (
        df['production_order_id'].astype(str) + "_" + df['operation_id'].astype(str)
    )
    op_keys = sorted(df['unique_op_key'].unique())
    op_to_idx = {k: i for i, k in enumerate(op_keys)}
    asset_ids = sorted(df['asset_id'].unique())
    asset_to_idx = {a: i for i, a in enumerate(asset_ids)}

    rows = df['unique_op_key'].map(op_to_idx).values
    cols = df['asset_id'].map(asset_to_idx).values
    data = np.ones(len(rows), dtype=np.int8)
    M = sp.csr_matrix((data, (rows, cols)), shape=(len(op_keys), len(asset_ids)))
    C = (M @ M.T).tocsr()
    shared_any_asset = np.asarray((C > 0).sum(axis=1)).ravel() - 1  # exclude self

    # Restrict to same operation_type -- "the same specific resource pool",
    # not just "shares any machine with anyone of any type" (which would be
    # ~everyone, since machine allocation isn't line-scoped -- see the
    # graph-structure audit's disjunctive-pool finding). Two operations of
    # DIFFERENT types essentially never share a machine anyway (each type
    # has its own dedicated machine pool), so this mostly just excludes the
    # rare cross-type overlap rather than doing real filtering work -- kept
    # explicit rather than assumed for correctness.
    types = np.array([optype_by_key.get(k) for k in op_keys], dtype=object)
    occupancy = {}
    for t in pd.unique(types[types != None]):
        idx = np.where(types == t)[0]
        sub = C[idx][:, idx]
        occ_t = np.asarray((sub > 0).sum(axis=1)).ravel() - 1
        for local_i, global_i in enumerate(idx):
            occupancy[op_keys[global_i]] = int(occ_t[local_i])
    return occupancy


def _derive_breakdown_scale(merged, column, supplied=None, default=5.0):
    """
    Scale for the breakdown-count features: the mean count among TRAIN
    operations that actually had a breakdown, floored at 1.0.

    `supplied` (from a training run's split manifest) always wins, so
    evaluation and inference normalise on exactly the scale training used.
    Falls back to `default` only when there is no usable train signal at all.
    """
    if supplied is not None:
        return max(1.0, float(supplied))
    if column not in merged.columns or 'production_order_id' not in merged.columns:
        return default
    # NO BROAD EXCEPT (Batch-4 fix 9). This used to wrap the split in
    # `except Exception: return default`, so ANY failure -- a renamed column,
    # a dtype change, a genuine bug in _build_split_masks -- silently produced
    # the hardcoded 5.0 and every breakdown feature in the graph was scaled by
    # a number nobody chose. A normalisation constant that quietly substitutes
    # itself is exactly the class of defect this batch is closing; failures
    # here now propagate.
    #
    # Imported lazily: graph_builder imports THIS module, so a top-level
    # import would be circular. _build_split_masks is a pure function of the
    # order-id set, seed and fraction, so calling it here reproduces
    # graph_builder's split exactly.
    from graph.graph_builder import _build_split_masks
    train_mask, _val = _build_split_masks(merged['production_order_id'].values)
    vals = merged.loc[np.asarray(train_mask), column].dropna()
    vals = vals[vals > 0]
    if vals.empty:
        # A genuine absence of signal, not an error: no TRAIN operation
        # recorded a breakdown. Logged so the default is visible rather than
        # assumed.
        logger.warning(
            f"No positive '{column}' values among train operations; using the "
            f"default breakdown scale {default}."
        )
        return default
    return max(1.0, float(vals.mean()))


def build_operation_node_features(df_routings: pd.DataFrame, df_schedule: pd.DataFrame,
                                   df_assignments: pd.DataFrame = None,
                                   df_dispatch_context: pd.DataFrame = None,
                                   breakdown_scale: float = None) -> tuple:
    """
    df_assignments: output of database.queries.fetch_operation_assignments() —
        distinct (operation, asset, employee) rows across ALL lots, Done
        included. Required for a leak-free machine_parallelism_norm; see the
        LEAKAGE note further down. Passing None falls back to the old
        Done-filtered count and logs a loud warning, so an accidental caller
        can't reintroduce the leak quietly.

    df_dispatch_context: output of database.queries.fetch_dispatch_context() —
        per-lot machine/employee/queue conditions AS OF DISPATCH, aggregated
        per operation by _aggregate_dispatch_context() above. None (the
        default) fills these four features (decisive-lot breakdown, mean
        queue delay, and now mean breakdown -- see below) with 0 rather
        than raising, so
        an un-updated caller still gets a result (just without this signal).
    """
    logger.info(f"Processing {len(df_routings)} operation records into tensors...")
    
    # PREVENT CARTESIAN EXPLOSION: Aggregate schedule lots up to the Operation level
    if not df_schedule.empty:
        status_priority = {'Failed': 4, 'Delayed': 3, 'Running': 2, 'Scheduled': 1, 'Done': 0}
        df_schedule['status_score'] = df_schedule['execution_status'].map(status_priority)
        
        agg_schedule = df_schedule.groupby(['production_order_id', 'operation_id']).agg({
            'planned_start_time': 'min',
            'planned_end_time': 'max',
            'status_score': 'max',
            'asset_id': 'nunique',
        }).rename(columns={'asset_id': 'active_machine_count'}).reset_index()
        
        reverse_priority = {v: k for k, v in status_priority.items()}
        agg_schedule['execution_status'] = agg_schedule['status_score'].map(reverse_priority)
    else:
        # Explicit numeric dtype on active_machine_count -- an empty
        # DataFrame's columns otherwise default to object dtype, which
        # survives the merge below and makes every downstream numeric column
        # derived from it object-dtype too (silently: fillna/clip/division
        # all still "work" on object arrays, but np.hstack then can't build a
        # float tensor from the result). Only reachable when df_schedule is
        # empty, which no production caller passes, but a unit test
        # (tests/test_dispatch_context_features.py) legitimately does.
        agg_schedule = pd.DataFrame({
            'production_order_id': pd.Series(dtype='object'),
            'operation_id': pd.Series(dtype='object'),
            'planned_start_time': pd.Series(dtype='datetime64[ns]'),
            'planned_end_time': pd.Series(dtype='datetime64[ns]'),
            'execution_status': pd.Series(dtype='object'),
            'active_machine_count': pd.Series(dtype='float64'),
        })

    # Merge exactly 1-to-1
    merged = pd.merge(df_routings, agg_schedule, on=['production_order_id', 'operation_id'], how='left')
    merged['unique_op_key'] = merged['production_order_id'].astype(str) + "_" + merged['operation_id'].astype(str)
    
    sam_norm = (merged['sam'] / 10.0).fillna(0).values.reshape(-1, 1)
    qty_norm = (merged['target_quantity'] / 15000.0).fillna(0).values.reshape(-1, 1)
    # REMOVED: is_running, is_delayed, lot_delay_frac.
    # All three were derived from the same execution_status column that
    # graph/labels.py uses to build the delay label — this is data leakage,
    # not a legitimate predictive feature. Verified empirically against real
    # exported data: lot_delay_frac > 0 was a PERFECT predictor of
    # delay_label (0 false positives, 0 false negatives across 7,500
    # operations) because both are deterministically derived from the same
    # MAX-aggregated status. The model wasn't learning to predict delay risk
    # — it was reading the answer off its own input, which is exactly why
    # its outputs were saturated at literal 1.00/0.00 with zero calibration
    # instead of genuine probabilities. Every previous "delay risk" number
    # from this model has been meaningless. This requires retraining — the
    # feature dimension changed (see models/gnn.py's operation input layer).
    # LEAKAGE (round 2) — the reasoning in the previous version of this
    # comment was wrong. It said this feature was safe because it was "built
    # from asset_id counts, not execution_status". The COLUMN is indeed
    # asset_id, but the ROWS came from fetch_current_shop_schedule(), whose
    # SQL excludes execution_status='Done'. That filter is the leak.
    #
    # An operation that is Delayed/Failed has by definition been worked on,
    # so most of its ~60 lots are already Done and vanish from the filtered
    # set, leaving ~10; an untouched operation keeps all ~60. Measured on the
    # real database:
    #     Done-filtered count: mean 57.2 (on-time) vs 10.0 (delayed)
    #                          -> single threshold scores 99.4% accuracy
    #     all-lots count:      mean 57.2 (on-time) vs 57.9 (delayed)
    #                          -> single threshold scores 72.8%, i.e. exactly
    #                             the 72.7% majority-class baseline
    # So 100% of this feature's apparent predictive power was the WHERE
    # clause. Now sourced from fetch_operation_assignments(), which counts
    # every lot regardless of status.
    #
    # Honest consequence: the leak-free version carries almost no signal
    # (std ~3.2 around a mean of ~57 for both classes). It is kept because it
    # is a genuine resource-allocation descriptor and costs nothing, NOT
    # because it is expected to predict delay. Real delay signal has to come
    # from machine health / worker availability reaching these nodes through
    # the graph — which requires the edge-direction fix in edge_builder.py.
    if df_assignments is not None and not df_assignments.empty:
        true_parallelism = (
            df_assignments.groupby(['production_order_id', 'operation_id'])['asset_id']
            .nunique().rename('all_lot_machine_count').reset_index()
        )
        merged = pd.merge(merged, true_parallelism,
                          on=['production_order_id', 'operation_id'], how='left')
        parallelism_source = merged['all_lot_machine_count']
    else:
        logger.warning(
            "build_operation_node_features() called without df_assignments — falling back to "
            "the Done-filtered active_machine_count, which LEAKS the delay label (99.4%% "
            "single-threshold accuracy vs a 72.7%% baseline). Pass "
            "database.queries.fetch_operation_assignments() instead."
        )
        parallelism_source = merged['active_machine_count']

    # Normalized against GA_LOTS_PER_OPERATION (utils/config.py), not a
    # literal 60.0 -- this is the same "how many parallel lots is an
    # operation actually split into" quantity ga/scheduler.py uses; a
    # literal here would silently desync from that config value if it's
    # ever tuned. FULL_CODEBASE_AUDIT.md Section 5b.
    machine_parallelism_norm = (
        parallelism_source / float(GA_LOTS_PER_OPERATION)
    ).fillna(0).clip(upper=1.0).values.reshape(-1, 1)

    # Pool occupancy (model/feature audit step 3, see chat) -- how many
    # OTHER operations of this operation's own type actually shared a
    # physical machine with it, across all real lots. See
    # _compute_pool_occupancy's own docstring for why this is built from
    # df_assignments (leak-free) rather than planned_start/end timing
    # (which would need the Done-filtered df_schedule or the LABEL-ONLY
    # df_actual_vs_planned -- both rejected). /1000.0 and clip upper=3.0
    # match this file's existing normalization convention (a fixed scale
    # from the real measured range -- mean 727, std 303, max 1,872 on the
    # live database -- not a data-fitted one), so a typical operation
    # lands near 0.5-0.8 rather than saturating the clip immediately.
    if df_assignments is not None and not df_assignments.empty:
        optype_by_key_for_occupancy = {
            key: machine_type_to_operation_type(rmt)
            for key, rmt in zip(merged['unique_op_key'], merged['required_machine_type'])
        }
        occupancy_map = _compute_pool_occupancy(df_assignments, optype_by_key_for_occupancy)
        _occupancy_raw = merged['unique_op_key'].map(occupancy_map)
        pool_occupancy_norm = (
            _occupancy_raw.fillna(0.0) / 1000.0
        ).clip(upper=3.0).values.reshape(-1, 1)
        has_assignment_history = _occupancy_raw.notna().astype(float).values.reshape(-1, 1)
    else:
        pool_occupancy_norm = np.zeros((len(merged), 1))
        has_assignment_history = np.zeros((len(merged), 1))

    # Deadline proximity: how much time budget this ORDER was given from
    # release to its customer deadline. A property of the order, fixed
    # regardless of when the graph happens to be built (unlike a live
    # machine/worker status snapshot) -- both order_deadline and release_date
    # are known at order-entry time, long before any of its operations run,
    # so this carries no temporal-alignment risk. Same value across every
    # operation of one order by design; a tight budget is still informative
    # per-operation since it structurally raises risk everywhere in that
    # order's routing, not just at one step.
    deadline_budget_days = (
        pd.to_datetime(merged['order_deadline']) - pd.to_datetime(merged['release_date'])
    ).dt.total_seconds() / 86400.0
    deadline_budget_norm = (deadline_budget_days / 60.0).fillna(0.0).clip(lower=0.0, upper=3.0).values.reshape(-1, 1)

    # Dispatch-context features (machine health / employee availability /
    # queue congestion AS OF DISPATCH) -- see _aggregate_dispatch_context and
    # PROJECT_STATUS.txt Section 0E. Missing rows (no dispatch_context passed,
    # or an operation absent from it) default to 0 -- "no evidence", not
    # "known safe" -- matching this file's existing missing-data convention.
    dispatch_agg = _aggregate_dispatch_context(df_dispatch_context)
    merged = pd.merge(merged, dispatch_agg, on=['production_order_id', 'operation_id'], how='left')
    # frac_employee_leave (the old frac_dispatched_employee_on_leave, an
    # across-all-lots MEAN) REMOVED from the feature vector -- model/feature
    # audit (see chat). Two independent methods on the live database
    # confirmed this wasn't a measurement artifact: it has real, healthy
    # variance (71.2% of operations nonzero, 88 distinct values, std 0.073
    # -- not the "no variance to learn from" failure mode that killed the
    # old machine_parallelism_norm), yet permutation importance on the
    # trained model showed ~zero effect on all three heads (delay
    # 0.0001+-0.0004, completion -0.0009+-0.0009, priority 0.0002+-0.0005 --
    # every value smaller than its own noise band). That measurement predates
    # both the delay-label causal-mechanism fix and this aggregation-mismatch
    # fix -- decisive_lot_employee_on_leave (below, still excluded from the
    # feature tensor) is a DIFFERENT quantity (a specific lot's 0/1 status,
    # not a mean) and its importance must be re-measured under the corrected
    # label/feature pair before any reinstatement decision, per the user's
    # explicit instruction -- not assumed to inherit the old number.
    # /200.0: queue_delay_mins_at_dispatch is unbounded above (a lot can wait
    # indefinitely for a busy machine), unlike the leave flag which is
    # already 0-1 -- normalized against a fixed scale in the same style as
    # sam_norm/qty_norm above rather than a data-fitted one, and clipped so a
    # rare extreme wait doesn't dominate the vector.
    mean_queue_delay_norm = (
        merged['mean_queue_delay_mins'].fillna(0.0) / 200.0
    ).clip(upper=3.0).values.reshape(-1, 1)
    # MISSINGNESS INDICATOR (audit #6). Every dispatch-context column below
    # is fillna(0.0)'d, which tells the model "no queue, no breakdowns" --
    # the most OPTIMISTIC possible reading -- for an operation that simply
    # has no execution history yet. Every operation in training has history
    # (occupancy mean ~0.73 normalized); a brand-new order, which is the
    # system's actual use case, would arrive with 0.0 across all four and be
    # scored as a uniquely healthy operation.
    #
    # CHOSEN: an explicit indicator, NOT train-mean imputation. Three
    # reasons. (1) Imputation would need the train split, but the split is
    # derived from merged_ops which this function produces -- the ordering is
    # circular, and breaking it means either two passes or persisting values
    # that can then drift. (2) A persisted imputation constant is exactly the
    # class of stale, data-fitted number already found elsewhere in this file
    # (REAL_EFFICIENCY_FALLBACK_NORM, measured on the old 15,066-worker DB).
    # (3) An indicator is strictly more informative: imputing the train mean
    # still asserts a value that was never observed, whereas the indicator
    # lets the heads learn their own response to absence. The zero-fill stays
    # underneath it as the neutral numeric placeholder.
    # Reads the pre-fill flag (Batch-4 fix 10a), NOT .notna() on a column
    # that has already been zero-filled -- that only told us whether the
    # left-merge matched, never whether the underlying context existed.
    has_dispatch_context = (
        merged['context_observed'].fillna(0.0).astype(float).values.reshape(-1, 1)
        if 'context_observed' in merged.columns
        else merged['mean_queue_delay_mins'].notna().astype(float).values.reshape(-1, 1)
    )
    # /5.0: the DECISIVE LOT's (max planned_end_time -- see
    # _aggregate_dispatch_context) machine's cumulative PRIOR breakdown count,
    # not an across-all-lots mean (aggregation-mismatch fix, see chat) --
    # machines that break down at all in this database do so ~1-9 times
    # across the whole simulated run (measured mean 3.1, max 9), so /5.0
    # keeps a typical value near 0.5-0.6 rather than saturating the clip
    # immediately. Replaces frac_dispatched_machine_down, which measured
    # identically 0.0 for every operation in this database -- see
    # PROJECT_STATUS.txt Section 0E / reversal 8. Renamed from
    # mean_breakdown_count_norm to decisive_lot_breakdown_count_norm --
    # "mean" would now be a misnomer.
    # DERIVED AT BUILD TIME FROM THE TRAIN SPLIT ONLY (audit #13).
    #
    # This divisor was the literal 5.0, justified by "measured mean 3.1, max
    # 9" -- statistics taken on the 15,066-worker database that has since
    # been reseeded. A data-fitted constant frozen in source goes stale at
    # every reseed, which is the same trap REAL_EFFICIENCY_FALLBACK_NORM is
    # still sitting in. Derived per build instead, so a reseed recalibrates
    # it automatically, and persisted in the split manifest so inference
    # reuses the training run's value rather than refitting to whatever
    # batch it happens to see.
    #
    # TRAIN-ONLY, not whole-population: a scale fitted over train+val is a
    # (mild) transductive statistic, and this project has already paid for
    # one leak of exactly that shape. The split is by ORDER and
    # _build_split_masks depends only on the SET of order ids -- not on node
    # ordering -- so calling it here yields precisely the partition
    # graph_builder computes later, with no circular dependency on the
    # features this function is producing.
    breakdown_scale = _derive_breakdown_scale(
        merged, 'decisive_lot_breakdown_count_at_dispatch', supplied=breakdown_scale)
    # Recorded on the function so graph_builder can persist it without
    # changing this function's already-wide return signature.
    build_operation_node_features._last_scale = breakdown_scale
    logger.info(f"Breakdown-count normaliser (train-derived): {breakdown_scale:.4f}")
    decisive_lot_breakdown_count_norm = (
        merged['decisive_lot_breakdown_count_at_dispatch'].fillna(0.0) / breakdown_scale
    ).clip(upper=3.0).values.reshape(-1, 1)
    # mean_breakdown_count_norm: completion's own aggregation of the same raw
    # column, restored ADDITIONALLY alongside decisive_lot_breakdown_count_norm
    # (delay-label fix trade-off decision, option (b) -- see
    # DELAY_LABEL_FIX_PROPOSAL.md's "IMPLEMENTATION STATUS" and this file's
    # _aggregate_dispatch_context docstring). Same /5.0 clip-3.0 convention as
    # the decisive-lot version, since it's the same underlying quantity
    # (breakdown count) at a different aggregation.
    mean_breakdown_count_norm = (
        merged['mean_breakdown_count_at_dispatch'].fillna(0.0) / breakdown_scale
    ).clip(upper=3.0).values.reshape(-1, 1)

    # Routing position (graph-module review, step 3) -- independently
    # flagged as a gap by both this project's own structure audit and the
    # external review before either party knew of the other's finding: with
    # routing length now genuinely varying 6-10 steps by garment type
    # (routing_tables.py), "how far into its own order's routing is this
    # operation" is a real, variable quantity nothing in the graph encoded
    # directly -- only implicit through precedence-edge topology. Computed
    # relative to each operation's OWN routing length (not a fixed /10
    # scale) so step 3-of-6 and step 5-of-10 are both legible as "about
    # halfway", which a fixed-scale index would not make comparable across
    # routing families of different lengths.
    routing_length = merged.groupby('production_order_id')['operation_sequence_number'].transform('count')
    routing_step_norm = (
        merged['operation_sequence_number'] / routing_length
    ).fillna(0.0).values.reshape(-1, 1)
    steps_remaining_norm = (
        (routing_length - merged['operation_sequence_number']) / routing_length
    ).fillna(0.0).values.reshape(-1, 1)

    features = np.hstack((
        sam_norm, qty_norm, machine_parallelism_norm, deadline_budget_norm,
        decisive_lot_breakdown_count_norm, mean_queue_delay_norm,
        routing_step_norm, steps_remaining_norm, pool_occupancy_norm,
        mean_breakdown_count_norm,
        # Missingness indicators, paired with the zero-filled columns above:
        # has_dispatch_context covers mean_queue_delay/both breakdown
        # columns (one left-merge, so they are absent together);
        # has_assignment_history covers pool_occupancy, which comes from
        # df_assignments instead. See the note at has_dispatch_context.
        has_dispatch_context, has_assignment_history,
    ))
    mapping = {key: idx for idx, key in enumerate(merged['unique_op_key'].values)}

    # operation_type (graph-module review, step 3) -- also independently
    # flagged by both the structure audit and the external review: no
    # feature encoded which of the 5 (soon possibly more) canonical
    # machine/operation types an operation itself requires; it only shaped
    # the graph's TOPOLOGY (which machine nodes connect, which disjunctive
    # pool it lands in), never appeared in the operation's own feature
    # vector. Returned as an INTEGER INDEX, not one-hot or folded into the
    # float feature tensor above -- models/gnn.py turns this into a learned
    # embedding, per instruction, specifically because a one-hot's width is
    # fixed at build time while an embedding table can be grown (new rows)
    # if a 6th type is ever added, without changing every one-hot-shaped
    # input elsewhere. Index resolution goes through the single shared
    # utils/machine_taxonomy.py mapping (operation_type_index) -- not a
    # second, independently-computed vocabulary -- unmapped/unknown types
    # resolve to the reserved UNKNOWN_OPERATION_TYPE_INDEX rather than
    # crashing.
    op_type_idx = torch.tensor(
        merged['required_machine_type'].apply(
            lambda t: operation_type_index(machine_type_to_operation_type(t))
        ).values,
        dtype=torch.long,
    )

    return torch.tensor(features, dtype=torch.float), mapping, merged, op_type_idx