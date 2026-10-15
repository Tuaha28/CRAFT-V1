"""
Lightweight fuzzy inference for scheduling disruption triage.

WHY FUZZY: a crisp threshold ("under 5 days = urgent") creates a cliff —
an order at 5.1 days of slack is treated as completely safe, one at 4.9
days is treated as an emergency, even though they're practically the same
order. Fuzzy membership smooths that out: "urgency" becomes a degree
(0.0-1.0), not a coin flip.

No external fuzzy library (e.g. scikit-fuzzy) is used on purpose — this is
implemented directly as trapezoidal/triangular membership functions plus a
small Mamdani-style rule base, so it's easy to read/defend in a viva and
doesn't add a new dependency to requirements.txt.

This module is pure-Python / no DB, no torch — safe to unit test in
isolation (see the __main__ block at the bottom).

This extends the same fuzzy-logic approach your FYP design already calls
for (handling uncertainty in worker efficiency / machine health / SMV
variance) into the rescheduling-triage decision, rather than introducing a
second, inconsistent way of handling uncertainty.
"""

import os
import sys
from enum import Enum

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.derived_constants import get_derived


class Tier(str, Enum):
    NO_ACTION = "NO_ACTION"
    LOCAL_PATCH = "LOCAL_PATCH"
    FULL_RESCHEDULE = "FULL_RESCHEDULE"


# ---------------------------------------------------------------------------
# Membership function anchors. These are business-policy choices, not
# mathematical constants — tune them against real factory data once you have
# it. Documented here instead of buried as magic numbers.
# ---------------------------------------------------------------------------

# Slack (days between now and an order's committed deadline)
#
# DERIVED FROM THE REAL SLACK DISTRIBUTION, not chosen (audit #1).
#
# The previous anchors (3 / 10 / 12+-8 / 14 / 20) were fixed day-counts with
# no relationship to this order book's actual spread. Measured against the
# live database (1,029 active orders, 2026-09-10): slack-to-deadline runs
# p0=-53d, p25=-12d, p50=+27d, p75=+88d, p95=+223d, p100=+372d, with 322
# orders already overdue. Under the old anchors 54.4% of orders pinned at
# fully-Relaxed and 34.0% at fully-Urgent, leaving only 11.6% anywhere in
# the graded region -- so slack contributed almost no gradient to triage and
# the Moderate band was nearly unreachable.
#
# Anchored on real quantiles instead, so each membership function covers a
# meaningful share of the population: Urgent ramps down across the lower
# half, Relaxed ramps up across the upper half, Moderate peaks at the median
# and spans the interquartile range.
#
# PROVENANCE AND STALENESS. These are loaded from what the SEEDER derived
# for the database actually in use (outputs/derived_constants.json, written
# by data_generation/seed_database_fixed.py) and only fall back to the
# literals below -- loudly -- when that file is absent. This is deliberate:
# the same class of constant elsewhere in this codebase
# (REAL_EFFICIENCY_FALLBACK_NORM) still carries statistics from a database
# that no longer exists, and a reseed moves this distribution. Falling back
# to the literals means running against measurements taken on 2026-09-10
# from a 1,029-order book -- correct then, not necessarily correct now.
# Legacy policy defaults; runtime calls load current dataset quantiles.
SLACK_URGENT_FULL = -12.
SLACK_URGENT_ZERO = 27.
SLACK_MODERATE_PEAK = 27.
SLACK_MODERATE_SPREAD = 50.
SLACK_RELAXED_ZERO = 27.
SLACK_RELAXED_FULL = 88.


def _slack_quantiles():
    import math
    q = get_derived('slack_days_quantiles', {'p25': -12., 'p50': 27., 'p75': 88.})
    values = tuple(float(q[k]) for k in ('p25', 'p50', 'p75'))
    if not all(math.isfinite(v) for v in values) or not values[0] < values[1] < values[2]:
        raise ValueError('Slack calibration requires finite, strictly ordered quantiles')
    return values


# Disruption impact (estimated ADDED delay, in hours, to an order's
# completion caused by this specific event)
IMPACT_NEGLIGIBLE_FULL = 0    # at or below: fully Negligible
IMPACT_NEGLIGIBLE_ZERO = 4    # at or above: zero Negligible membership
IMPACT_MODERATE_PEAK = 16     # hours at which Moderate membership peaks
IMPACT_MODERATE_SPREAD = 20   # +/- spread around the peak for Moderate
IMPACT_SEVERE_ZERO = 24       # at or below: zero Severe membership
IMPACT_SEVERE_FULL = 48       # at or above: fully Severe (~2 days, factory-shutdown scale)


def _trapezoid(x: float, a: float, b: float, c: float, d: float) -> float:
    """
    Standard trapezoid membership: 0 below a, ramps up to 1 between a-b,
    flat 1 between b-c, ramps down to 0 between c-d, 0 above d.
    Pass a == b or c == d to get a triangular/simple-ramp shape.
    """
    if x <= a or x >= d:
        return 0.0
    if b <= x <= c:
        return 1.0
    if a < x < b:
        return (x - a) / (b - a) if b > a else 1.0
    if c < x < d:
        return (d - x) / (d - c) if d > c else 1.0
    return 0.0


