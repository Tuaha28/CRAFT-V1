"""
Regression tests for PROJECT_STATUS.txt Section 0H.9: the MC-ceiling
protection fix.

Context: 0H.8's soft machine-SELECTION tie-break for the window's most-
congested type measured as a complete no-op -- instrumentation showed
select_machines_and_start never had slack to redistribute, because the MC
(machine-count) gene already saturates the operation's entire per-line pool
on every call (0 of 1,200 protected calls had len(pool) > take). This fix
attacks that saturation directly: ga.schedule_utils.MC_PROTECTION_FRACTION
caps what fraction of its per-line pool a single operation of the protected
type may claim, enforced at the one place both the GA and the baseline
heuristics already get their MC ceiling from --
ChromosomeFactory._max_machines_for -- and re-enforced after crossover/
mutation by repair_line_and_machine_count, so offspring can't drift back
above the cap a freshly-born individual already respects.

Two things are tested: _max_machines_for's fraction scaling (via a real,
tiny ChromosomeFactory -- synthetic DataFrames, no DB), and
repair_line_and_machine_count's identical scaling for the post-crossover/
mutation repair path. Both default to a fraction of 1.0 (no-op, current
behaviour) so every pre-existing caller that never sets
protected_machine_type is completely unaffected -- confirmed explicitly
below.
"""
import math

import pandas as pd
import pytest

import ga.schedule_utils as schedule_utils
from ga.chromosome import ChromosomeFactory
from ga.operators import repair_line_and_machine_count


@pytest.fixture(autouse=True)
def restore_fraction():
    """MC_PROTECTION_FRACTION is a shared, monkeypatchable module global
    (same convention as RISK_BUFFER_AT_CERTAINTY) -- restore it after every
    test so one test's patch can never leak into the next."""
    original = schedule_utils.MC_PROTECTION_FRACTION
    yield
    schedule_utils.MC_PROTECTION_FRACTION = original


def _factory(tsj_machines=10, other_machines=4):
    """One active line, `tsj_machines` Tubular Sleeve Join machines and
    `other_machines` Body Assembly machines on it, one operation of each
    type."""
    df_lines = pd.DataFrame([
        {'line_id': 1, 'is_active': 1},
    ])
    machine_rows = (
        [{'asset_id': i, 'line_id': 1, 'operation_type': 'tubular sleeve join'}
         for i in range(tsj_machines)]
        + [{'asset_id': 100 + i, 'line_id': 1, 'operation_type': 'body assembly'}
           for i in range(other_machines)]
    )
    df_machines = pd.DataFrame(machine_rows)
    df_routings = pd.DataFrame([
        # required_machine_type is the raw MACHINE CLASS vocabulary (see
        # utils/machine_taxonomy.py), which maps to the canonical
        # operation_type used everywhere else -- not the operation_type
        # name itself.
        {'production_order_id': 1, 'operation_id': 1, 'operation_sequence_number': 1,
         'required_machine_type': 'feed-off-the-arm special'},
        {'production_order_id': 1, 'operation_id': 2, 'operation_sequence_number': 2,
         'required_machine_type': 'single-needle lockstitch'},
    ])
    return ChromosomeFactory(df_routings, df_machines, df_lines, legacy_mode='legacy_company_wide')


# ---------------------------------------------------------------------------
# ChromosomeFactory._max_machines_for
# ---------------------------------------------------------------------------

def test_default_fraction_is_unchanged_behaviour():
    factory = _factory()
    factory.protected_machine_type = 'tubular sleeve join'
    # MC_PROTECTION_FRACTION untouched (1.0 from the fixture) -- setting
    # protected_machine_type alone must not change anything.
    tsj_bound = factory._max_machines_for('1_1', 1)
    assert schedule_utils.MC_PROTECTION_FRACTION == 1.0
    unprotected_run = ChromosomeFactory(factory.df_routings, factory.df_machines, factory.df_lines, legacy_mode='legacy_company_wide')
    assert tsj_bound == unprotected_run._max_machines_for('1_1', 1)


