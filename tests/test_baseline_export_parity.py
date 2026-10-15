"""
run_rolling_horizon_baseline must be able to run the SAME worker-assignment
export path the GA arm uses.

Without it, a baseline's staffing feasibility is UNMEASURED, not zero -- and a
report showing the GA's shortage counts beside baselines that were never
assessed invites a cross-arm reading the data cannot support. These tests pin
the parity, and pin that paired arms do not share reservation state.
"""

import inspect

import pytest

from ga import rolling_horizon


def test_baseline_runner_accepts_the_same_export_parameters_as_the_ga_runner():
    ga_params = set(inspect.signature(rolling_horizon.run_rolling_horizon).parameters)
    bl_params = set(inspect.signature(rolling_horizon.run_rolling_horizon_baseline).parameters)
    for p in ('export', 'decision_engine', 'operator_index', 'initial_operator_reservations'):
        assert p in ga_params, p
        assert p in bl_params, f"baseline runner cannot be exported: missing {p}"


def test_baseline_export_defaults_off_so_existing_callers_are_unchanged():
    sig = inspect.signature(rolling_horizon.run_rolling_horizon_baseline)
    assert sig.parameters['export'].default is False
    assert sig.parameters['decision_engine'].default is None
    assert sig.parameters['operator_index'].default is None
    assert sig.parameters['initial_operator_reservations'].default is None


def test_baseline_result_always_carries_an_export_summaries_key():
    # The paired report reads result['export_summaries'] for both arms. A
    # baseline that omitted the key entirely would raise, or worse, be
    # papered over with .get(...) and silently report zero shortages for an
    # arm that was never assessed.
    src = inspect.getsource(rolling_horizon.run_rolling_horizon_baseline)
    assert "'export_summaries': export_summaries" in src


def test_baseline_export_decodes_against_window_start_state_not_end_state():
    # The GA path had this exact bug once: exporting against the post-window
    # machine state decodes the schedule under different conditions than the
    # ones it was scored against. The baseline path must capture the start
    # state BEFORE evaluate_schedule reassigns machine_free.
    src = inspect.getsource(rolling_horizon.run_rolling_horizon_baseline)
    start_capture = src.index('window_start_machine_free = machine_free')
    reassign = src.index("machine_free = det['final_machine_free']")
    assert start_capture < reassign, \
        "window-start machine state must be captured before it is overwritten"
    assert 'initial_machine_free=window_start_machine_free' in src


IMPACT_FIELDS = ('shortfall', 'duration_minutes', 'shortfall_worker_minutes')


def test_understaffed_record_carries_worker_minutes_not_only_headcount():
    # One operation short by 3 operators for 8 hours and one short by 3 for
    # 5 minutes are the same COUNT and wildly different shortages. A paired
    # comparison summed on counts alone would call those equal.
    from agent import decision_engine
    src = inspect.getsource(decision_engine.AIDecisionEngine._assign_operators)
    for field in IMPACT_FIELDS:
        assert f"'{field}'" in src, field


def test_every_understaffed_return_path_carries_the_impact_fields():
    # THE defect this pins: _assign_operators has TWO paths that build an
    # understaffed record -- the fully-unfilled one ("nobody qualified is
    # free") and the partial-crew one. The impact fields were added to the
    # partial-crew path only, so every summed worker-minute figure silently
    # ignored the WORST shortages: a missing key defaults to zero in any
    # caller that sums it, and fully-unfilled operations are exactly the
    # ones short by their entire headcount.
    import ast
    import textwrap
    from agent import decision_engine
    src = textwrap.dedent(inspect.getsource(decision_engine.AIDecisionEngine._assign_operators))
    tree = ast.parse(src)

    def dict_keys(node):
        return {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}

    # Every dict literal that names 'required' and 'available' is an
    # understaffed record, wherever it is built.
    records = [n for n in ast.walk(tree)
               if isinstance(n, ast.Dict) and {'required', 'available'} <= dict_keys(n)]
    assert len(records) >= 2, \
        f"expected both understaffed construction paths, found {len(records)}"
    for i, rec in enumerate(records):
        keys = dict_keys(rec)
        missing = [f for f in IMPACT_FIELDS if f not in keys]
        assert not missing, f"understaffed record #{i + 1} is missing {missing}"


def test_paired_runs_must_not_share_reservation_state():
    # Both arms start from identical committed reservations, but each must
    # evolve its OWN: sharing one mutable dict lets whichever arm runs second
    # inherit the first's bookings, manufacturing an asymmetry that is purely
    # an artefact of execution order.
    import pathlib
    src = pathlib.Path('run_paired_staffing_validation.py').read_text(encoding='utf-8')
    assert src.count('copy.deepcopy(base_reservations)') == 2, \
        "each arm needs its own deep copy of the initial reservations"
    assert 'fetch_committed_operator_reservations(ORIGIN)' in src


