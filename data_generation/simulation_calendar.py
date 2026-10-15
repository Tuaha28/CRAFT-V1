"""Explicit pause/resume calendar for synthetic work; no implicit handoffs."""
from datetime import datetime, time, timedelta
import math
from utils.shift_rotation import batch_available_at, band_covers
from utils.work_calendar import NoAvailableCalendar, productive_segments


def split_quantity(quantity, target=20, maximum_lots=60):
    if quantity <= 0 or target <= 0 or maximum_lots <= 0:
        raise ValueError("Quantity and lot settings must be positive")
    count = min(quantity, maximum_lots, max(1, round(quantity / target)))
    base, remainder = divmod(quantity, count)
    return [base + (i < remainder) for i in range(count)]


def work_on_calendar(ready, minutes, crew=None, band=None):
    """Return start/end and productive segments; reserve the span conservatively.

    Calendar follows the existing civil-date rotation/rest-day convention.
    Work pauses at every off-duty boundary; the same worker resumes it.

    The walk itself now lives in utils/work_calendar.py so that the SEEDER and
    agent/decision_engine.py's worker eligibility run ONE implementation
    instead of two. They previously disagreed -- the exporter required
    uninterrupted single-worker coverage of the whole span, a requirement this
    model has never satisfied for any operation longer than one 8h shift --
    and that disagreement accounted for 56.6% of the measured worker shortage.
    Only the availability predicate differs between the two callers; the
    segmentation must not.
    """
    if crew is None and band is None:
        raise ValueError("Missing worker calendar")
    is_available = ((lambda t: batch_available_at(*crew, t)) if crew is not None
                    else (lambda t: band_covers(band, t)))
    try:
        return productive_segments(ready, minutes, is_available)
    except NoAvailableCalendar as exc:
        # Re-raised as a plain ValueError with the original wording: the
        # seeder's callers treat this as a data fault, and the message is
        # asserted on in tests.
        raise ValueError("Worker has no available shift within 28 days") from exc
