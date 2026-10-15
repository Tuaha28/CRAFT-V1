"""
Regression tests for the five label/metric correctness fixes.

Each test pins the specific defect it replaces, so a future change that
reintroduces the old behaviour fails here rather than silently going back to
producing a better-looking number from a worse label.
"""

import json

import numpy as np
import pandas as pd
import pytest
import torch

from graph.labels import (
    extract_gnn_labels,
    _aggregate_actual_vs_planned,
    derive_priority_cutpoints,
    apply_priority_cutpoints,
)


def _lot(order_id, op_id, planned_start, planned_end, actual_start, actual_end):
    return {
        'production_order_id': order_id,
        'operation_id': op_id,
        'planned_start_time': pd.Timestamp(planned_start),
        'planned_end_time': pd.Timestamp(planned_end),
        'actual_start_time': pd.Timestamp(actual_start) if actual_start else pd.NaT,
        'actual_end_time': pd.Timestamp(actual_end) if actual_end else pd.NaT,
    }


def _merged(order_id, op_id, deadline='2026-02-01', seq=1):
    return {
        'unique_op_key': f"{order_id}_{op_id}",
        'operation_sequence_number': seq,
        'sam': 30,
        'order_deadline': pd.Timestamp(deadline),
        'release_date': pd.Timestamp('2026-01-01'),
    }


# ---------------------------------------------------------------------------
# Lateness is OPERATION-LEVEL: max(actual_end) - max(planned_end).
#
# A change to per-lot max(actual - planned) was implemented and then
# reverted -- a lot that overran but still finished before the operation's
# last lot delayed nothing, and max(a-p) is a max-of-N statistic that
# inherits the lot count. These tests pin the operation-level semantics so
# the same change cannot be reintroduced from the docstring alone.
# ---------------------------------------------------------------------------

def test_lateness_is_operation_level_not_largest_single_lot_overrun():
    # Deliberately constructed so the two candidate decisive lots DIFFER:
    #   Lot A: planned ends 09:00, actual ends 09:05 -> finishes LAST,
    #          overran by 5 min. This is the operation's real finish.
    #   Lot B: planned ends 08:30, actual ends 09:02 -> overran by 32 min,
    #          but finished BEFORE lot A and so delayed nothing.
    # Operation-level lateness = 09:05 - 09:00 = 5 minutes.
    # The largest single-lot overrun would be 32 -- explicitly NOT the label.
    df = pd.DataFrame([
        _lot('1', '1', '2026-01-01 08:00', '2026-01-01 09:00',
             '2026-01-01 08:00', '2026-01-01 09:05'),
        _lot('1', '1', '2026-01-01 08:00', '2026-01-01 08:30',
             '2026-01-01 08:00', '2026-01-01 09:02'),
    ])
    lateness_mins, _, _, _ = _aggregate_actual_vs_planned(df)['1_1']
    assert lateness_mins == pytest.approx(5.0)


def test_a_lot_that_overran_but_finished_early_does_not_make_the_operation_late():
    # Same principle at label level: one lot overruns hugely relative to its
    # own plan, but the operation still finishes when planned. Not delayed.
    df = pd.DataFrame([
        # Finishes last, exactly on plan -> operation is on time.
        _lot('4', '1', '2026-01-01 08:00', '2026-01-01 10:00',
             '2026-01-01 08:00', '2026-01-01 10:00'),
        # Overran its own 30-minute plan by 60 minutes, but still done at 09:30.
        _lot('4', '1', '2026-01-01 08:00', '2026-01-01 08:30',
             '2026-01-01 08:00', '2026-01-01 09:30'),
    ])
    merged = pd.DataFrame([_merged('4', '1')])
    labels, _ = extract_gnn_labels(merged, {'4_1': 0}, df)
    assert float(labels['delay'][0]) == 0.0


