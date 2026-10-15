import torch
import pandas as pd
import numpy as np

# Fraction of an operation's own PLANNED duration its actual completion must
# exceed to count as "delayed" for GNN training purposes. Relative, not a
# fixed minute count, because operations range from ~30 minutes to several
# hours -- an absolute threshold would make short operations nearly
# impossible to trip while long ones clear it trivially. Endorsed default:
# 20% over plan. See PROJECT_STATUS.txt Section 0D.
DELAY_LABEL_LATENESS_FRACTION = 0.20


def _aggregate_actual_vs_planned(df_actual_vs_planned: pd.DataFrame) -> dict:
    """
    {unique_op_key: (lateness_minutes, mean_lot_duration_minutes,
                     lots_observed, lots_total)}.

    LATENESS IS OPERATION-LEVEL: max(actual_end) - max(planned_end).
    That is, WHEN THE OPERATION ACTUALLY FINISHED versus WHEN IT WAS
    PLANNED TO FINISH. The decisive lot is the one that finished LAST (max
    actual end) -- not the lot with the largest individual overrun.

    *** WHAT THIS LABEL NOW MEASURES: QUEUEING, NOT EXECUTION VARIANCE. ***
    This head was designed to detect execution overrun -- work taking longer
    than planned. On the current dataset it does not primarily measure that.
    Decomposing per-lot lateness exactly (residual 0.000000 min, n=40,000):

        actual_start - planned_start     (dispatch/queue wait)  88.5%
        actual_pause - planned_pause     (calendar/shift wait)  11.4%
        actual_prod  - planned_prod      (execution + repair)    0.1%

    of the total lateness variance. So a positive label overwhelmingly means
    "this operation could not START when planned", not "this operation RAN
    long". The cause is the seeder's move from non-cascading synthetic
    actuals to an independently resource-constrained, CASCADING actual
    timeline, where a late lot displaces the next one (PROJECT_STATUS.txt
    Section 0J). Measured: lots dispatched with zero queue fell 77.3% ->
    24.7%; median queue delay 0.0 -> 418.8 min.

    This is arguably closer to how a real floor runs -- queueing, not
    execution spread, is what makes garment orders late -- and it lifted the
    positive rate from 10.86% to 28.33%, which is far healthier for
    training. But do NOT describe this head as an execution-variance
    predictor, and do not interpret its features as if it were one.

    The DENOMINATOR is unaffected and still means what it always did: mean
    lot PLANNED elapsed duration, median 53.6 -> 57.6 min (1.1x), p25-p75
    40.8-60.7 min. 98.1% of lots carry zero planned pause, so the 20%
    threshold remains a ~30-70 minute execution-scale bar; only its p95/p99
    tail fattened.

    This distinction was previously stated ambiguously here ("driven by
    whichever lot's variance draw pushed the operation's overall completion
    latest"), which reads as per-lot overrun and caused a change to
    max(actual_end - planned_end) per lot. That was reverted; the formula
    was right and this docstring was the actual bug. Recording why, so it
    is not re-litigated:

      * A lot that overran but still finished BEFORE the operation's last
        lot delayed nothing. It does not move downstream precedence, order
        completion, or a customer promise -- all of which depend on when
        the OPERATION completed, which is what this measures.
      * max(a - p) is a max-of-N statistic and therefore inherits the lot
        count: across ~20 lots it almost always finds one lot more than
        20% late, so a 60-lot operation would be structurally likelier to
        be labelled delayed than a 3-lot one regardless of real
        performance. That is the mirror image of the CLT-averaging
        degeneracy this label was designed to escape (Section 0D) --
        averaging crushed the spread to zero, max inflates it toward one.
        Measured: switching to max(a - p) moved the positive rate from
        10.86% to 73.61% on 8,802 operations.
      * The feature alignment agrees. graph/node_features.py picks each
        operation's DECISIVE LOT as its max-planned-end lot, and that lot
        matches the max-ACTUAL-end lot (this formula's decisive concept)
        in 96.0% of operations, versus only 8.1% agreement with the
        max-lateness lot. The existing feature alignment was built
        correctly for these operation-level semantics and is left alone.

    lots_observed / lots_total support the delay head's validity mask
    (label-audit fix 2): an operation is only a usable delay example when
    EVERY one of its lots has a recorded actual outcome. See
    extract_gnn_labels for why a partially-observed operation is masked
    rather than labelled from the lots that happen to be present.

    CORRECTED DESIGN -- the first version of this function averaged each
    lot's OWN lateness (actual_end - planned_end) across all ~60 lots of an
    operation. Measured against this database, that was a second, different
    degenerate label: mean_lateness_mins concentrated within +-2% of a lot's
    own duration (std ~1.7%) by the Central Limit Theorem -- averaging ~60
    roughly-independent per-lot variance draws crushes their spread by
    ~sqrt(60), so essentially NO operation's mean ever crossed the 20%
    threshold. Retraining against it produced positive_rate=0.0000 and a
    trivial always-negative classifier (accuracy 1.0, F1 0.0) -- the same
    failure shape as the ORIGINAL execution_status label, just from the
    opposite direction. See PROJECT_STATUS.txt Section 0D for the measured
    numbers.

    Fixed by matching how an operation's completion actually works: with its
    quantity split across ~60 parallel lots (real machine-level parallelism,
    not independent trials), the operation isn't done until its LAST lot
    finishes -- exactly how ga/fitness.py already treats an operation's own
    completion (the max across its chosen machines). So lateness here is
    (latest actual finish) - (latest planned finish) across the operation's
    lots: the operation's own finish time against the operation's own
    planned finish time, not an average that dilutes it away and not the
    largest single-lot overrun (see the operation-level note at the top of
    this docstring for why those are different, and why this one is the
    quantity that matters). The denominator is the MEAN SINGLE-LOT planned duration (the scale
    execution variance actually operates at -- ~30-70 minutes), not the full
    multi-lot span (~500-2500 minutes, since lots queue onto machines over an
    extended window): dividing by the wrong-scale denominator was a second
    bug in the same first version, diluting even a max-based lateness to
    <5% for every operation in this database.

    Rows missing actual_start_time/actual_end_time (a database seeded before
    this column existed, or a lot with no recorded outcome for any other
    reason) are dropped rather than treated as on-time -- silently defaulting
    them to "not late" would just be a milder version of the leak this
    replaces.
    """
    if df_actual_vs_planned is None or df_actual_vs_planned.empty:
        return {}

    # EXPECTED LOT COUNT IS TAKEN BEFORE ANY EXCLUSION (Batch-4 fix 10b).
    #
    # lots_total used to be counted AFTER dropping rows with no planned span,
    # so completeness was measured against a denominator that had already
    # removed the very lots it was supposed to notice. An operation whose
    # missing-plan lots were silently excluded then looked fully observed,
    # and the delay-validity mask -- whose entire job is to catch incomplete
    # operations -- passed it.
    _all_rows = df_actual_vs_planned.copy()
    _all_rows['unique_op_key'] = (
        _all_rows['production_order_id'].astype(str) + "_"
        + _all_rows['operation_id'].astype(str)
    )
    lots_expected = _all_rows.groupby('unique_op_key').size()

    # A lot only EXISTS for labelling purposes once it has a planned span --
    # that is what "actual vs planned" is measured against.
    df_all = df_actual_vs_planned.dropna(
        subset=['planned_start_time', 'planned_end_time']
    ).copy()
    if df_all.empty:
        return {}

    df_all['unique_op_key'] = (
        df_all['production_order_id'].astype(str) + "_" + df_all['operation_id'].astype(str)
    )
    # Guards against a zero/negative single-lot span turning any lateness at
    # all into an infinite or undefined fraction.
    df_all['lot_duration_mins'] = (
        (df_all['planned_end_time'] - df_all['planned_start_time']).dt.total_seconds() / 60.0
    ).clip(lower=1.0)

    # Counted from the PRE-EXCLUSION frame above, so lots dropped for a
    # missing plan still count against completeness (fix 10b).
    lots_total = lots_expected

    # Only lots with a recorded outcome can contribute a lateness value.
    df_obs = df_all.dropna(subset=['actual_start_time', 'actual_end_time']).copy()
    if df_obs.empty:
        return {}

    agg = df_obs.groupby('unique_op_key').agg(
        latest_actual_end=('actual_end_time', 'max'),
        latest_planned_end=('planned_end_time', 'max'),
        mean_lot_duration_mins=('lot_duration_mins', 'mean'),
        lots_observed=('actual_end_time', 'size'),
    )
    agg['lateness_mins'] = (
        (agg['latest_actual_end'] - agg['latest_planned_end']).dt.total_seconds() / 60.0
    )
    agg['lots_total'] = lots_total.reindex(agg.index).fillna(0).astype(int)

    return {
        key: (
            row['lateness_mins'],
            row['mean_lot_duration_mins'],
            int(row['lots_observed']),
            int(row['lots_total']),
        )
        for key, row in agg.iterrows()
    }