def slack_membership(slack_days: float) -> dict:
    """Returns {'urgent': .., 'moderate': .., 'relaxed': ..} each in [0,1]."""
    # NEGATIVE SLACK IS NO LONGER CLAMPED TO ZERO.
    #
    # This used to do `slack_days = max(0.0, slack_days)`, which made every
    # overdue order look identical to one due exactly today. That was
    # tolerable while the anchors were positive day-counts; it is not now
    # that they are real quantiles of a distribution whose 25th percentile
    # is NEGATIVE (-12 days on the measured book, with 322 of 1,029 orders
    # already overdue). Clamping would have collapsed the entire bottom
    # quartile -- precisely the orders triage exists to catch -- onto one
    # value. The trapezoid handles negatives correctly on its own: further
    # below SLACK_URGENT_FULL simply saturates at fully-Urgent.
    # Left-shoulder shape: fully Urgent at or below SLACK_URGENT_FULL, ramping
    # down to zero at SLACK_URGENT_ZERO.
    #
    # BUG FIX: the first two anchors were (-1e9, SLACK_URGENT_FULL), which made
    # the plateau start only AT SLACK_URGENT_FULL and put everything below it on
    # a ramp climbing from -1e9. The effect was small but wrong in the worst
    # place: an order with 0 days of slack scored 0.999999997 rather than 1.0,
    # and urgency INCREASED from 0 to 3 days before decreasing, so a
    # just-overdue order was ranked fractionally less urgent than one with
    # three days in hand. Setting both left anchors to -1e9 gives the flat
    # shoulder the docstring on SLACK_URGENT_FULL already describes.
    p25, p50, p75 = _slack_quantiles()
    urgent = _trapezoid(slack_days, -1e9, -1e9, p25, p50)
    moderate = _trapezoid(
        slack_days,
        p25, p50, p50, p75,
    )
    relaxed = _trapezoid(slack_days, p50, p75, 1e9, 1e9 + 1)
    return {"urgent": urgent, "moderate": moderate, "relaxed": relaxed}


def impact_membership(impact_hours: float) -> dict:
    """Returns {'negligible': .., 'moderate': .., 'severe': ..} each in [0,1]."""
    impact_hours = max(0.0, impact_hours)
    negligible = _trapezoid(impact_hours, -1e9, IMPACT_NEGLIGIBLE_FULL, IMPACT_NEGLIGIBLE_FULL, IMPACT_NEGLIGIBLE_ZERO)
    moderate = _trapezoid(
        impact_hours,
        IMPACT_MODERATE_PEAK - IMPACT_MODERATE_SPREAD,
        IMPACT_MODERATE_PEAK, IMPACT_MODERATE_PEAK,
        IMPACT_MODERATE_PEAK + IMPACT_MODERATE_SPREAD,
    )
    severe = _trapezoid(impact_hours, IMPACT_SEVERE_ZERO, IMPACT_SEVERE_FULL, 1e9, 1e9 + 1)
    return {"negligible": negligible, "moderate": moderate, "severe": severe}


def urgency_score(slack_days: float, manager_forced: bool = False) -> float:
    """
    Smooth 0..1 urgency score for RANKING orders against each other (who's
    the affected order that needs protecting, who's the donor with room to
    spare). This is deliberately separate from the tier decision below —
    tier answers "what do we do", urgency_score answers "who goes first".

    manager_forced=True (an explicit manager directive, e.g. "Order E is top
    priority") always returns 1.0 — a manual override sits above the fuzzy
    layer entirely rather than being inferred from slack.

    MONOTONIC BY CONSTRUCTION: more slack must never mean more urgent.

    This was `urgent + 0.5 * moderate`, a sum of two overlapping memberships.
    Because the Moderate membership ramps UP to its peak at 12 days, the sum
    dipped and then rose again: 0.375 at 10 days, 0.4375 at 11, 0.500 at 12.
    An order with more slack outranked one with less over roughly the 10-14
    day band.

    That was not cosmetic. agent/impact_assessor.py preempts a donor only when
    the donor's urgency is strictly LOWER than the affected order's, so inside
    that band the rule inverted: it would take a committed slot from an order
    with less slack to protect one with more — exactly what the rule exists to
    prevent.

    Replaced with a single descending ramp over the same anchors the rest of
    this module already uses: fully urgent at or below SLACK_URGENT_FULL,
    falling to zero at SLACK_RELAXED_FULL. It keeps the no-cliff property that
    motivated the fuzzy approach (4.9 and 5.1 days still score almost
    identically) while being monotonic by construction rather than by luck.

    The Moderate membership is untouched and still drives decide_tier() below;
    it is only its use in RANKING that was wrong.
    """
    if manager_forced:
        return 1.0
    p25, _, p75 = _slack_quantiles()
    return _trapezoid(
        slack_days, -1e9, -1e9, p25, p75
    )


