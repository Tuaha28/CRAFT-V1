"""Plays an approved plan forward, one factory hour at a time. Pure: the caller stores the state and applies the
events (output, quality checks, breakdowns and repairs, absences and returns) through the normal app records.

Times are plan seconds (seconds after the plan's reference time), whole hours."""
import math
from dataclasses import dataclass, field

HOUR = 3600
DAY = 24 * HOUR


@dataclass
class Rates:
    breakdown_per_hour: float = 0.0015   # per busy machine per working hour
    repair_hours: tuple = (2, 8)
    absence_per_day: float = 0.02        # per planned worker per day
    defect: tuple = (0.02, 0.06)         # defect share in the daily check
    perf_mean: float = 0.95              # each operation's speed against plan (lognormal)
    perf_sd: float = 0.10


@dataclass
class State:
    done: dict = field(default_factory=dict)     # operation key -> pieces done (every plan run)
    perf: dict = field(default_factory=dict)     # operation key -> performance factor
    down: dict = field(default_factory=dict)     # machine id -> plan second it is repaired
    absent: dict = field(default_factory=dict)   # worker id -> plan day they are away
    stopped: set = field(default_factory=set)    # lines stopped
    day_out: dict = field(default_factory=dict)  # operation key -> pieces today (for the daily check)

    def to_json(self):
        return {'done': self.done, 'perf': self.perf, 'down': {str(k): v for k, v in self.down.items()},
                'absent': {str(k): v for k, v in self.absent.items()}, 'stopped': sorted(self.stopped),
                'day_out': self.day_out}

    @classmethod
    def from_json(cls, data):
        data = data or {}
        return cls(done=dict(data.get('done') or {}), perf=dict(data.get('perf') or {}),
                   down={int(k): int(v) for k, v in (data.get('down') or {}).items()},
                   absent={int(k): int(v) for k, v in (data.get('absent') or {}).items()},
                   stopped=set(data.get('stopped') or ()), day_out=dict(data.get('day_out') or {}))


def _perf(rng, rates):
    variance = math.log(1 + (rates.perf_sd / rates.perf_mean) ** 2)
    return rng.lognormvariate(math.log(rates.perf_mean) - variance / 2, math.sqrt(variance))


def _ids(values):
    return [int(v) for v in values or () if str(v).strip().lstrip('-').isdigit()]


def _shift_hours(plan):
    """Hours of the day each line works in the plan (late work continues only in those hours)."""
    hours = {}
    for op in plan.ops.values():
        for a, b in op['segments']:
            for h in range(a // HOUR, (b - 1) // HOUR + 1):
                hours.setdefault(op['line'], set()).add(h % 24)
    return hours


def _works_on(op, day):
    start, end = day * DAY, (day + 1) * DAY
    return any(a < end and b > start for a, b in op['segments'])


def advance(plan, state, t0, t1, rng, rates):
    """Events for the factory hours in [t0, t1); updates state."""
    events = []
    ops = [plan.ops[k] for k in sorted(plan.ops)]
    shifts = _shift_hours(plan)
    for h in range(int(t0), int(t1), HOUR):
        day = h // DAY
        for machine, until in sorted(state.down.items()):
            if until <= h:
                del state.down[machine]
                line = next((op['line'] for op in ops if machine in _ids(op['machines'])), None)
                events.append({'type': 'REPAIRED', 'machine': machine, 'line': line})
        for worker, away in sorted(state.absent.items()):
            if away < day:
                del state.absent[worker]
                events.append({'type': 'BACK', 'worker': worker})
        if h % DAY == 0 and rates.absence_per_day > 0:
            planned = sorted({(w, op['line']) for op in ops if _works_on(op, day) for w in _ids(op['workers'])})
            for worker, line in planned:
                if worker not in state.absent and rng.random() < rates.absence_per_day:
                    state.absent[worker] = day
                    events.append({'type': 'ABSENT', 'worker': worker, 'line': line, 'day': day})
        for op in ops:
            key, pieces = op['key'], int(op.get('pieces') or 0)
            if key not in state.perf:
                state.perf[key] = _perf(rng, rates)
            done = state.done.get(key, 0)
            if pieces <= 0 or done >= pieces or not op['productive']:
                continue
            due = plan.planned_between(op, 0, h + HOUR) - done   # this hour's plan plus any backlog
            if due <= 0:
                continue
            rate = pieces * HOUR / op['productive']               # planned pieces per productive hour
            in_window = plan.planned_between(op, h, h + HOUR) > 0
            late = h >= max(b for _a, b in op['segments']) and (h // HOUR) % 24 in shifts.get(op['line'], ())
            if not (in_window or late):
                continue
            machines, workers = _ids(op['machines']), _ids(op['workers'])
            if op['line'] in state.stopped or any(m in state.down for m in machines):
                continue
            available = 1 - (sum(w in state.absent for w in workers) / len(workers) if workers else 0)
            output = math.floor(min(pieces - done, due, rate * state.perf[key] * available))
            if output <= 0:
                continue
            state.done[key] = done + output
            state.day_out[key] = state.day_out.get(key, 0) + output
            events.append({'type': 'OUTPUT', 'key': key, 'line': op['line'], 'slot': h // HOUR, 'pieces': output})
            for machine in machines:
                if machine not in state.down and rng.random() < rates.breakdown_per_hour:
                    until = h + HOUR * rng.randint(*rates.repair_hours)
                    state.down[machine] = until
                    events.append({'type': 'BREAKDOWN', 'machine': machine, 'line': op['line'], 'until': until})
        if (h + HOUR) % DAY == 0 and state.day_out:
            lines = {op['key']: op['line'] for op in ops}
            for key in sorted(state.day_out):
                checked = min(state.day_out[key], 20)
                share = rng.uniform(*rates.defect)
                defects = sum(rng.random() < share for _ in range(checked))
                events.append({'type': 'CHECK', 'key': key, 'line': lines.get(key), 'day': day,
                               'checked': checked, 'defects': defects})
            state.day_out = {}
    return events