def test_operation_level_lateness_never_exceeds_largest_single_lot_overrun():
    # max(a) - max(p) <= max(a - p) always. Spot-check on random multi-lot
    # operations so the relationship is pinned, not just asserted in a
    # docstring -- this is the inequality that makes the per-lot form
    # systematically larger, and therefore the reason it inflated the
    # positive rate from 10.86% to 73.61% when it was briefly adopted.
    rng = np.random.default_rng(0)
    base = pd.Timestamp('2026-01-01 08:00')
    for _ in range(200):
        rows = []
        for _ in range(rng.integers(2, 6)):
            p_start = base + pd.Timedelta(minutes=int(rng.integers(0, 120)))
            p_end = p_start + pd.Timedelta(minutes=int(rng.integers(10, 90)))
            a_end = p_end + pd.Timedelta(minutes=int(rng.integers(-20, 60)))
            rows.append(_lot('9', '9', p_start, p_end, p_start, a_end))
        df = pd.DataFrame(rows)
        operation_level, _, _, _ = _aggregate_actual_vs_planned(df)['9_9']
        per_lot_max = ((df['actual_end_time'] - df['planned_end_time'])
                       .dt.total_seconds() / 60.0).max()
        assert operation_level <= per_lot_max + 1e-9


# ---------------------------------------------------------------------------
# FIX 2 -- missing / partial outcomes are masked, not labelled on-time
# ---------------------------------------------------------------------------

def test_operation_with_no_actuals_is_masked_not_labelled_zero():
    df = pd.DataFrame([_lot('1', '1', '2026-01-01 08:00', '2026-01-01 09:00', None, None)])
    merged = pd.DataFrame([_merged('1', '1')])
    labels, meta = extract_gnn_labels(merged, {'1_1': 0}, df)

    assert bool(labels['delay_valid'][0]) is False, "no-outcome operation must be masked"
    assert meta['delay_masked_no_outcome'] == 1
    assert meta['delay_valid_count'] == 0


def test_partially_observed_operation_is_masked():
    # One lot has an outcome, one does not. The unobserved lot could be the
    # decisive one, so any label here would be a lower bound presented as
    # truth -- masked instead.
    df = pd.DataFrame([
        _lot('2', '1', '2026-01-01 08:00', '2026-01-01 09:00',
             '2026-01-01 08:00', '2026-01-01 09:30'),
        _lot('2', '1', '2026-01-01 08:00', '2026-01-01 09:00', None, None),
    ])
    merged = pd.DataFrame([_merged('2', '1')])
    labels, meta = extract_gnn_labels(merged, {'2_1': 0}, df)

    assert bool(labels['delay_valid'][0]) is False
    assert meta['delay_masked_partial'] == 1
    assert meta['delay_valid_count'] == 0


def test_fully_observed_operation_stays_valid():
    df = pd.DataFrame([
        _lot('3', '1', '2026-01-01 08:00', '2026-01-01 09:00',
             '2026-01-01 08:00', '2026-01-01 09:30'),
        _lot('3', '1', '2026-01-01 08:00', '2026-01-01 09:00',
             '2026-01-01 08:00', '2026-01-01 09:05'),
    ])
    merged = pd.DataFrame([_merged('3', '1')])
    labels, meta = extract_gnn_labels(merged, {'3_1': 0}, df)

    assert bool(labels['delay_valid'][0]) is True
    assert meta['delay_valid_count'] == 1
    # 30 minutes late against a 60-minute mean lot span = 50% > the 20% bar.
    assert float(labels['delay'][0]) == 1.0


# ---------------------------------------------------------------------------
# Completion fallback -- no planned span available
# ---------------------------------------------------------------------------

def test_completion_is_masked_when_no_planned_span_exists():
    # Batch-4 fix 10c: mask rather than substitute.
    merged = pd.DataFrame([{
        'unique_op_key': '5_1', 'operation_sequence_number': 1,
        'sam': 0, 'target_quantity': 0,
        'order_deadline': pd.Timestamp('2026-02-01'),
        'release_date': pd.Timestamp('2026-01-01'),
    }])
    labels, meta = extract_gnn_labels(merged, {'5_1': 0}, None)
    assert bool(labels['completion_valid'][0]) is False
    assert meta['completion_fallback_count'] == 1


