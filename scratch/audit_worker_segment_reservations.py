"""Read-only audit probe for exporter worker-segmentation reservations.

This exercises the production _assign_operators method twice. A ten-hour
job started at 06:00 requires 06:00-14:00 on Monday and 06:00-08:00 Tuesday
for a worker whose recurring availability is 06:00-14:00. The second job is
deliberately proposed inside that Tuesday resumed-work segment.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.decision_engine import AIDecisionEngine
from utils.work_calendar import productive_segments


def main():
    monday = datetime(2026, 3, 2, 6, 0)
    tuesday = datetime(2026, 3, 3, 6, 0)
    employee_id = 1
    index = {
        "by_line": {(10, "body assembly"): [employee_id]},
        "by_factory": {(1, "body assembly"): [employee_id]},
        "by_skill": {"body assembly": [employee_id]},
        "line_factory": {10: 1},
        "shift_of": {},
        "crew_of": {},
        "efficiency_of": {(employee_id, "body assembly"): 84.0},
    }
    reservations = {}
    busy = {}

    original = AIDecisionEngine._available_at
    try:
        AIDecisionEngine._available_at = staticmethod(
            lambda _idx, _emp, when: 6 <= when.hour < 14
        )
        first = AIDecisionEngine._assign_operators(
            index, "single-needle lockstitch", 10, 1, busy, 600.0,
            start_dt=monday, operator_reservations=reservations,
            order_priority=0.0,
        )
        _, true_end, true_segments = productive_segments(
            monday, 600.0, lambda t: 6 <= t.hour < 14
        )
        first_stored = tuple(reservations[str(employee_id)][0])
        second = AIDecisionEngine._assign_operators(
            index, "single-needle lockstitch", 10, 1, busy, 60.0,
            start_dt=tuesday, operator_reservations=reservations,
            order_priority=0.0,
        )
    finally:
        AIDecisionEngine._available_at = original

    second_end = tuesday.replace(hour=7)
    overlaps = any(s < second_end and tuesday < e for s, e in true_segments)
    print(json.dumps({
        "first_assignment": first[0],
        "stored_first_reservation": [v.isoformat() for v in first_stored],
        "calendar_true_end": true_end.isoformat(),
        "calendar_true_segments": [
            [start.isoformat(), end.isoformat()] for start, end in true_segments
        ],
        "second_assignment_inside_resumed_segment": second[0],
        "second_was_accepted": bool(second[0]),
        "second_overlaps_true_resumed_segment": overlaps,
        "defect_reproduced": bool(second[0]) and overlaps,
    }, indent=2))


if __name__ == "__main__":
    main()
