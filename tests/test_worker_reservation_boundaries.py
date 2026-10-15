from datetime import datetime

from ga.worker_allocation import allocate_crew


WORKER = 7648


def _index():
    return {
        'by_line': {(1, 'body assembly'): [WORKER]},
        'by_factory': {(1, 'body assembly'): [WORKER]},
        'by_skill': {'body assembly': [WORKER]},
        'line_factory': {1: 1},
        'shift_of': {WORKER: (6.0, 14.0)},
        'crew_of': {},
        'efficiency_of': {(WORKER, 'body assembly'): 84.0},
    }


def _overlap(left, right):
    return left[0] < right[1] and right[0] < left[1]


def test_subhour_reservation_boundary_blocks_later_backdated_allocation():
    day = datetime(2026, 9, 19)
    first_start = day.replace(hour=9, minute=15, second=53)
    first_end = day.replace(hour=9, minute=50, second=31)
    state = {str(WORKER): [(first_start, first_end)]}
    result = allocate_crew(
        _index(), 'body assembly', 1, 1, day.replace(hour=9), 120,
        state, state_is_normalized=True,
    )
    assert result['employees'] == [WORKER]
    assert result['segments'][0] == (day.replace(hour=9), first_start)
    assert result['segments'][1][0] == first_end
    for segment in result['segments']:
        assert not _overlap(segment, (first_start, first_end))


def test_reservation_state_prevents_worker_double_booking_when_os_backtracks():
    day = datetime(2026, 9, 19)
    state = {}
    first = allocate_crew(
        _index(), 'body assembly', 1, 1,
        day.replace(hour=9, minute=15, second=53), 34 + 38 / 60,
        state, state_is_normalized=True,
    )
    second = allocate_crew(
        _index(), 'body assembly', 1, 1, day.replace(hour=9), 120,
        state, first['busy_minutes'], state_is_normalized=True,
    )
    first_segments = first['segments']
    second_segments = second['segments']
    assert all(not _overlap(a, b) for a in first_segments for b in second_segments)
    assert all(isinstance(key, str) for key in second['reservations'])
