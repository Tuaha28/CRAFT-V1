"""The approved CRAFT V3 plan as the floor sees it: per line, per day, per hour.

Times in a plan are seconds from its reference time (midnight of the capture
date). An operation's planned pieces are spread over its productive segments
(shift pauses and the setup carry no output), so the target for an hour is
pieces x productive seconds in that hour / all productive seconds.
"""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from planning.models import PlanRun

DAY, HOUR = 86400, 3600


def approved_plan(dataset_id, factory_id):
    """The plan the floor works to: the most recently approved finished plan."""
    return (PlanRun.objects.filter(kind='SCHEDULE', status='SUCCEEDED', decision='APPROVED',
                                   dataset_id=dataset_id, factory_id=factory_id)
            .select_related('dataset').order_by('-decided_at').first())


class Plan:
    def __init__(self, run):
        out = Path(run.out_dir)
        digest = json.loads((out / 'digest.json').read_text(encoding='utf-8'))
        schedule = json.loads((out / 'craft_v3_schedule.json').read_text(encoding='utf-8'))
        self.run = run
        self.reference = datetime.fromisoformat(digest['reference_time'])
        segments = {r['operation_key']: [(int(a), int(b)) for a, b in r['worker_segments']] for r in schedule}
        self.ops = {}
        for row in digest['gantt'].get('CRAFT_V3', ()):
            segs = segments.get(row['key'], [])
            productive = sum(b - a for a, b in segs)
            self.ops[row['key']] = {**row, 'segments': segs, 'productive': productive}
        self.due = {r['order_id']: r.get('due_day') for r in digest.get('order_rows', ())}

    def lines(self):
        return sorted({op['line'] for op in self.ops.values()})

    def date_of(self, day):
        return (self.reference + timedelta(days=day)).date()

    def day_of(self, value: date):
        return (value - self.reference.date()).days

    def clock(self, seconds):
        return (self.reference + timedelta(seconds=seconds)).strftime('%H:%M')

    def planned_between(self, op, start, end):
        """Planned pieces of one operation in [start, end)."""
        if not op['productive'] or not op['pieces']:
            return 0.0
        inside = sum(max(0, min(b, end) - max(a, start)) for a, b in op['segments'])
        return op['pieces'] * inside / op['productive']

    def work_days(self, line):
        days = set()
        for op in self.ops.values():
            if op['line'] == line:
                for a, b in op['segments']:
                    days.update(range(a // DAY, (b - 1) // DAY + 1))
        return sorted(days)

    def day_ops(self, line, day):
        """Operations of a line with productive work on a plan day."""
        start, end = day * DAY, (day + 1) * DAY
        result = []
        for op in self.ops.values():
            if op['line'] != line:
                continue
            today = [(max(a, start), min(b, end)) for a, b in op['segments'] if a < end and b > start]
            if not today:
                continue
            result.append({'key': op['key'], 'order_id': op['order_id'], 'step': op['step'], 'name': op['name'],
                           'machine_type': op['machine_type'], 'crew': op['crew'], 'pieces': op['pieces'],
                           'workers': op['workers'], 'machines': op['machines'],
                           'windows': [[self.clock(a), '24:00' if b == end else self.clock(b)] for a, b in today],
                           'planned_today': round(self.planned_between(op, start, end)),
                           'planned_before': round(self.planned_between(op, 0, start)),
                           'due_day': self.due.get(op['order_id']),
                           'start': op['start'], 'end': op['end']})
        return sorted(result, key=lambda r: (r['windows'][0][0], r['order_id'], r['step']))

    def hourly_targets(self, line, day):
        """{operation key: {hour 0-23: planned pieces}} for a line on a plan day (hours with work only)."""
        targets = {}
        for op in self.ops.values():
            if op['line'] != line:
                continue
            for hour in range(24):
                start = day * DAY + hour * HOUR
                value = self.planned_between(op, start, start + HOUR)
                if value > 0:
                    targets.setdefault(op['key'], {})[hour] = value
        return targets


_CACHE = {}


def load(run):
    """Plans are immutable once finished, so cache them by run id."""
    if run.pk not in _CACHE:
        _CACHE[run.pk] = Plan(run)
    return _CACHE[run.pk]