def _aggregate_planned_span(df_actual_vs_planned: pd.DataFrame) -> dict:
    """
    {unique_op_key: (planned_start_min, planned_end_max)} across ALL lots of
    an operation, unfiltered -- for the COMPLETION head (Head 2).

    FIXES a real bug found by the Section 0H database/queries.py sweep: Head
    2 previously read planned_start_time/planned_end_time from merged_ops,
    which is populated (via graph/node_features.py's agg_schedule) from
    database.queries.fetch_current_shop_schedule() -- a query that EXCLUDES
    'Done' rows, for good reason in ITS OWN context (a live WIP/bottleneck
    view), but silently wrong here: for a PARTIALLY-DONE operation, excluding
    the Done lots understates planned_start_time.min() (the earliest lots,
    typically processed first, are exactly the ones most likely Done), which
    understates the computed duration. Measured directly against the live
    database: 2,783 of 7,500 operations (37.1%) are partially done, and for a
    2,000-operation sample of those, the Done-filtered duration was on
    average 76.5 minutes SHORTER than the true all-lots duration (53.4% of
    the sample showed a measurable, >1 minute, distortion; some by over 24
    hours). The DELAY head was never affected -- it already sources from this
    same unfiltered df_actual_vs_planned via _aggregate_actual_vs_planned
    above. This applies the identical fix to Head 2. See PROJECT_STATUS.txt
    Section 0H.4.
    """
    if df_actual_vs_planned is None or df_actual_vs_planned.empty:
        return {}

    df = df_actual_vs_planned.dropna(subset=['planned_start_time', 'planned_end_time']).copy()
    if df.empty:
        return {}

    df['unique_op_key'] = (
        df['production_order_id'].astype(str) + "_" + df['operation_id'].astype(str)
    )
    agg = df.groupby('unique_op_key').agg(
        planned_start_min=('planned_start_time', 'min'),
        planned_end_max=('planned_end_time', 'max'),
    )
    return {
        key: (row['planned_start_min'], row['planned_end_max'])
        for key, row in agg.iterrows()
    }