def decide_tier(slack_days: float, impact_hours: float, manager_forced: bool = False) -> dict:
    """
    Mamdani-style rule evaluation -> a single categorical tier decision.

    Rule base:
      R1: IF impact is Negligible                       -> NO_ACTION
      R2: IF impact is Moderate AND slack is Relaxed     -> LOCAL_PATCH
      R3: IF impact is Moderate AND slack is Moderate    -> LOCAL_PATCH
      R4: IF impact is Moderate AND slack is Urgent      -> FULL_RESCHEDULE
      R5: IF impact is Severe                            -> FULL_RESCHEDULE

    Each rule's strength = min() of its antecedent memberships (standard
    fuzzy AND). Rules are grouped by consequent tier and the tier whose
    supporting rules have the highest combined (max) strength wins — a
    simple, defensible defuzzification for a categorical (not continuous)
    output.

    A manager directive on this specific order always forces FULL_RESCHEDULE
    treatment (i.e. don't let a merely-local patch quietly leave a
    manager-flagged order sitting on a workaround) — matches the "manager
    override sits above the fuzzy layer" design from earlier.
    """
    if manager_forced:
        return {
            "tier": Tier.FULL_RESCHEDULE,
            "reason": "Manager-forced priority order — bypasses fuzzy triage, always gets a full reschedule pass.",
            "rule_strengths": {},
        }

    s = slack_membership(slack_days)
    i = impact_membership(impact_hours)

    r1 = i["negligible"]                        # -> NO_ACTION
    r2 = min(i["moderate"], s["relaxed"])        # -> LOCAL_PATCH
    r3 = min(i["moderate"], s["moderate"])       # -> LOCAL_PATCH
    r4 = min(i["moderate"], s["urgent"])         # -> FULL_RESCHEDULE
    r5 = i["severe"]                             # -> FULL_RESCHEDULE

    strengths = {
        Tier.NO_ACTION: r1,
        Tier.LOCAL_PATCH: max(r2, r3),
        Tier.FULL_RESCHEDULE: max(r4, r5),
    }
    # TIE-BREAK: MOST CONSERVATIVE WINS (audit #6).
    #
    # This was plain max(strengths, key=strengths.get), which on an exact tie
    # returns the FIRST key in insertion order -- Tier.NO_ACTION. So an
    # genuinely ambiguous signal silently resolved to "do nothing", the least
    # safe reading available, with nothing in the output indicating a tie had
    # occurred. Reachable in practice: negligible and moderate impact
    # memberships cross at roughly 2.6 hours.
    #
    # Ranked explicitly instead, highest intervention first. On a tie the
    # handler escalates rather than stands down: an unnecessary local patch
    # costs a small reschedule, an unnecessary NO_ACTION costs a missed
    # disruption.
    _ESCALATION_ORDER = [Tier.FULL_RESCHEDULE, Tier.LOCAL_PATCH, Tier.NO_ACTION]
    tier = max(_ESCALATION_ORDER, key=lambda t: (strengths[t], -_ESCALATION_ORDER.index(t)))

    return {
        "tier": tier,
        "reason": (
            f"slack={slack_days:.1f}d (urgent={s['urgent']:.2f}, moderate={s['moderate']:.2f}, "
            f"relaxed={s['relaxed']:.2f}) | impact={impact_hours:.1f}h (negligible={i['negligible']:.2f}, "
            f"moderate={i['moderate']:.2f}, severe={i['severe']:.2f}) | "
            f"rule strengths: NO_ACTION={strengths[Tier.NO_ACTION]:.2f}, "
            f"LOCAL_PATCH={strengths[Tier.LOCAL_PATCH]:.2f}, FULL_RESCHEDULE={strengths[Tier.FULL_RESCHEDULE]:.2f}"
        ),
        "rule_strengths": {k.value: v for k, v in strengths.items()},
    }


if __name__ == "__main__":
    # Quick sanity checks against the scenarios discussed while designing this.
    scenarios = [
        ("Order A, urgent (5d slack), minor 2h delay", 5, 2, False),
        ("Order A, urgent (5d slack), severe 30h delay", 5, 30, False),
        ("Order D, relaxed (20d slack), minor 2h delay", 20, 2, False),
        ("Order D, relaxed (20d slack), moderate 18h delay", 20, 18, False),
        ("Order B/C, moderate (15d slack), moderate 18h delay", 15, 18, False),
        ("Order E, manager-forced priority, minor 1h delay", 10, 1, True),
        ("Borderline: 4.9d slack vs 5.1d slack, same 10h impact", 4.9, 10, False),
        ("Borderline: 5.1d slack", 5.1, 10, False),
    ]
    for label, slack, impact, forced in scenarios:
        result = decide_tier(slack, impact, manager_forced=forced)
        u = urgency_score(slack, manager_forced=forced)
        print(f"\n{label}")
        print(f"  -> tier={result['tier'].value}, urgency_score={u:.2f}")
        print(f"  -> {result['reason']}")
