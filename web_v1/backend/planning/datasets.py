"""Discover pinned captures. Reads only report.json and manifest.json, never the
(large) source files, and never connects to a database."""
import json
import re
from pathlib import Path
from django.conf import settings
from .models import Dataset

_LOAD = re.compile(r'load(\d{2,3})')
_STAMP = re.compile(r'capture_(\d{8}T\d{6})')


def _describe(path):
    report = json.loads((path / 'report.json').read_text(encoding='utf-8'))
    manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    dataset = report.get('dataset') or {}
    database = str(dataset.get('database') or '')
    load = _LOAD.search(database)
    stamp = _STAMP.search(path.name)
    factories = []
    for summary in report.get('factory_summaries') or ():
        fid = int(summary['factory_id'])
        if f'factory{fid}_source.json' not in manifest or not (path / f'factory{fid}_readiness.json').exists():
            continue  # only factories whose pinned files are present and in the manifest
        resources = summary.get('resource_counts') or {}
        factories.append({'factory_id': fid, 'orders': summary.get('orders'),
                          'operations': summary.get('operations'), 'lines': resources.get('lines'),
                          'machines': resources.get('machines'), 'workers': resources.get('workers')})
    load_pct = int(load.group(1)) if load else None
    if load_pct is not None:
        label = f'{load_pct}% load'
    elif database:
        label = 'Factory database'
    else:
        label = path.name
    return {'label': label, 'database': database, 'load_pct': load_pct,
            'reference_time': str(dataset.get('reference_time') or ''),
            'factories': sorted(factories, key=lambda f: f['factory_id']),
            'captured_at': stamp.group(1) if stamp else ''}


def discover():
    """Register every complete capture under the configured roots (idempotent)."""
    found = []
    for root in settings.PLANNING_CAPTURE_ROOTS:
        root = Path(root)
        if not root.is_dir():
            continue
        for path in sorted(root.glob('capture_*')):
            if not (path / 'report.json').exists() or not (path / 'manifest.json').exists():
                continue
            try:
                info = _describe(path)
            except (ValueError, KeyError, TypeError, OSError):
                continue
            if not info['factories']:
                continue
            origin = 'LIVE_CAPTURE' if (path / 'web_capture.json').exists() else 'SNAPSHOT'
            dataset, _created = Dataset.objects.update_or_create(
                slug=path.name, defaults={**info, 'origin': origin, 'path': str(path.resolve())})
            found.append(dataset)
    return found


def factory_of(dataset, factory_id):
    return next((f for f in dataset.factories if f['factory_id'] == int(factory_id)), None)
