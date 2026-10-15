"""Reproducible final checks; logs are confined to this audit directory."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
env = {**os.environ, 'DJANGO_SECRET_KEY': secrets.token_urlsafe(48)}
python = str(ROOT / 'venv/Scripts/python.exe')
django = str(ROOT / 'web_v1/backend/.venv/Scripts/python.exe')
groups = {
    'boundary': [([python, '-m', 'pytest', 'web_v1/backend/integrations/craft/tests', '-q', '-p', 'no:cacheprovider'], ROOT)],
    'continuity': [([python, '-m', 'pytest', *['tests/'+name for name in (
        'test_boundary_aware_modes.py', 'test_replay_semantics.py', 'test_rolling_horizon_v2_step3a.py',
        'test_rolling_horizon_v2_step3b.py', 'test_production_controller_v1.py', 'test_worker_reservation_boundaries.py',
        'test_worker_pause_resume_contract.py', 'test_rolling_worker_segment_carry.py', 'test_incremental_calendar_regimes.py',
        'test_calendar_regime_coalescing.py')], '-k', 'not window14', '-q', '-p', 'no:cacheprovider'], ROOT)],
    'governance': [([django, 'web_v1/backend/manage.py', 'test', 'governance', '--settings=config.settings_integration', '--verbosity', '1'], ROOT)],
    'checks': [([django, 'web_v1/backend/manage.py', 'check', '--settings=config.settings_integration'], ROOT),
               ([django, 'web_v1/backend/manage.py', 'makemigrations', '--check', '--dry-run', '--settings=config.settings_integration'], ROOT),
               ([django, 'web_v1/backend/manage.py', 'test', 'api', '--settings=config.settings', '--verbosity', '1'], ROOT)],
    'frontend': [(['npm.cmd', *args], ROOT/'web_v1/frontend') for args in [('test',), ('run', 'lint'), ('run', 'build')]],
}
results = []
for group in (sys.argv[1:] or groups.keys()):
    for index, (command, cwd) in enumerate(groups[group]):
        start = time.perf_counter()
        try:
            completed = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                                       encoding='utf-8', errors='replace', timeout=600)
            code, log = completed.returncode, completed.stdout+'\n'+completed.stderr
        except subprocess.TimeoutExpired:
            code, log = -1, 'TIMEOUT: no pass claimed'
        name = f'{group}_{index}'
        (OUT / f'{name}.log').write_text(log, encoding='utf-8')
        result = {'name': name, 'command': command, 'returncode': code, 'seconds': time.perf_counter()-start,
                  'status': 'PASS' if code == 0 else 'FAIL', 'log': f'{name}.log'}
        results.append(result)
        print(json.dumps(result), flush=True)
    (OUT / f'{group}_results.json').write_text(json.dumps([r for r in results if r['name'].startswith(group+'_')], indent=2), encoding='utf-8')
sys.exit(int(any(r['returncode'] for r in results)))
