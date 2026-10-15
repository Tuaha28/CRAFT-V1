"""
Tests for data_generation/routing_tables.py -- the literature-derived
per-garment-type routing tables replacing the invented uniform 5-step
template (see routing_spec.md and PROJECT_STATUS.txt Section 1.2.1).

The decisive property under test throughout: Feed-off-the-Arm Special
(Tubular Sleeve Join) must never be a routing's terminal (last) operation,
and every group's actual terminal must match routing_spec.md Section 4's
distribution (Flatlock Coverstitch for knit tops, Automated Pattern Tacker
for everything else). A regression here would silently reintroduce the
exact defect this whole exercise corrected.
"""
import pytest

from data_generation.routing_tables import (
    KNIT_TOP, TANK_TOP, POLO, TROUSER, WOVEN, WOVEN_SHORT_SLEEVE, ZIPPED_OUTERWEAR,
    GARMENT_TYPE_TO_ROUTING, SNLS, OVERLOCK, FLATLOCK, TACKER, FOA,
    routing_for, routing_group_name, sam_per_step, SHORT_SLEEVE_SHARE,
)

ALL_TABLES = {
    'KNIT_TOP': KNIT_TOP, 'TANK_TOP': TANK_TOP, 'POLO': POLO,
    'TROUSER': TROUSER, 'WOVEN': WOVEN, 'ZIPPED_OUTERWEAR': ZIPPED_OUTERWEAR,
    'WOVEN_SHORT_SLEEVE': WOVEN_SHORT_SLEEVE,
}
CANONICAL_MACHINE_TYPES = {SNLS, OVERLOCK, FLATLOCK, TACKER, FOA}


# ---------------------------------------------------------------------------
# The decisive finding: Feed-off-the-Arm is never terminal, never first.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,table", ALL_TABLES.items())
def test_feed_off_the_arm_is_never_the_terminal_operation(name, table):
    last_op, last_machine = table[-1]
    assert last_machine != FOA, (
        f"{name}: Feed-off-the-Arm is the LAST step -- this is exactly the "
        f"defect (a mid-sequence machine placed at the end of every routing) "
        f"routing_spec.md corrected. Terminal is '{last_op}'."
    )


@pytest.mark.parametrize("name,table", ALL_TABLES.items())
def test_feed_off_the_arm_appears_mid_sequence_not_first_either(name, table):
    machines = [m for _, m in table]
    assert FOA in machines, f"{name}: Feed-off-the-Arm should still appear somewhere (it's a real operation, just not terminal)"
    idx = machines.index(FOA)
    assert 0 < idx < len(table) - 1, (
        f"{name}: Feed-off-the-Arm at position {idx+1} of {len(table)} -- "
        f"should be strictly mid-sequence, not first or last."
    )


@pytest.mark.parametrize("name,table,expected_terminal", [
    ('KNIT_TOP', KNIT_TOP, FLATLOCK),
    ('TANK_TOP', TANK_TOP, FLATLOCK),
    ('POLO', POLO, FLATLOCK),
    ('TROUSER', TROUSER, TACKER),
    ('WOVEN', WOVEN, TACKER),
    ('WOVEN_SHORT_SLEEVE', WOVEN_SHORT_SLEEVE, TACKER),
    ('ZIPPED_OUTERWEAR', ZIPPED_OUTERWEAR, TACKER),
])
def test_terminal_operation_matches_routing_spec_section_4(name, table, expected_terminal):
    _, actual_terminal = table[-1]
    assert actual_terminal == expected_terminal, (
        f"{name}: terminal is {actual_terminal}, routing_spec.md Section 4 "
        f"says it should be {expected_terminal}"
    )


# ---------------------------------------------------------------------------
# Structural validity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,table", ALL_TABLES.items())
def test_every_step_uses_a_canonical_machine_type(name, table):
    for op_name, machine_type in table:
        assert machine_type in CANONICAL_MACHINE_TYPES, f"{name}: unknown machine type '{machine_type}' for op '{op_name}'"


@pytest.mark.parametrize("name,table", ALL_TABLES.items())
def test_routing_length_is_at_least_five_and_varies_by_group(name, table):
    # routing_spec.md: "Operation counts now vary (7-10 steps)" -- Tank Top
    # is the deliberate short exception at 6 (two sleeve steps genuinely
    # don't apply), so the floor checked here is 5, not 7.
    assert len(table) >= 5, f"{name}: only {len(table)} steps"


def test_routing_lengths_are_not_all_identical():
    # The whole point: routing length should vary by garment type now,
    # not be a fixed constant the way the old 5-step template was.
    lengths = {len(t) for t in ALL_TABLES.values()}
    assert len(lengths) > 1, f"all routings are the same length: {lengths}"


def test_no_step_names_are_empty():
    for name, table in ALL_TABLES.items():
        for op_name, _ in table:
            assert op_name and op_name.strip(), f"{name}: an operation has an empty name"