def test_no_work_content_substitute_is_written_for_a_masked_operation():
    # The substitute (sam x quantity = 1000 here) must NOT appear: work
    # content is not an elapsed span, and teaching the head otherwise is
    # worse than giving it no example.
    merged = pd.DataFrame([{
        'unique_op_key': '6_1', 'operation_sequence_number': 1,
        'sam': 2.5, 'target_quantity': 400,
        'order_deadline': pd.Timestamp('2026-02-01'),
        'release_date': pd.Timestamp('2026-01-01'),
    }])
    labels, _meta = extract_gnn_labels(merged, {'6_1': 0}, None)
    assert bool(labels['completion_valid'][0]) is False
    assert float(labels['completion'][0]) != pytest.approx(1000.0)


def test_operation_with_a_planned_span_does_not_use_the_fallback():
    df = pd.DataFrame([
        _lot('7', '1', '2026-01-01 08:00', '2026-01-01 09:00',
             '2026-01-01 08:00', '2026-01-01 09:00'),
    ])
    merged = pd.DataFrame([_merged('7', '1')])
    labels, meta = extract_gnn_labels(merged, {'7_1': 0}, df)
    assert meta['completion_fallback_count'] == 0
    assert float(labels['completion'][0]) == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# FIX 1 -- priority cut-points derived from the TRAIN split only
# ---------------------------------------------------------------------------

def test_cutpoints_are_derived_from_training_slacks_only():
    train_slacks = [1.0, 2.0, 3.0, 4.0]
    curve = derive_priority_cutpoints(train_slacks)
    assert curve is not None
    # The curve spans exactly the training range, not anything wider.
    assert curve[0] == pytest.approx(1.0)
    assert curve[-1] == pytest.approx(4.0)


def test_val_operation_is_scored_against_frozen_curve_not_its_own_rank():
    # Training slack spans 10..20 days. A validation operation with 0 days
    # of slack is more urgent than anything in training and must clamp to
    # priority 1.0 -- under the old whole-population ranking its label would
    # have depended on how many other val operations were in the same batch.
    curve = derive_priority_cutpoints([10.0, 12.0, 14.0, 16.0, 18.0, 20.0])
    out = apply_priority_cutpoints({0: 0.0, 1: 20.0, 2: 15.0}, curve, 3)
    assert out[0, 0] == pytest.approx(1.0)   # least slack -> most urgent
    assert out[1, 0] == pytest.approx(0.0)   # most slack  -> least urgent
    assert 0.0 < out[2, 0] < 1.0


def test_priority_label_does_not_change_when_other_operations_are_added():
    # The defect this replaces: a label that is a property of the batch
    # rather than of the operation. With a frozen curve, scoring the same
    # slack alongside different companions must give the same answer.
    curve = derive_priority_cutpoints([5.0, 10.0, 15.0, 20.0])
    alone = apply_priority_cutpoints({0: 12.0}, curve, 1)[0, 0]
    crowded = apply_priority_cutpoints({0: 12.0, 1: 1.0, 2: 99.0}, curve, 3)[0, 0]
    assert alone == pytest.approx(crowded)


def test_train_mask_path_reports_its_source_and_excludes_val_from_the_curve():
    merged = pd.DataFrame([
        _merged('1', '1', deadline='2026-01-11'),   # 10 days slack
        _merged('2', '1', deadline='2026-01-21'),   # 20 days slack
        _merged('3', '1', deadline='2026-06-01'),   # far more slack
    ])
    op_map = {'1_1': 0, '2_1': 1, '3_1': 2}
    train_mask = torch.tensor([True, True, False])

    _, meta = extract_gnn_labels(merged, op_map, None, train_mask=train_mask)

    assert meta['priority_cutpoints'] is not None
    assert '2 training operations' in meta['priority_cutpoints_source']
    # The curve must not extend to the val operation's much larger slack.
    assert max(meta['priority_cutpoints']) < 100.0