def _slack_days_for(order_deadline, planned_start, release_date):
    """
    Raw slack, in days, between an order's deadline and a reference point
    for Head 3 (PROJECT_STATUS.txt Section 0H.10). Returns None -- not 0.0
    -- when there's no usable deadline/reference at all, so a genuinely
    zero-slack order and a no-evidence operation can never collapse to the
    same value.

    Reference point:
      1. Preferred -- this OPERATION's own planned_start_time (from the
         same leak-free, unfiltered planned-span source Head 2 already
         uses, see _aggregate_planned_span). Varies WITHIN an order: a
         later step genuinely has less runway left than an earlier one on
         the same order, which a single per-order number can't express.
      2. Fallback -- the ORDER's release_date, when this specific
         operation has no recorded planned span. Same two columns
         graph/node_features.py's own deadline_budget_norm feature already
         uses, so still a real, order-differentiated signal even without
         per-operation resolution.

    NOT run through agent/fuzzy_logic.py::urgency_score. That was the first
    approach and was measured against the real database before shipping:
    urgency_score's day-scale anchors (SLACK_URGENT_FULL=3,
    SLACK_RELAXED_FULL=20 -- tuned for the LIVE breakdown-triage agent's
    real-time judgment) collapsed 97.8% of this dataset's 7,485 real
    operations to an identical 0.0. Every order here has AT LEAST 21 days
    of release-to-deadline budget, and even the single most time-pressured
    operation in the whole dataset never had less than ~14.6 days of slack
    at its own planned start -- the anchors don't span this dataset's
    actual scale, so almost nothing ever left their flat-zero tail. See
    _percentile_rank_priority below for what replaced it.
    """
    if order_deadline is None or pd.isnull(order_deadline):
        return None
    reference = planned_start if (planned_start is not None and pd.notnull(planned_start)) else release_date
    if reference is None or pd.isnull(reference):
        return None
    return (pd.to_datetime(order_deadline) - pd.to_datetime(reference)).total_seconds() / 86400.0