# ---------------------------------------------------------------------------
# GarmentType -> routing coverage (must cover every real grid.xls category)
# ---------------------------------------------------------------------------

REAL_GRID_GARMENT_TYPES = [
    # From grid.xls's real distribution (PROJECT_STATUS.txt Section 1.2.1 /
    # routing_spec.md Section 6.3), excluding NaN (already dropped upstream
    # by the seeder before any category is ever selected).
    'Pant', 'U Neck', 'Blouse', 'Full Zip Mock Neck', 'Short', 'Dress',
    'Tank Top', '1/2 Zip Mock Neck', 'Hoodie', 'V Neck', 'Full Zip Hoodie',
    'Shirt', 'Skirt', 'Polo', '1/2 Zipper Hoodie', 'Mock Neck',
    'Jump Suit', 'Tube', 'Helmet',
]


@pytest.mark.parametrize("garment_type", REAL_GRID_GARMENT_TYPES)
def test_every_real_garment_type_has_a_routing(garment_type):
    table = routing_for(garment_type)
    assert table in ALL_TABLES.values(), f"{garment_type} did not resolve to one of the known tables"
    assert len(table) >= 5


def test_unknown_garment_type_falls_back_to_woven_not_a_crash():
    assert routing_for('Something Not In The Real Data') == WOVEN


# ---------------------------------------------------------------------------
# Blouse/Dress short-sleeve variant (routing_spec.md 3.3) -- the gap the
# original implementation left undone: every Blouse/Dress product silently
# defaulted to the 9-step long-sleeve base regardless of the `short_sleeve`
# flag's existence.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("garment_type", ['Blouse', 'Dress'])
def test_short_sleeve_true_selects_the_8_step_variant(garment_type):
    table = routing_for(garment_type, short_sleeve=True)
    assert table == WOVEN_SHORT_SLEEVE
    assert len(table) == 8


@pytest.mark.parametrize("garment_type", ['Blouse', 'Dress'])
def test_short_sleeve_false_or_default_selects_the_9_step_base(garment_type):
    assert routing_for(garment_type, short_sleeve=False) == WOVEN
    assert routing_for(garment_type) == WOVEN


def test_short_sleeve_variant_omits_only_the_cuff_step():
    # WOVEN_SHORT_SLEEVE should be WOVEN with exactly "Cuff attach" removed
    # and every other step, order, and machine type unchanged.
    long_ops = [op for op, _ in WOVEN]
    short_ops = [op for op, _ in WOVEN_SHORT_SLEEVE]
    assert long_ops == short_ops[:6] + ['Cuff attach'] + short_ops[6:]


def test_short_sleeve_flag_is_ignored_outside_blouse_dress():
    # The flag only means something for the woven group's two garment
    # types with a documented variant -- passing it for anything else
    # must not silently change behaviour.
    assert routing_for('Pant', short_sleeve=True) == routing_for('Pant', short_sleeve=False)
    assert routing_for('U Neck', short_sleeve=True) == routing_for('U Neck', short_sleeve=False)


def test_short_sleeve_share_is_a_valid_probability():
    assert 0.0 <= SHORT_SLEEVE_SHARE <= 1.0


def test_routing_group_name_matches_the_terminal_family():
    assert routing_group_name('U Neck') == 'knit_top'
    assert routing_group_name('Pant') == 'trouser'
    assert routing_group_name('Shirt') == 'woven'
    assert routing_group_name('Hoodie') == 'zipped_outerwear'
    # documented fallbacks
    assert routing_group_name('Jump Suit') == 'trouser'
    assert routing_group_name('Tube') == 'knit_top'
    assert routing_group_name('Helmet') == 'woven'


# ---------------------------------------------------------------------------
# SAM distribution
# ---------------------------------------------------------------------------

def test_sam_per_step_divides_evenly():
    assert sam_per_step(21.0, 7) == pytest.approx(3.0)


def test_sam_per_step_sums_back_to_the_total():
    total = 26.74
    n = len(TROUSER)
    per_step = sam_per_step(total, n)
    assert per_step * n == pytest.approx(total)


def test_sam_per_step_zero_steps_returns_zero_not_a_crash():
    assert sam_per_step(20.0, 0) == 0.0


def test_sam_per_step_reflects_operation_count_proportionally():
    # A machine type used in more steps of a routing should receive a
    # proportionally larger share of the total SMV -- exactly what equal
    # per-step division produces without any extra per-operation weighting.
    total = 28.0
    per_step = sam_per_step(total, len(ZIPPED_OUTERWEAR))
    overlock_steps = sum(1 for _, m in ZIPPED_OUTERWEAR if m == OVERLOCK)
    overlock_share = per_step * overlock_steps
    assert overlock_share / total == pytest.approx(overlock_steps / len(ZIPPED_OUTERWEAR))