def test_paired_script_reports_worker_minutes_and_affected_orders():
    import pathlib
    src = pathlib.Path('run_paired_staffing_validation.py').read_text(encoding='utf-8')
    for token in ('shortfall_worker_minutes', 'affected_orders', 'jaccard'):
        assert token in src, token


# ---------------------------------------------------------------------------
# No zero coverage without a record -- the third instance of this shape
# ---------------------------------------------------------------------------

def test_unresolved_machine_type_is_recorded_not_silently_dropped():
    # THE defect: `if optype is None: return [], 'unfilled', None` handed back
    # an empty crew with NO understaffed record, so the operation contributed
    # its full labour DEMAND to every denominator and nothing to the shortage
    # count. A replay fed 2,579 such operations reported
    # understaffed_operations=0 alongside unfilled/required=84.55% -- a
    # self-contradiction only this path can produce.
    from agent.decision_engine import AIDecisionEngine
    crew, tier, under = AIDecisionEngine._assign_operators(
        {}, 'a-machine-type-that-does-not-exist', 10, 4, {}, 120.0,
        start_dt=None, operator_reservations={})
    assert crew == []
    assert under is not None, 'zero coverage with no understaffed record'
    assert under['required'] == 4 and under['available'] == 0
    assert under['shortfall'] == 4
    assert under['shortfall_worker_minutes'] == pytest.approx(4 * 120.0)
    assert under['limiting_factor'] == 'unresolved-machine-type'


def test_zero_headcount_is_the_only_recordless_path():
    # When no crew is REQUIRED, zero coverage is correct and there is nothing
    # to report. This is the one legitimate exception, pinned so it is not
    # confused with the defect above.
    from agent.decision_engine import AIDecisionEngine
    crew, _tier, under = AIDecisionEngine._assign_operators(
        {}, 'single-needle lockstitch', 10, 0, {}, 120.0,
        start_dt=None, operator_reservations={})
    assert crew == [] and under is None


def test_every_crew_returning_path_records_a_shortfall_or_needs_none():
    # Structural sweep: each `return ..., ..., ...` in _assign_operators must
    # either hand back a record or be guarded by headcount <= 0. A fourth
    # silent-zero path added later fails here.
    import ast
    import inspect
    import textwrap
    from agent import decision_engine
    src = textwrap.dedent(inspect.getsource(
        decision_engine.AIDecisionEngine._assign_operators))
    tree = ast.parse(src)
    recordless = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Tuple):
            continue
        if len(node.value.elts) != 3:
            continue
        third = node.value.elts[2]
        if isinstance(third, ast.Constant) and third.value is None:
            recordless += 1
    assert recordless == 1, (
        f'{recordless} crew-returning paths hand back no understaffed record; '
        f'exactly one (headcount <= 0) is legitimate')


# ---------------------------------------------------------------------------
# Replay input provenance
# ---------------------------------------------------------------------------

def test_replay_validates_row_parity_and_run_identity():
    import pathlib
    src = pathlib.Path('run_staffing_attribution_replay.py').read_text(encoding='utf-8')
    # Run identity, not mtime -- mtime was what let a cross-run pairing through.
    assert 'require_same_run' in src
    assert 'PAIRED_RUN_ID' in src
    # Row parity against the graph's operation count.
    assert 'EXPECTED_OPERATIONS' in src
    assert '!= expected_rows' in src
    # Arm-specific prefixes, so a stale proposed_schedule_w5.json cannot match.
    assert "'paired_ga'" in src and "'paired_edf'" in src
    assert 'contiguous' in src


def test_paired_run_uses_arm_specific_export_labels():
    import inspect
    import pathlib
    from ga import rolling_horizon
    src = pathlib.Path('run_paired_staffing_validation.py').read_text(encoding='utf-8')
    assert "export_label_prefix='paired_ga'" in src
    assert "export_label_prefix='paired_edf'" in src
    # Both runners must accept the knob, and default to the historical scheme
    # so existing single-arm callers are unchanged.
    for fn in (rolling_horizon.run_rolling_horizon,
               rolling_horizon.run_rolling_horizon_baseline):
        params = inspect.signature(fn).parameters
        assert 'export_label_prefix' in params, fn.__name__
        assert params['export_label_prefix'].default is None, fn.__name__
