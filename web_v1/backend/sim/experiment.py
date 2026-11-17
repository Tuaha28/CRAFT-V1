"""Experiment mode: replay one approved plan with the engine, no clock and no database, and compare the plan
with what the simulated floor achieved (for the paper: plans under execution noise and disruptions)."""
import random
from .engine import DAY, HOUR, State, advance


def _planned_finish(plan):
    """{order id: plan seconds its last step ends}."""
    finish = {}
    for op in plan.ops.values():
        end = max((b for _a, b in op['segments']), default=0)
        finish[op['order_id']] = max(finish.get(op['order_id'], 0), end)
    return finish


def run(plan, *, days, seed, rates):
    state = State()
    rng = random.Random(seed)
    horizon = int(days) * DAY
    pieces = {k: int(op.get('pieces') or 0) for k, op in plan.ops.items()}
    finished_at, planned_done, on_time_done = {}, 0.0, 0.0
    breakdowns = absences = 0
    for h in range(0, horizon, HOUR):
        events = advance(plan, state, h, h + HOUR, rng, rates)
        out = {}
        for e in events:
            if e['type'] == 'OUTPUT':
                out[e['key']] = out.get(e['key'], 0) + e['pieces']
            elif e['type'] == 'BREAKDOWN':
                breakdowns += 1
            elif e['type'] == 'ABSENT':
                absences += 1
        for key, op in plan.ops.items():
            hour_plan = plan.planned_between(op, h, h + HOUR)
            planned_done += hour_plan
            on_time_done += min(hour_plan, out.get(key, 0))
            if key not in finished_at and pieces[key] and state.done.get(key, 0) >= pieces[key]:
                finished_at[key] = h + HOUR
        if len(finished_at) == len([k for k in pieces if pieces[k]]):
            break
    planned = _planned_finish(plan)
    due = getattr(plan, 'due', {}) or {}
    orders, unfinished = [], 0
    for order_id in sorted(planned, key=lambda o: (len(o), o)):
        keys = [k for k, op in plan.ops.items() if op['order_id'] == order_id and pieces[k]]
        done = all(k in finished_at for k in keys)
        unfinished += 0 if done else 1
        simulated = max((finished_at.get(k, horizon) for k in keys), default=0) / DAY
        planned_day, due_day = planned[order_id] / DAY, due.get(order_id)
        orders.append({'order_id': order_id, 'planned_finish_day': round(planned_day, 4),
                       'simulated_finish_day': round(simulated, 4) if done else None,
                       'due_day': due_day, 'finished': done,
                       'lateness_planned': max(0.0, planned_day - due_day) if due_day is not None else None,
                       'lateness_simulated': max(0.0, simulated - due_day) if due_day is not None else None})
    with_due = [o for o in orders if o['due_day'] is not None]
    share = lambda key: (sum(1 for o in with_due if o[key] == 0) / len(with_due)) if with_due else None
    mean = lambda key: (sum(o[key] for o in with_due) / len(with_due)) if with_due else None
    return {'seed': seed, 'days': days, 'orders': orders, 'unfinished': unfinished,
            'on_time_planned': share('lateness_planned'), 'on_time_simulated': share('lateness_simulated'),
            'mean_lateness_planned': mean('lateness_planned'), 'mean_lateness_simulated': mean('lateness_simulated'),
            'adherence': (on_time_done / planned_done) if planned_done else None,
            'breakdowns': breakdowns, 'absences': absences}