def _percentile_rank_priority(slack_by_idx: dict, num_nodes: int) -> np.ndarray:
    """
    Converts raw slack-in-days (_slack_days_for) into a [0,1] Head 3 label
    by PERCENTILE RANK within this call's own operation set, instead of
    fixed day-scale anchors. Self-calibrating by construction: whichever
    operation has the least slack in THIS dataset always ranks near 1.0 and
    whichever has the most always ranks near 0.0, so there is no day-scale
    constant here that can ever again mismatch the data the way
    urgency_score's fixed anchors did (see _slack_days_for's docstring).

    Operations with no valid slack default to 0.5 -- the same neutral "no
    evidence" value every downstream consumer of a missing priority
    prediction already falls back to (ga/fitness.py, agent/
    decision_engine.py: {'priority': 0.5}) -- and are EXCLUDED from the
    ranking population itself, so missing data can't distort the
    percentiles of the operations that do have real slack.

    Fewer than 2 distinct slack values (a single-operation call, or every
    valid operation sharing one order's release_date with nothing to
    differentiate them) means there is nothing meaningful to rank against;
    every operation defaults to 0.5 rather than an arbitrary 1.0/0.0
    tie-break.
    """
    out = np.full((num_nodes, 1), 0.5, dtype=np.float32)
    if len(slack_by_idx) < 2:
        return out
    idxs = list(slack_by_idx.keys())
    values = pd.Series([slack_by_idx[i] for i in idxs])
    n = len(values)
    if values.nunique() < 2:
        return out
    # (rank - 1) / (n - 1): a true 0..1 span, min slack -> 0.0, max slack ->
    # 1.0 exactly. rank(pct=True) (rank / n) was tried first and rejected --
    # it scales 1/n .. 1.0, so even the single most urgent operation in a
    # small batch never actually reaches priority 1.0 (e.g. n=2 tops out at
    # 0.5), undermining the whole point of this design (full [0,1] usage
    # regardless of batch size). Ties get the average rank, same as before.
    ranks = values.rank(method='average').values  # 1..n
    percentile = (ranks - 1) / (n - 1)
    priority = 1.0 - percentile
    for i, p in zip(idxs, priority):
        out[i, 0] = p
    return out


# Resolution of the persisted slack->percentile curve. 101 points = the
# 0th, 1st, ... 100th percentile of the TRAINING slack distribution, i.e.
# 1-percentile resolution. Enough to reproduce the empirical CDF closely
# while staying small enough to store in a JSON manifest and reapply to a
# single new order at inference.
PRIORITY_CUTPOINT_POINTS = 101


