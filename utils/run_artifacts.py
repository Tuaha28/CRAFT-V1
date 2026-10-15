"""Explicit run identities, with content hashes to detect stale sidecars."""
import hashlib
import json
import os
import uuid
from pathlib import Path

_RUN_ID = os.environ.get('SCHEDULING_RUN_ID') or str(uuid.uuid4())

class RunIdentityError(RuntimeError):
    pass


def current_run_id():
    return _RUN_ID


def stamp_artifact(path, run_id=None):
    path = Path(path)
    payload = {'version': 1, 'run_id': run_id or current_run_id(),
               'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    sidecar = Path(str(path) + '.run.json')
    temporary = Path(str(sidecar) + '.tmp')
    temporary.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    temporary.replace(sidecar)


def require_same_run(paths):
    identities = {}
    for name in paths:
        path = Path(name)
        if not path.exists():
            continue  # optional absent input contributes no data
        try:
            meta = json.loads(Path(str(path) + '.run.json').read_text(encoding='utf-8'))
            if meta.get('version') != 1 or not meta.get('run_id'):
                raise ValueError('missing run identity')
            if meta.get('sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
                raise ValueError('artifact contents no longer match their run stamp')
            identities[str(path)] = meta['run_id']
        except (OSError, ValueError, TypeError) as exc:
            raise RunIdentityError(f'Cannot verify report input {path}: {exc}') from exc
    if len(set(identities.values())) > 1:
        raise RunIdentityError(f'Report inputs come from different runs: {identities}')
    return next(iter(identities.values()), current_run_id())