def test_no_train_mask_falls_back_to_population_ranking_and_says_so():
    merged = pd.DataFrame([_merged('1', '1'), _merged('2', '1', deadline='2026-03-01')])
    _, meta = extract_gnn_labels(merged, {'1_1': 0, '2_1': 1}, None)
    assert meta['priority_cutpoints'] is None
    assert 'NOT split-safe' in meta['priority_cutpoints_source']


# ---------------------------------------------------------------------------
# FIX 5 -- a mismatched split manifest is rejected, not silently used
# ---------------------------------------------------------------------------

class _FakeBuilder:
    def __init__(self, fingerprint, order_ids):
        self._fingerprint = fingerprint
        self.order_ids_per_node = np.array(order_ids, dtype=object)

    def graph_fingerprint(self):
        return self._fingerprint


class _FakeGraph:
    def __init__(self, train_mask, val_mask):
        self._d = {'operation': type('N', (), {'train_mask': train_mask, 'val_mask': val_mask})()}

    def __getitem__(self, k):
        return self._d[k]


def _manifest(**overrides):
    base = {
        "split_seed": 42,
        "val_fraction": 0.2,
        "num_operation_nodes": 4,
        "num_train_operations": 2,
        "num_val_operations": 2,
        "graph_fingerprint": "abc123",
        "checkpoint_file": "gnn_multitask_latest.pth",
        "checkpoint_sha256": "deadbeef",
        "production_order_ids": ["7", "8"],
    }
    base.update(overrides)
    return base


def _fixture():
    train_mask = torch.tensor([True, True, False, False])
    val_mask = torch.tensor([False, False, True, True])
    builder = _FakeBuilder("abc123", ['1', '1', '7', '8'])
    return builder, _FakeGraph(train_mask, val_mask)


def test_matching_manifest_is_accepted():
    from models.evaluate import _verify_split_manifest
    builder, graph = _fixture()
    val_mask = _verify_split_manifest(
        _manifest(), "m.json", builder, graph, "/nonexistent/checkpoint.pth")
    assert int(val_mask.sum()) == 2


@pytest.mark.parametrize("bad", [
    {"graph_fingerprint": "a-different-graph"},
    {"num_operation_nodes": 9999},
    {"num_train_operations": 3},
    {"num_val_operations": 1},
    {"split_seed": 7},
    {"val_fraction": 0.5},
])
def test_mismatched_manifest_is_rejected(bad):
    from models.evaluate import _verify_split_manifest, SplitManifestError
    builder, graph = _fixture()
    with pytest.raises(SplitManifestError):
        _verify_split_manifest(
            _manifest(**bad), "m.json", builder, graph, "/nonexistent/checkpoint.pth")


def test_manifest_whose_order_ids_no_longer_match_is_rejected():
    from models.evaluate import _verify_split_manifest, SplitManifestError
    builder, graph = _fixture()
    # Right counts, but the recorded orders are not in this graph any more.
    with pytest.raises(SplitManifestError, match="order book has changed"):
        _verify_split_manifest(
            _manifest(production_order_ids=["555", "666"]),
            "m.json", builder, graph, "/nonexistent/checkpoint.pth")


def test_missing_manifest_raises_rather_than_falling_back(tmp_path, monkeypatch):
    import models.evaluate as ev
    monkeypatch.setattr(ev, "OUTPUT_DIR", str(tmp_path))
    with pytest.raises(ev.SplitManifestError, match="not found"):
        ev._load_split_manifest()


def test_checkpoint_hash_mismatch_is_rejected(tmp_path):
    from models.evaluate import _verify_split_manifest, SplitManifestError
    builder, graph = _fixture()
    ckpt = tmp_path / "gnn_multitask_latest.pth"
    ckpt.write_bytes(b"some other weights")
    with pytest.raises(SplitManifestError, match="checkpoint_sha256"):
        _verify_split_manifest(_manifest(), "m.json", builder, graph, str(ckpt))
