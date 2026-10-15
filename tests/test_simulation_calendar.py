from datetime import datetime, timedelta, time
import pytest
from data_generation.simulation_calendar import split_quantity, work_on_calendar


@pytest.mark.parametrize("quantity", [1, 19, 20, 21, 52, 331, 3300])
def test_lots_conserve_every_unit(quantity):
    lots = split_quantity(quantity)
    assert sum(lots) == quantity
    assert min(lots) > 0
    assert len(lots) <= 60
    assert max(lots) - min(lots) <= 1


def test_long_operation_pauses_and_resumes_without_truncation():
    band = ('Morning', time(6), time(14))
    start, end, segments = work_on_calendar(datetime(2026, 9, 10, 13), 600, band=band)
    assert start == datetime(2026, 9, 10, 13)
    assert end == datetime(2026, 9, 12, 7)
    assert sum((b-a).total_seconds() for a,b in segments) == 600*60
    assert all(6 <= a.hour < 14 and b.hour <= 14 for a,b in segments)


def test_actual_overrun_cascades_to_next_reservation():
    band = ('Morning', time(6), time(14))
    ready = datetime(2026, 9, 10, 6)
    _, planned_end, _ = work_on_calendar(ready, 60, band=band)
    _, actual_end, _ = work_on_calendar(ready, 120, band=band)
    next_start, _, _ = work_on_calendar(max(planned_end, actual_end), 60, band=band)
    assert next_start == actual_end


def test_crew_segments_never_cross_off_duty_time():
    from utils.shift_rotation import batch_available_at
    _, _, segments = work_on_calendar(datetime(2026, 9, 6, 21), 1200, crew=(2, 0))
    for start, end in segments:
        assert batch_available_at(2, 0, start)
        assert batch_available_at(2, 0, end-timedelta(seconds=1))


@pytest.mark.parametrize("duration", [0, -1, float('nan'), float('inf')])
def test_invalid_duration_is_rejected(duration):
    with pytest.raises(ValueError):
        work_on_calendar(datetime(2026, 9, 10), duration, band=('Morning',time(6),time(14)))
