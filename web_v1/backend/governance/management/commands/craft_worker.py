"""One claimed job per invocation, intended for an external supervisor in development."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from django.core.management.base import BaseCommand, CommandError
from governance.services import claim_run, finish_run, expire_abandoned_runs


class Command(BaseCommand):
    help = 'Execute one isolated synthetic CRAFT proof job; never admits live data.'

    def handle(self, **options):
        root = Path(__file__).resolve().parents[5]
        executable = os.environ.get('CRAFT_WORKER_PYTHON')
        if not executable or not Path(executable).is_file():
            raise CommandError('Set CRAFT_WORKER_PYTHON to the scheduler virtualenv python.exe')
        expire_abandoned_runs()
        run = claim_run()
        if run is None:
            self.stdout.write('No queued job')
            return
        try:
            with tempfile.TemporaryDirectory(prefix='craft-proof-') as directory:
                output = Path(directory) / 'result.json'
                contract = run.snapshot.get('contract')
                scripts = {
                    'RESIDUAL_VERTICAL_SLICE_V1': root / 'web_v1/audits/industrial_readiness_v1/run_fixture.py',
                    'FACTORY1_PARTIAL_CHAIN_V1': root / 'web_v1/audits/fyp_completion_20261010/run_partial_chain_smoke.py',
                    'SYNTHETIC_PRIORITY_TWO_ORDER_V1': root / 'web_v1/backend/integrations/craft/run_priority_fixture.py',
                    'SYNTHETIC_GLOBAL_RESOURCE_V1': root / 'web_v1/backend/integrations/craft/run_global_state_fixture.py',
                    'FACTORY1_GOVERNED_BOUNDED_V1': root / 'web_v1/backend/integrations/craft/run_factory1_governed.py',
                    'CAPTURE_GOVERNED_TEST_V1': root / 'web_v1/backend/integrations/craft/run_capture_governed.py',
                }
                script = scripts.get(contract)
                if script is None:
                    raise CommandError('Unsupported controlled scheduling contract')
                arguments = [executable, str(script), str(output)]
                if contract in ('SYNTHETIC_PRIORITY_TWO_ORDER_V1', 'SYNTHETIC_GLOBAL_RESOURCE_V1',
                                'FACTORY1_GOVERNED_BOUNDED_V1', 'CAPTURE_GOVERNED_TEST_V1'):
                    snapshot_path = Path(directory) / 'request_snapshot.json'
                    snapshot_path.write_text(json.dumps(run.snapshot, sort_keys=True), encoding='utf-8')
                    arguments.append(str(snapshot_path))
                if contract in ('FACTORY1_GOVERNED_BOUNDED_V1', 'CAPTURE_GOVERNED_TEST_V1') and run.snapshot.get('cycle_target'):
                    checkpoint_path = Path(directory) / 'cycle_checkpoint.json'
                    checkpoint_path.write_text(json.dumps(run.result.get('checkpoint'), sort_keys=True),
                                               encoding='utf-8')
                    arguments.append(str(checkpoint_path))
                child = subprocess.run(arguments,
                    cwd=root, capture_output=True, text=True, timeout=180, check=False)
                if child.returncode or not output.exists():
                    if child.stderr:
                        self.stderr.write(child.stderr[-4000:])
                    result = {'reason': 'WORKER_PROCESS_FAILED', 'exit_code': child.returncode}
                else:
                    result = json.loads(output.read_text(encoding='utf-8'))
        except subprocess.TimeoutExpired:
            result = {'reason': 'WORKER_TIME_LIMIT', 'controller_status': 'COMPUTATIONAL_LIMIT',
                      'physical_infeasibility_proven': False, 'existing_schedule_preserved': True}
        except (OSError, ValueError):
            result = {'reason': 'WORKER_IO_FAILURE'}
        try:
            final = finish_run(run.pk, result)
        except Exception:
            # Never reactivate a cancelled run or change active schedules.
            raise CommandError('Result not admitted; inspect run state. Active schedule is preserved.')
        self.stdout.write(f'{run.pk}: {final}')