def test_fraction_scales_down_only_the_protected_type():
    factory = _factory()
    baseline_tsj = factory._max_machines_for('1_1', 1)
    baseline_other = factory._max_machines_for('1_2', 1)

    schedule_utils.MC_PROTECTION_FRACTION = 0.5
    factory.protected_machine_type = 'tubular sleeve join'

    protected_bound = factory._max_machines_for('1_1', 1)
    other_bound = factory._max_machines_for('1_2', 1)

    assert protected_bound == max(1, math.ceil(baseline_tsj * 0.5))
    assert protected_bound < baseline_tsj
    # Body Assembly isn't the protected type -- must be completely untouched.
    assert other_bound == baseline_other


def test_fraction_never_caps_below_one_machine():
    factory = _factory(tsj_machines=1, other_machines=1)
    schedule_utils.MC_PROTECTION_FRACTION = 0.1
    factory.protected_machine_type = 'tubular sleeve join'
    assert factory._max_machines_for('1_1', 1) >= 1


def test_protected_machine_type_none_is_a_noop_even_with_fraction_set():
    factory = _factory()
    schedule_utils.MC_PROTECTION_FRACTION = 0.5
    factory.protected_machine_type = None  # default -- nothing to protect yet
    baseline = ChromosomeFactory(factory.df_routings, factory.df_machines, factory.df_lines, legacy_mode='legacy_company_wide')
    assert factory._max_machines_for('1_1', 1) == baseline._max_machines_for('1_1', 1)


# ---------------------------------------------------------------------------
# repair_line_and_machine_count
# ---------------------------------------------------------------------------

def _repair_args():
    os_array = ['op_a', 'op_b']
    la_array = [1, 1]
    mc_array = [40, 40]  # deliberately above any real cap, so clamping is observable
    eligibility_map = {'op_a': [1], 'op_b': [1]}
    machines_per_line_map = {'op_a': {1: 10}, 'op_b': {1: 10}}
    op_optype = {'op_a': 'tubular sleeve join', 'op_b': 'body assembly'}
    return os_array, la_array, mc_array, eligibility_map, machines_per_line_map, op_optype


def test_repair_leaves_mc_at_full_cap_by_default():
    os_array, la_array, mc_array, elig, per_line, optype = _repair_args()
    _, _, mc_out = repair_line_and_machine_count(
        os_array, la_array, mc_array, elig, per_line, max_mc_cap=40,
        op_optype=optype, protected_machine_type='tubular sleeve join')
    assert mc_out == [10, 10]  # fraction is 1.0 -- untouched


def test_repair_scales_down_only_the_protected_type():
    schedule_utils.MC_PROTECTION_FRACTION = 0.5
    os_array, la_array, mc_array, elig, per_line, optype = _repair_args()
    _, _, mc_out = repair_line_and_machine_count(
        os_array, la_array, mc_array, elig, per_line, max_mc_cap=40,
        op_optype=optype, protected_machine_type='tubular sleeve join')
    assert mc_out[0] == 5   # op_a: tubular sleeve join, capped to half of 10
    assert mc_out[1] == 10  # op_b: body assembly, untouched


def test_repair_ignores_the_fraction_when_protected_machine_type_is_none():
    schedule_utils.MC_PROTECTION_FRACTION = 0.5
    os_array, la_array, mc_array, elig, per_line, optype = _repair_args()
    _, _, mc_out = repair_line_and_machine_count(
        os_array, la_array, mc_array, elig, per_line, max_mc_cap=40,
        op_optype=optype, protected_machine_type=None)
    assert mc_out == [10, 10]


def test_repair_ignores_the_fraction_when_op_optype_is_missing():
    schedule_utils.MC_PROTECTION_FRACTION = 0.5
    os_array, la_array, mc_array, elig, per_line, _ = _repair_args()
    _, _, mc_out = repair_line_and_machine_count(
        os_array, la_array, mc_array, elig, per_line, max_mc_cap=40,
        op_optype=None, protected_machine_type='tubular sleeve join')
    assert mc_out == [10, 10]
