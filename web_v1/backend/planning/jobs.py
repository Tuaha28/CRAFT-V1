"""How the worker runs CRAFT V3, reads its progress and ingests its result.

The scheduler itself is never imported here: every run is a subprocess of the
scheduler virtualenv (settings.PLANNING_SCHEDULER_PYTHON), exactly the command
the paper experiments used, so the web app cannot change CRAFT V3.
"""
import json
import os
from pathlib import Path
import subprocess
from django.conf import settings

ROOT = Path(__file__).resolve().parents[3]
RUNNER = ROOT / 'scratch' / 'run_craft_v2.py'
DIGEST = ROOT / 'web_v1' / 'backend' / 'integrations' / 'craft' / 'plan_digest.py'
ORDERBOOK = ROOT / 'web_v1' / 'backend' / 'integrations' / 'craft' / 'plan_orderbook.py'
SCENARIO = ROOT / 'web_v1' / 'backend' / 'integrations' / 'craft' / 'plan_scenario.py'
CAPTURE_SCRIPT = ROOT / 'web_v1' / 'audits' / 'factory_source_integration_v1' / 'capture_six_factories.py'

# FULL = the locked V3 settings of the paper experiments. QUICK = the same V3
# code with the runner's validated laptop limits and a short improvement.
MODES = {
    'FULL': {'stage_seconds': '10,20,30', 'objective_seconds': '30', 'improve_seconds': 1800},
    'QUICK': {'stage_seconds': '3,5,8', 'objective_seconds': '5', 'improve_seconds': 120},
}
# Order the runner records policies in, with each stage's typical share of the run.
# Only EDD runs of the simple rules: it is one of V3's starts. FIFO and SPT are
# comparison baselines for the experiments; V3 never reads them, so the app skips them.
STAGES = [('EDD', 0.05), ('EDD_CREW', 0.06), ('SAVABLE_FIRST_CREW', 0.06),
          ('CRAFT_TW', 0.43), ('CRAFT_V3', 0.40)]
STAGE_LABELS = {'FIFO': 'FIFO rule', 'EDD': 'EDD rule', 'SPT': 'SPT rule', 'EDD_CREW': 'EDD with crews',
                'SAVABLE_FIRST_CREW': 'Savable-first with crews', 'CRAFT_TW': 'CP-SAT time windows',
                'CRAFT_V3': 'CP-SAT improvement'}


def scheduler_env(dataset_path, mode=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('DJANGO_', 'LEGACY_DB_'))}
    env.update(DB_PASS='unused', CRAFT_CAPTURE_DIR=str(dataset_path), CRAFT_RESIDUAL_ROUTES='1',
               CRAFT_WORKER_LINE_POLICY='BATCH_LINE_THEN_FACTORY', CRAFT_SOLVER_WORKERS='8',
               CRAFT_DISPATCH_WINDOW_DAYS='14', PYTHONUNBUFFERED='1')
    if mode:
        env.update(CRAFT_STAGE_SECONDS=MODES[mode]['stage_seconds'],
                   CRAFT_OBJECTIVE_SECONDS=MODES[mode]['objective_seconds'])
    return env


def schedule_command(run, out_dir):
    orders = (['--order-ids', ','.join(str(o) for o in run.order_ids)] if run.order_ids
              else ['--orders', str(run.orders), '--order-offset', str(run.order_offset)])
    return [settings.PLANNING_SCHEDULER_PYTHON, str(RUNNER), str(run.factory_id), str(out_dir), *orders,
            '--seconds', str(MODES[run.mode]['improve_seconds']), '--horizon-extra-days', '90', '--mode', 'v3',
            '--rules', 'EDD']


def capture_command():
    return [settings.PLANNING_SCHEDULER_PYTHON, str(CAPTURE_SCRIPT)]