def derive_priority_cutpoints(slack_values, n_points: int = PRIORITY_CUTPOINT_POINTS):
    """
    The slack->percentile curve, derived from ONE population (in production:
    the TRAINING split only) and then frozen.

    LABEL-AUDIT FIX 1. _percentile_rank_priority above ranks every operation
    against every other operation in the same call, and extract_gnn_labels
    used to call it over the WHOLE graph before the train/val split existed.
    Three things were wrong with that:
      1. Cross-split leakage -- a validation operation's label was its rank
         among the training operations too, so the val label encoded the
         training distribution.
      2. Batch-relative meaning -- the same order got a different label as
         the order book changed, because the label was a property of the
         population, not of the operation.
      3. Not computable at inference -- scoring one genuinely new order
         would require re-ranking it against a population you no longer have.
    Freezing the curve on the training split fixes all three: val is scored
    against a curve it did not participate in, the curve is a stable
    artifact, and a single new order can be scored against it later.

    Returns a list of n_points slack values (the quantiles of the input),
    ascending. Paired with apply_priority_cutpoints below. Returns None when
    there is nothing rankable (fewer than 2 distinct slack values), matching
    _percentile_rank_priority's own "nothing meaningful to rank" rule.
    """
    values = np.asarray([v for v in slack_values if v is not None], dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 2 or np.unique(values).size < 2:
        return None
    return np.quantile(values, np.linspace(0.0, 1.0, n_points)).tolist()


def apply_priority_cutpoints(slack_by_idx: dict, cutpoints, num_nodes: int) -> np.ndarray:
    """
    Scores operations against an already-derived (frozen) slack->percentile
    curve from derive_priority_cutpoints.

    priority = 1 - percentile(slack): least slack -> 1.0, most slack -> 0.0,
    the same orientation and [0,1] span _percentile_rank_priority produces.
    Slack outside the training range clamps to the end of the curve (np.interp
    does this by default), which is the correct behaviour for an unseen
    order more extreme than anything in training -- it is simply maximally
    (or minimally) urgent, not an error.

    Operations with no valid slack default to 0.5, unchanged from
    _percentile_rank_priority -- the same neutral "no evidence" value every
    downstream consumer already falls back to. A None/empty curve means the
    training split had nothing rankable, so everything gets 0.5.
    """
    out = np.full((num_nodes, 1), 0.5, dtype=np.float32)
    if not cutpoints or len(cutpoints) < 2 or not slack_by_idx:
        return out
    curve = np.asarray(cutpoints, dtype=float)
    percentile_grid = np.linspace(0.0, 1.0, len(curve))
    idxs = list(slack_by_idx.keys())
    slacks = np.asarray([slack_by_idx[i] for i in idxs], dtype=float)
    percentile = np.interp(slacks, curve, percentile_grid)
    priority = 1.0 - percentile
    for i, p in zip(idxs, priority):
        out[i, 0] = p
    return out


def extract_gnn_labels(merged_ops: pd.DataFrame, op_mapping: dict,
                        df_actual_vs_planned: pd.DataFrame = None,
                        train_mask=None, priority_cutpoints=None) -> tuple:
    """
    Extracts Ground Truth targets using the pre-aggregated operations dataframe.

    df_actual_vs_planned: database.queries.fetch_actual_vs_planned_timing()'s
        output -- every lot's planned vs. synthetic perturbed (actual_*)
        timing, unfiltered by execution_status or by the seeder's now_time
        snapshot. This is what the delay head is now derived from.

        The corrected two-phase simulator freezes the complete plan first:
        resource identities, planned timestamps and predecessor readiness use
        planned-only state. Phase B then advances independent actual machine,
        employee and predecessor clocks on those frozen assignments. Full
        staging proof over 176,187 rows showed that changing only the actual
        seed leaves the normalized planned projection identical.

        PREVIOUSLY the delay label was `1.0 if execution_status in
        ['Delayed', 'Failed'] else 0.0`. execution_status only turns
        Delayed/Failed for lots the seeder's snapshot clock considers already
        started (start_t <= now_time) -- anything scheduled later is always
        'Scheduled' regardless of its actual risk, which measured against
        this database made the label ~99.9% equivalent to "has this
        operation started" rather than a genuine risk signal (delay
        classifier: F1 0.0, AP ~= positive rate, ROC AUC ~= 0.5). See
        PROJECT_STATUS.txt Section 0D. None (the default) reproduces the
        old fallback of "no evidence -> not delayed" for every operation
        rather than raising, so a caller that hasn't updated still gets a
        result, just an all-zero delay label -- now correctly accompanied by
        an all-False delay_valid mask, so "no evidence" trains on nothing
        instead of training as a fleet of true negatives.

    train_mask: boolean mask (length num_nodes) marking the TRAINING
        operations. When given, Head 3's slack->percentile curve is derived
        from those operations ONLY and then applied to every node -- see
        derive_priority_cutpoints for why deriving it over the whole graph
        leaked the split. Production always passes this (graph_builder.py).
        When omitted, the legacy whole-population ranking is used and
        meta['priority_cutpoints_source'] says so explicitly.

    priority_cutpoints: an already-frozen curve from a previous run (e.g.
        the training run's manifest). Takes precedence over train_mask --
        this is how evaluation and inference score against exactly the curve
        training used, rather than re-deriving a new one.

    Returns (labels, meta):
      labels: {'delay', 'completion', 'priority', 'delay_valid',
        'completion_valid'} tensors.
        delay_valid is the per-operation validity mask for Head 1 (below).
      meta: the derived priority curve and delay-mask bookkeeping, for the
        caller to persist alongside the checkpoint.

    DELAY VALIDITY (label-audit fix 2). Head 1's label is only defined for
    an operation whose lots actually have recorded outcomes. Two distinct
    cases are both masked OUT rather than labelled 0.0:
      * no lot has an actual outcome -- previously these kept the np.zeros
        default and trained as true negatives, which this file's own
        _aggregate_actual_vs_planned docstring already said the design
        exists to avoid.
      * SOME lots have outcomes and others do not. Masked deliberately:
        the label is max(actual_end) - max(planned_end) for the OPERATION,
        so the actual last-finishing lot may be one of the unobserved ones.
        Labelling from the observed subset could understate the operation's
        completion and present a lower bound as truth.
        A lower bound biased toward on-time is not a weaker label, it is a
        wrong one, so these are excluded from both loss and metrics.
    """
    num_nodes = len(op_mapping)
    delay_labels = np.zeros((num_nodes, 1), dtype=np.float32)
    delay_valid = np.zeros((num_nodes, 1), dtype=bool)
    # Head 2 validity (Batch-4 fix 10c). Default True: a completion label is
    # defined for every operation that HAS a planned span, which is the
    # normal case. Cleared only where the span is missing.
    completion_valid = np.ones((num_nodes, 1), dtype=bool)
    n_masked_no_outcome = 0
    n_masked_partial = 0
    n_completion_fallback = 0
    completion_labels = np.zeros((num_nodes, 1), dtype=np.float32)
    # Head 3's raw slack is collected during the loop below and converted to
    # a percentile-rank priority AFTER it, since that conversion needs the
    # full distribution across every operation, not just one row at a time.
    # See _percentile_rank_priority.
    slack_by_idx = {}

    lateness_by_key = _aggregate_actual_vs_planned(df_actual_vs_planned)
    planned_span_by_key = _aggregate_planned_span(df_actual_vs_planned)

    for _, row in merged_ops.iterrows():
        key = row['unique_op_key']
        if key in op_mapping:
            idx = op_mapping[key]

            # Head 1: Binary Delay -- genuine plan-vs-actual lateness, not
            # execution_status. See the df_actual_vs_planned note above.
            lateness = lateness_by_key.get(key)
            if lateness is None:
                # No lot of this operation has a recorded outcome. Masked
                # out entirely -- NOT labelled 0.0. See the DELAY VALIDITY
                # note in this function's docstring.
                n_masked_no_outcome += 1
            else:
                lateness_mins, mean_lot_duration_mins, lots_observed, lots_total = lateness
                if lots_observed < lots_total:
                    # Partially observed: the decisive lot may be one of the
                    # missing ones, so any label computed here is a lower
                    # bound presented as truth. Masked out.
                    n_masked_partial += 1
                else:
                    delay_labels[idx] = 1.0 if (
                        lateness_mins > DELAY_LABEL_LATENESS_FRACTION * mean_lot_duration_mins
                    ) else 0.0
                    delay_valid[idx] = True

            # Head 2: Expected Duration (End - Start) -- sourced from
            # planned_span_by_key (ALL lots, unfiltered), NOT from row's own
            # planned_start_time/planned_end_time. See _aggregate_planned_span.
            span = planned_span_by_key.get(key)
            if span is not None and pd.notnull(span[0]) and pd.notnull(span[1]):
                duration = (span[1] - span[0]).total_seconds() / 60.0
                completion_labels[idx] = max(1.0, duration)
            else:
                # FALLBACK: no planned span recorded for this operation.
                #
                # This was `sam * 50`. The x50 is present in the very first
                # commit with no derivation, and is the same undated,
                # underived constant family as the seeder's old /50.0
                # duration divisor -- which DATA_AUDIT.md Module 5
                # explicitly describes as "invented with no cited
                # derivation" and has since removed
                # (data_generation/seed_database_fixed.py:635,681). Its
                # intended meaning could not be recovered from the code or
                # from git history, so it is not preserved: an unexplained
                # multiplier is not a conversion, and keeping it would just
                # carry the same unexplained number forward.
                #
                # Replaced with the operation's own single-machine work
                # content, sam x target_quantity -- the same quantity
                # ga/scheduler.py calls serial_processing_mins. That is a
                # real, traceable number rather than a magic constant. It is
                # NOT the same quantity as the planned span this label
                # normally carries (a span includes queueing between lots,
                # work content does not), so it is a deliberate
                # approximation for an operation that has no span to
                # measure, and it is counted below so it can never shape
                # the label distribution unnoticed.
                #
                # Clamped to >= 1.0, matching the branch above. The old
                # expression had no clamp, so a missing sam produced a
                # 0-minute completion target -- physically impossible, and
                # a free "instant operation" for the model to fit.
                # MASKED, NOT SUBSTITUTED (Batch-4 fix 10c).
                #
                # The previous fallback wrote sam x target_quantity -- the
                # operation's single-machine WORK CONTENT. The label this
                # head learns is an elapsed SPAN (first lot start to last lot
                # end), which includes queueing between lots. They are
                # different physical quantities, so substituting one for the
                # other teaches the head that some operations have spans
                # equal to their work content, which is never true when lots
                # queue. Better to have no example than a wrong one -- the
                # same reasoning already applied to the delay head, reusing
                # the per-head validity mask built for it.
                #
                # Defensive: this branch fires 0 times on the current data.
                completion_valid[idx] = False
                n_completion_fallback += 1

            # Head 3: raw deadline slack, collected now and converted to a
            # percentile-rank priority once the full distribution is known
            # (see _slack_days_for / _percentile_rank_priority above for the
            # fallback chain -- this operation's own planned_start -> order
            # release_date -- and why this replaced urgency_score()).
            # Reuses `span` from Head 2 above -- both draw from the same
            # leak-free planned-span source.
            planned_start = span[0] if span is not None else None
            slack = _slack_days_for(row.get('order_deadline'), planned_start, row.get('release_date'))
            if slack is not None:
                slack_by_idx[idx] = slack

    # Head 3: score against a FROZEN slack->percentile curve rather than
    # ranking this call's whole population against itself (label-audit fix
    # 1 -- see derive_priority_cutpoints for the three failures that caused).
    if priority_cutpoints is not None:
        cutpoints = list(priority_cutpoints)
        cutpoints_source = 'supplied (frozen from a previous run)'
        priority_labels = apply_priority_cutpoints(slack_by_idx, cutpoints, num_nodes)
    elif train_mask is not None:
        mask_arr = np.asarray(
            train_mask.cpu().numpy() if hasattr(train_mask, 'cpu') else train_mask
        ).ravel().astype(bool)
        train_slacks = [s for i, s in slack_by_idx.items() if i < len(mask_arr) and mask_arr[i]]
        cutpoints = derive_priority_cutpoints(train_slacks)
        cutpoints_source = f'derived from {len(train_slacks)} training operations'
        priority_labels = apply_priority_cutpoints(slack_by_idx, cutpoints, num_nodes)
    else:
        # Legacy whole-population ranking. Correct only when there is no
        # split to leak across (ad-hoc callers, unit tests). Recorded in
        # meta so a caller can never mistake it for the split-safe path.
        cutpoints = None
        cutpoints_source = 'whole population (NOT split-safe)'
        priority_labels = _percentile_rank_priority(slack_by_idx, num_nodes)

    labels = {
        'delay': torch.tensor(delay_labels, dtype=torch.float),
        'completion': torch.tensor(completion_labels, dtype=torch.float),
        'priority': torch.tensor(priority_labels, dtype=torch.float),
        'delay_valid': torch.tensor(delay_valid, dtype=torch.bool),
        'completion_valid': torch.tensor(completion_valid, dtype=torch.bool),
    }
    meta = {
        'priority_cutpoints': cutpoints,
        'priority_cutpoints_source': cutpoints_source,
        'delay_valid_count': int(delay_valid.sum()),
        'delay_masked_no_outcome': n_masked_no_outcome,
        'delay_masked_partial': n_masked_partial,
        # Head 2 operations that had no planned span and fell back to an
        # approximated work-content label. Surfaced so an approximation can
        # never quietly become a large share of the training target.
        'completion_fallback_count': n_completion_fallback,
        'completion_valid_count': int(completion_valid.sum()),
    }
    return labels, meta
