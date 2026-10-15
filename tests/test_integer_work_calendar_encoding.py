"""Integer TEST contract work must not gain a second in the calendar codec."""
from datetime import datetime
import math

import pytest

from crafter.authoritative_semantics import calendar_minutes_from_integer_seconds
from utils.work_calendar import productive_segments


def test_observed_469_1744_duration_round_trip():
    assert math.ceil(534.2 * 60) == 32053
    minutes = calendar_minutes_from_integer_seconds(32052)
    assert math.ceil(minutes * 60) == 32052
    ready = datetime(2026, 9, 10, 6)
    _, end, segments = productive_segments(ready, minutes, lambda _: True)
    assert (end - ready).total_seconds() == 32052
    assert sum((b - a).total_seconds() for a, b in segments) == 32052


def test_exhaustive_small_integer_oracle_and_large_boundaries():
    for seconds in [*range(1, 100001), 2592000, 31536000, 2**52]:
        assert math.ceil(calendar_minutes_from_integer_seconds(seconds) * 60) == seconds


@pytest.mark.parametrize('seconds', [0, -1, True, 1.5, 2**52 + 1])
def test_invalid_integer_work_is_rejected(seconds):
    with pytest.raises(ValueError, match='INTEGER_WORK_SECONDS_OUT_OF_RANGE'):
        calendar_minutes_from_integer_seconds(seconds)


def test_native_minute_legacy_semantics_are_not_changed():
    ready = datetime(2026, 9, 10, 6)
    _, end, _ = productive_segments(ready, 534.2, lambda _: True)
    assert (end - ready).total_seconds() == 32053