def progress(out_dir):
    """(fraction done, label of the running stage) from the runner's report.json."""
    try:
        report = json.loads((Path(out_dir) / 'report.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 0.0, STAGE_LABELS['EDD']
    done = {r['policy'] for r in report.get('results', ())}
    fraction = sum(share for name, share in STAGES if name in done)
    running = next((name for name, _share in STAGES if name not in done), None)
    return round(fraction, 3), STAGE_LABELS.get(running, 'Writing results')


def digest(run):
    """Workbook + digest.json from the finished run (scheduler subprocess)."""
    child = subprocess.run([settings.PLANNING_SCHEDULER_PYTHON, str(DIGEST), str(run.factory_id), run.out_dir],
                           cwd=ROOT, env=scheduler_env(run.capture_path), capture_output=True, text=True,
                           timeout=1800, check=False)
    path = Path(run.out_dir) / 'digest.json'
    if child.returncode or not path.exists():
        raise RuntimeError('DIGEST_FAILED: ' + (child.stderr or '')[-1500:])
    return json.loads(path.read_text(encoding='utf-8'))


def build_scenario(run, base_dir):
    """What-if copy of the capture for this run (scheduler subprocess); returns its folder."""
    out = Path(base_dir) / 'scenario'
    spec = Path(base_dir) / 'scenario_request.json'
    spec.write_text(json.dumps(run.scenario), encoding='utf-8')
    source = run.source_path or run.dataset.path
    child = subprocess.run([settings.PLANNING_SCHEDULER_PYTHON, str(SCENARIO), source, str(run.factory_id),
                            str(out), str(spec)], cwd=ROOT, env=scheduler_env(source),
                           capture_output=True, text=True, timeout=1800, check=False)
    if child.returncode or 'SCENARIO_OK' not in child.stdout:
        raise RuntimeError('SCENARIO_FAILED: ' + ((child.stdout or '') + (child.stderr or ''))[-1500:])
    return str(out)


def promise(data, order_id):
    """Promised delivery of the new order in a PROMISE run."""
    row = next((r for r in data.get('order_rows', ()) if r['order_id'] == str(order_id)), None)
    if row is None:
        return None
    mine = row['methods'].get('CRAFT_V3') or {}
    finish, due = mine.get('finish_day'), row.get('due_day')
    return {'order_id': str(order_id), 'finish_day': finish, 'due_day': due, 'complete': mine.get('complete'),
            'days_late': round(max(0.0, finish - due), 2) if finish is not None and due is not None else None,
            'status': row.get('status'), 'reason': row.get('reason')}


def summarize(data, run=None):
    """The small part of a digest kept in the database (lists, cards)."""
    methods = {m['policy']: m for m in data['methods']}
    v3, crew = methods.get('CRAFT_V3') or {}, methods.get('EDD_CREW') or {}
    return {'operations': data.get('operations'), 'reference_time': data.get('reference_time'),
            'methods': data['methods'], 'kept_start': (data.get('craft_v3') or {}).get('kept_start'),
            'craft_v3': {k: v3.get(k) for k in ('complete', 'late', 'mean_tardiness_days', 'makespan_days',
                                                 'workers_used', 'util_used_pct')},
            'vs_edd_crew': {'late': (v3.get('late') or 0) - (crew.get('late') or 0) if v3 and crew else None,
                            'mean_tardiness_days': (round(v3['mean_tardiness_days'] - crew['mean_tardiness_days'], 3)
                                                    if v3.get('mean_tardiness_days') is not None
                                                    and crew.get('mean_tardiness_days') is not None else None)},
            **({'promise': promise(data, run.scenario['new_order']['order_id'])}
               if run is not None and run.purpose == 'PROMISE' else {})}


def orderbook(dataset, factory_id):
    """Eligible order book (cached per capture and factory)."""
    snapshot = Path(dataset.path).name  # a refreshed dataset reads its new order book
    cache = Path(settings.PLANNING_RUNS_DIR) / 'orderbooks' / f'{dataset.slug}_{snapshot}_factory{int(factory_id)}.json'
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        partial = cache.with_suffix('.partial')
        child = subprocess.run([settings.PLANNING_SCHEDULER_PYTHON, str(ORDERBOOK), str(int(factory_id)), str(partial)],
                               cwd=ROOT, env=scheduler_env(dataset.path), capture_output=True, text=True,
                               timeout=900, check=False)
        if child.returncode or not partial.exists():
            raise RuntimeError('ORDERBOOK_FAILED: ' + (child.stderr or '')[-800:])
        partial.replace(cache)
    return json.loads(cache.read_text(encoding='utf-8'))
