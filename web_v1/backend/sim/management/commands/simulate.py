"""Experiment: replay an approved plan many times (seeds x breakdown rates) and save planned vs executed results.

    manage.py simulate --run <plan id> [--days 30] [--seeds 1-10] [--breakdown 0,0.0015,0.003] [--absence 0.02]

Writes <experiments dir>/simulation/<plan>_<time>.json (every order of every run) and summary.csv (one row per
breakdown rate: means over the seeds). In memory only: no app records are written."""
import csv
import json
import statistics
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from floor.plan import load
from planning.models import PlanRun
from sim.engine import Rates
from sim.experiment import run


def _seeds(text):
    if '-' in text:
        a, b = (int(x) for x in text.split('-', 1))
        return list(range(a, b + 1))
    return [int(x) for x in text.split(',') if x.strip()]


class Command(BaseCommand):
    help = 'Replay an approved plan under execution noise and breakdowns; save planned vs executed results.'

    def add_arguments(self, parser):
        parser.add_argument('--run', required=True, help='id of a finished plan')
        parser.add_argument('--days', type=int, default=30)
        parser.add_argument('--seeds', default='1-10')
        parser.add_argument('--breakdown', default='0,0.0015,0.003', help='breakdown chance per machine-hour, comma list')
        parser.add_argument('--absence', type=float, default=0.02, help='absence chance per worker-day')

    def handle(self, **o):
        found = PlanRun.objects.filter(pk=o['run'], kind='SCHEDULE', status='SUCCEEDED').first()
        if found is None:
            raise CommandError('No finished plan with that id')
        plan = load(found)
        rates = [float(x) for x in o['breakdown'].split(',') if x.strip()]
        seeds = _seeds(o['seeds'])
        results, summary = [], []
        for rate in rates:
            rows = [run(plan, days=o['days'], seed=s,
                        rates=replace(Rates(), breakdown_per_hour=rate, absence_per_day=o['absence'])) for s in seeds]
            results.append({'breakdown_per_hour': rate, 'runs': rows})
            avg = lambda key: statistics.fmean(r[key] for r in rows if r[key] is not None) if any(
                r[key] is not None for r in rows) else None
            summary.append({'breakdown_per_hour': rate, 'seeds': len(seeds), 'orders': len(rows[0]['orders']),
                            'on_time_planned': avg('on_time_planned'), 'on_time_simulated': avg('on_time_simulated'),
                            'mean_lateness_planned_days': avg('mean_lateness_planned'),
                            'mean_lateness_simulated_days': avg('mean_lateness_simulated'),
                            'adherence': avg('adherence'), 'breakdowns': avg('breakdowns'),
                            'absences': avg('absences'), 'unfinished': avg('unfinished')})
            self.stdout.write(f'breakdown {rate}: on time {summary[-1]["on_time_planned"]} planned -> '
                              f'{summary[-1]["on_time_simulated"]} simulated, adherence {summary[-1]["adherence"]}')
        out = Path(settings.PLANNING_EXPERIMENTS_DIR) / 'simulation'
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%dT%H%M%S')
        name = f'{str(found.pk)[:8]}_{stamp}'
        (out / f'{name}.json').write_text(json.dumps({'run': str(found.pk), 'factory_id': found.factory_id,
                                                      'dataset': found.dataset.label, 'days': o['days'],
                                                      'absence_per_day': o['absence'], 'results': results},
                                                     indent=1), encoding='utf-8')
        with open(out / f'{name}_summary.csv', 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=list(summary[0]))
            writer.writeheader()
            writer.writerows(summary)
        self.stdout.write(self.style.SUCCESS(f'Saved {out / name}.json and {name}_summary.csv'))
