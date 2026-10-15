"""
Tests for the fuzzy disruption-triage membership functions.

The point of the fuzzy layer is that it has no cliff: an order at 5.1 days of
slack must not be treated as categorically different from one at 4.9. These
tests assert that property directly, along with the shapes the rule base
depends on.
"""

import pytest

from agent.fuzzy_logic import (
    SLACK_URGENT_FULL, SLACK_RELAXED_FULL,
    Tier,
    decide_tier,
    impact_membership,
    slack_membership,
    urgency_score,
    _trapezoid,
)


# ---------------------------------------------------------------------------
# Trapezoid primitive
# ---------------------------------------------------------------------------

def test_trapezoid_plateau_and_shoulders():
    # 0 below a, ramp a->b, flat 1 between b and c, ramp down c->d, 0 above d.
    assert _trapezoid(-1, 0, 2, 4, 6) == 0.0
    assert _trapezoid(1, 0, 2, 4, 6) == pytest.approx(0.5)
    assert _trapezoid(3, 0, 2, 4, 6) == 1.0
    assert _trapezoid(5, 0, 2, 4, 6) == pytest.approx(0.5)
    assert _trapezoid(7, 0, 2, 4, 6) == 0.0


def test_trapezoid_is_monotonic_on_each_shoulder():
    rising = [_trapezoid(x / 10, 0, 2, 4, 6) for x in range(0, 21)]
    assert rising == sorted(rising)
    falling = [_trapezoid(4 + x / 10, 0, 2, 4, 6) for x in range(0, 21)]
    assert falling == sorted(falling, reverse=True)


def test_trapezoid_never_leaves_the_unit_interval():
    for x in range(-50, 150):
        assert 0.0 <= _trapezoid(x / 10, 0, 3, 3, 10) <= 1.0


# ---------------------------------------------------------------------------
# Membership functions
# ---------------------------------------------------------------------------

def test_slack_memberships_are_bounded():
    for days in range(-5, 40):
        m = slack_membership(days)
        assert set(m) == {'urgent', 'moderate', 'relaxed'}
        for v in m.values():
            assert 0.0 <= v <= 1.0


def test_deeply_overdue_is_fully_urgent():
    # Anchors are now real quantiles of the measured slack distribution,
    # whose 25th percentile is NEGATIVE -- so "fully urgent" starts below
    # zero, not at it.
    m = slack_membership(SLACK_URGENT_FULL - 10)
    assert m['urgent'] == 1.0
    assert m['relaxed'] == 0.0


def test_overdue_is_more_urgent_than_due_today():
    # The old code clamped negative slack to 0, making every overdue order
    # look identical to one due today -- collapsing exactly the orders
    # triage exists to catch. Being further overdue must now rank higher.
    assert slack_membership(-30)['urgent'] >= slack_membership(0)['urgent']
    assert slack_membership(0)['urgent'] >= slack_membership(30)['urgent']


def test_very_long_slack_is_fully_relaxed():
    m = slack_membership(SLACK_RELAXED_FULL + 10)
    assert m['relaxed'] == 1.0
    assert m['urgent'] == 0.0


def test_impact_memberships_are_bounded():
    for hours in range(0, 100):
        m = impact_membership(hours)
        assert set(m) == {'negligible', 'moderate', 'severe'}
        for v in m.values():
            assert 0.0 <= v <= 1.0


def test_zero_impact_is_fully_negligible():
    assert impact_membership(0)['negligible'] == 1.0


def test_huge_impact_is_fully_severe():
    assert impact_membership(200)['severe'] == 1.0


# ---------------------------------------------------------------------------
# The no-cliff property -- the reason this layer is fuzzy at all
# ---------------------------------------------------------------------------

def test_urgency_has_no_discontinuity_across_the_old_crisp_threshold():
    # A crisp "under 5 days = urgent" rule made 4.9 and 5.1 categorically
    # different. They must now be almost identical.
    assert urgency_score(4.9) == pytest.approx(urgency_score(5.1), abs=0.05)


def test_urgency_is_continuous_everywhere():
    prev = urgency_score(0.0)
    step = 0.1
    x = step
    while x <= 30.0:
        cur = urgency_score(x)
        assert abs(cur - prev) < 0.05, f"jump at {x:.1f} days: {prev:.3f} -> {cur:.3f}"
        prev = cur
        x += step


def test_urgency_decreases_as_slack_grows():
    # Regression test for a real inversion: urgency_score used to be
    # urgent + 0.5 * moderate, and because Moderate ramps up to a peak at 12
    # days the sum dipped to 0.375 at 10 days and rose to 0.500 at 12 -- more
    # slack ranked more urgent. impact_assessor preempts only a strictly less
    # urgent donor, so in that band it would take a slot from an order with
    # less slack to protect one with more.
    scores = [urgency_score(d) for d in range(0, 30)]
    assert scores == sorted(scores, reverse=True)


def test_urgency_has_no_local_bump_at_fine_resolution():
    # The old defect sat between whole days, so check a tenth-day grid too.
    prev = urgency_score(0.0)
    x = 0.1
    while x <= 30.0:
        cur = urgency_score(x)
        assert cur <= prev + 1e-12, f"urgency rose at {x:.1f} days: {prev:.4f} -> {cur:.4f}"
        prev = cur
        x += 0.1


def test_urgency_stays_in_the_unit_interval():
    for d in range(-10, 60):
        assert 0.0 <= urgency_score(d) <= 1.0


def test_manager_override_pins_urgency_to_maximum():
    # A directive sits above the fuzzy layer entirely rather than being
    # inferred from slack.
    assert urgency_score(45, manager_forced=True) == 1.0
    assert urgency_score(0, manager_forced=True) == 1.0


# ---------------------------------------------------------------------------
# Tier decisions
# ---------------------------------------------------------------------------

def test_negligible_impact_is_a_no_op():
    assert decide_tier(slack_days=20, impact_hours=0)['tier'] == Tier.NO_ACTION


def test_severe_impact_escalates_however_relaxed_the_order():
    assert decide_tier(slack_days=60, impact_hours=200)['tier'] == Tier.FULL_RESCHEDULE


def test_moderate_impact_on_an_urgent_order_escalates():
    assert decide_tier(slack_days=1, impact_hours=16)['tier'] == Tier.FULL_RESCHEDULE


def test_moderate_impact_on_a_relaxed_order_is_patchable():
    assert decide_tier(slack_days=25, impact_hours=16)['tier'] == Tier.LOCAL_PATCH


def test_manager_forced_always_escalates():
    d = decide_tier(slack_days=60, impact_hours=0, manager_forced=True)
    assert d['tier'] == Tier.FULL_RESCHEDULE


def test_every_decision_reports_a_valid_tier_and_a_reason():
    for slack in (0, 5, 12, 30):
        for impact in (0, 2, 18, 40, 200):
            d = decide_tier(slack, impact)
            assert d['tier'] in set(Tier)
            assert d['reason']


def test_tier_is_never_less_severe_as_impact_grows():
    # Holding slack fixed, more damage must never downgrade the response.
    rank = {Tier.NO_ACTION: 0, Tier.LOCAL_PATCH: 1, Tier.FULL_RESCHEDULE: 2}
    for slack in (2, 12, 25):
        tiers = [rank[decide_tier(slack, h)['tier']] for h in range(0, 60, 2)]
        assert tiers == sorted(tiers), f"response downgraded as impact rose at slack={slack}"
