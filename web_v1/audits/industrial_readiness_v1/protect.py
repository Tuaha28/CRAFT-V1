"""Capture/verify protected content; never writes outside this audit folder."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent


def fingerprints():
    result = {}
    for directory in ('crafter', 'ga', 'models', 'graph', 'agent', 'outputs'):
        base = ROOT / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                checksum = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024*1024), b''):
                        checksum.update(chunk)
                result[path.relative_to(ROOT).as_posix()] = checksum.hexdigest()
    return result


if __name__ == '__main__':
    before = OUT / 'protected_before.json'
    now = fingerprints()
    if not before.exists():
        before.write_text(json.dumps(now, indent=2, sort_keys=True), encoding='utf-8')
        print('Captured', len(now), 'protected files before core changes')
    else:
        previous = json.loads(before.read_text(encoding='utf-8'))
        changed = [key for key in sorted(previous.keys() | now.keys()) if previous.get(key) != now.get(key)]
        result = {'changed_files': changed, 'outputs_unchanged': not any(k.startswith('outputs/') for k in changed),
                  'legacy_algorithms_unchanged': not any(k.startswith(('ga/', 'models/', 'graph/', 'agent/')) for k in changed)}
        (OUT / 'protected_verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result))
