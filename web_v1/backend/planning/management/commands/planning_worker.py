"""Background worker: runs queued CRAFT V3 plans and read-only captures, one at a time."""
from collections import deque
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from governance.management.commands.craft_worker import _last_exception_line, _redact
from planning import autoplan, jobs, notify, services
from planning.datasets import discover

POLL_SECONDS = 5
AUTOPLAN_SECONDS = 60  # how often the auto-planning watcher looks at stored triggers
SIM_SECONDS = 5  # how often a running factory clock moves (sim app)


def _tail(path, lines=40):
    try:
        with open(path, encoding='utf-8', errors='replace') as stream:
            kept = deque((line.rstrip() for line in stream if '[INFO]' not in line), maxlen=lines)
    except OSError:
        return ''
    return _redact('\n'.join(kept))[-6000:]


def _stop(process):
    if process.poll() is not None:
        return
    if sys.platform == 'win32':  # the runner starts CP-SAT threads only, but kill the whole tree
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(process.pid)], capture_output=True, check=False)
    else:
        process.kill()
    process.wait(timeout=60)


class Command(BaseCommand):
    help = 'Run queued CRAFT V3 plans and read-only database captures (one at a time).'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='handle at most one queued run, then exit')

    def handle(self, **options):
        if not Path(settings.PLANNING_SCHEDULER_PYTHON).is_file():
            self.stderr.write('Set CRAFT_WORKER_PYTHON to the scheduler virtualenv python.exe')
            return
        self.stdout.write('Planning worker ready')
        last_tick = 0.0
        last_sim = 0.0
        while True:
            services.worker_beat()
            services.expire_orphans()
            if apps.is_installed('sim') and time.monotonic() - last_sim >= SIM_SECONDS:
                last_sim = time.monotonic()
                try:
                    from sim import runner as sim_runner
                    sim_runner.tick()  # the simulated factory clock, if one is running
                except Exception as exc:  # the simulator must never stop the worker
                    self.stderr.write(f'Factory simulator failed: {type(exc).__name__}: {_redact(str(exc))[:300]}')
            if time.monotonic() - last_tick >= AUTOPLAN_SECONDS:
                last_tick = time.monotonic()
                try:
                    for error in autoplan.watch():  # each step is guarded on its own
                        self.stderr.write(f'Auto-planning check failed: {_redact(error)[:300]}')
                except Exception as exc:  # the watcher must never stop the worker
                    self.stderr.write(f'Auto-planning check failed: {type(exc).__name__}: {_redact(str(exc))[:300]}')
            run = services.claim()
            if run is not None:
                self.stdout.write(f'Run {run.pk} ({run.kind}) started')
                try:
                    self.run_job(run)
                except Exception as exc:  # a failed run must never stop the worker
                    services.finish(run.pk, 'FAILED', stage='Failed', error=_redact(str(exc))[:2000])
                self.notify(run)
                self.stdout.write(f'Run {run.pk} finished')
            if options['once']:
                return
            if run is None:
                time.sleep(POLL_SECONDS)

    def notify(self, run):
        """Email the managers about a finished automatic run (notify never raises; belt and braces here)."""
        try:
            run.refresh_from_db()
            if run.kind == 'SCHEDULE' and run.status in ('SUCCEEDED', 'FAILED'):
                notify.run_finished(run)
        except Exception as exc:  # email must never stop the worker
            self.stderr.write(f'Email for run {run.pk} failed: {type(exc).__name__}')

    def watch(self, run, process, log_path, out_dir=None):
        """Wait for the child, beating and reporting progress; stop it on cancel."""
        while process.poll() is None:
            time.sleep(POLL_SECONDS)
            services.worker_beat()
            if not services.still_running(run.pk):
                _stop(process)
                return None
            fields = {'log_tail': _tail(log_path)}
            if out_dir is not None:
                fraction, stage = jobs.progress(out_dir)
                fields.update(progress=fraction, stage=stage)
            services.beat(run.pk, **fields)
        return process.returncode

    def run_job(self, run):
        base = Path(settings.PLANNING_RUNS_DIR) / str(run.pk)
        base.mkdir(parents=True, exist_ok=True)
        log_path = base / 'run.log'
        if run.kind == 'CAPTURE':
            return self.capture(run, base, log_path)
        out_dir = base / 'craft'
        if run.scenario and not run.scenario_dir:
            services.beat(run.pk, stage='Preparing the what-if data')
            scenario_dir = jobs.build_scenario(run, base)
            services.beat(run.pk, scenario_dir=scenario_dir)
            run.refresh_from_db()
        services.beat(run.pk, out_dir=str(out_dir), stage=jobs.STAGE_LABELS['EDD'])
        with open(log_path, 'w', encoding='utf-8') as log:
            process = subprocess.Popen(jobs.schedule_command(run, out_dir), cwd=jobs.ROOT,
                                       env=jobs.scheduler_env(run.capture_path, run.mode),
                                       stdout=log, stderr=subprocess.STDOUT)
            services.beat(run.pk, pid=process.pid)
            code = self.watch(run, process, log_path, out_dir)
        if code is None:
            return  # cancelled
        tail = _tail(log_path)
        if code != 0:
            services.finish(run.pk, 'FAILED', stage='Failed', log_tail=tail,
                            error=_last_exception_line(tail) or f'CRAFT exited with code {code}')
            return
        services.beat(run.pk, stage='Writing results', progress=0.99, log_tail=tail)
        run.refresh_from_db()
        data = jobs.digest(run)
        services.finish(run.pk, 'SUCCEEDED', stage='Done', progress=1.0, summary=jobs.summarize(data, run))

    def capture(self, run, base, log_path):
        root = jobs.CAPTURE_SCRIPT.parent
        before = {p.name for p in root.glob('capture_*')}
        env = {k: v for k, v in os.environ.items() if not k.startswith('DJANGO_')}
        if run.scenario.get('database'):  # which factory database to read (e.g. a demo copy)
            env['LEGACY_DB_NAME'] = run.scenario['database']
        services.beat(run.pk, stage='Reading the database (read-only)', out_dir=str(base))
        with open(log_path, 'w', encoding='utf-8') as log:
            process = subprocess.Popen(jobs.capture_command(), cwd=jobs.ROOT, env=env,
                                       stdout=log, stderr=subprocess.STDOUT)
            services.beat(run.pk, pid=process.pid)
            code = self.watch(run, process, log_path)
        if code is None:
            return
        tail = _tail(log_path)
        new = sorted(p for p in root.glob('capture_*') if p.name not in before and (p / 'report.json').exists())
        if code != 0 or not new:
            services.finish(run.pk, 'FAILED', stage='Failed', log_tail=tail,
                            error=_last_exception_line(tail) or f'Capture exited with code {code}')
            return
        (new[-1] / 'web_capture.json').write_text(json.dumps(
            {'run_id': str(run.pk), 'requested_by': run.requested_by.get_username(),
             'finished_at': timezone.now().isoformat()}), encoding='utf-8')
        discover()
        from planning.models import Dataset
        into = Dataset.objects.filter(path=str(new[-1].resolve())).first()
        services.finish(run.pk, 'SUCCEEDED', stage='Done', progress=1.0, log_tail=tail,
                        summary={'dataset': into.slug if into else new[-1].name, 'capture': new[-1].name})
