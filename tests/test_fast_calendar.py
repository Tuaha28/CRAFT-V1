"""fast_calendar must reproduce utils.work_calendar.productive_segments exactly."""
from datetime import datetime, timedelta
import random

import pytest

from crafter.repair.fast_calendar import CrewCalendar, _abs_seconds
from ga.worker_allocation import operator_available_at
from utils.work_calendar import NoAvailableCalendar, productive_segments

ORIGIN = datetime(2026, 9, 10)


def original(index, workers, reservations, ready, minutes):
    def available(when):
        return all(operator_available_at(index, int(w), when) and
                   all(not (a <= when < b) for a, b in reservations.get(w, ()))
                   for w in workers)
    edges = sorted({e for w in workers for iv in reservations.get(w, ()) for e in iv})

    def next_change(when):
        later = [e for e in edges if e > when]
        return later[0] if later else None
    return productive_segments(ready, minutes, available, next_availability_change=next_change)


def as_seconds(result):
    start, end, segments = result
    return _abs_seconds(start), _abs_seconds(end), [(_abs_seconds(a), _abs_seconds(b)) for a, b in segments]


@pytest.mark.parametrize("seed", range(40))
def test_matches_original_walk_on_random_crews_and_reservations(seed):
    rng = random.Random(seed)
    workers = [str(100 + i) for i in range(rng.choice((1, 1, 2, 3)))]
    index = {"crew_of": {int(w): (rng.randrange(3), rng.randrange(7)) for w in workers}, "shift_of": {}}
    reservations = {}
    for w in workers:
        rows, t = [], ORIGIN + timedelta(seconds=rng.randrange(0, 5 * 86400))
        for _ in range(rng.randrange(0, 12)):
            t += timedelta(seconds=rng.randrange(0, 3 * 86400))
            end = t + timedelta(seconds=rng.randrange(1, 4 * 86400))
            rows.append((t, end))
            if rng.random() < 0.7:
                t = end
        reservations[w] = rows
    calendar = CrewCalendar(index, workers, reservations)
    assert calendar.supported
    for _ in range(60):
        ready = ORIGIN + timedelta(seconds=rng.randrange(0, 40 * 86400))
        minutes = rng.choice((0.5, 7.3, 60, 479.99, 480, 1000.25, 4167.67, rng.uniform(1, 20000)))
        try:
            expected = as_seconds(original(index, workers, reservations, ready, minutes))
        except NoAvailableCalendar:
            with pytest.raises(NoAvailableCalendar):
                calendar.execute(_abs_seconds(ready), minutes)
            continue
        start, end, segments = calendar.execute(_abs_seconds(ready), minutes)
        assert (start, end, segments) == expected


def test_floater_without_batch_crew_falls_back():
    calendar = CrewCalendar({"crew_of": {}, "shift_of": {7: (6, 14)}}, ["7"], {})
    assert not calendar.supported


def test_long_reservation_raises_like_original():
    index = {"crew_of": {5: (0, 0)}, "shift_of": {}}
    reservations = {"5": [(ORIGIN, ORIGIN + timedelta(days=60))]}
    with pytest.raises(NoAvailableCalendar):
        original(index, ["5"], reservations, ORIGIN, 60)
    with pytest.raises(NoAvailableCalendar):
        CrewCalendar(index, ["5"], reservations).execute(_abs_seconds(ORIGIN), 60)
