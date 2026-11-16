"""Discover pinned captures. Reads only report.json and manifest.json, never the
(large) source files, and never connects to a database."""
import json
import re
from pathlib import Path
from django.conf import settings
from .models import Dataset

_LOAD = re.compile(r'load(\d{2,3})')
# The original v1 demo database: kept for older runs, not offered (the app shows 30/60/90% load).
HIDDEN_DATABASES = {'garment_fyp_db'}
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


def base_database(database):
    """The original database name (a demo copy's capture belongs to its original's dataset)."""
    suffix = settings.CRAFT_DEMO_SUFFIX
    return database[:-len(suffix)] if suffix and database.endswith(suffix) else database


def _dataset_of(database):
    """The listed dataset for this database (the oldest active one), or None."""
    base = base_database(database)
    for dataset in Dataset.objects.filter(active=True).order_by('pk'):
        if base_database(dataset.database) == base:
            return dataset
    return None


def _refresh(dataset, path, info):
    """Point the dataset at a newer capture; its name, plans and switches stay. False when not newer."""
    if (info['captured_at'] or '') <= (dataset.captured_at or ''):
        return False
    Dataset.objects.filter(pk=dataset.pk).update(path=str(path.resolve()), reference_time=info['reference_time'],
                                                 factories=info['factories'], captured_at=info['captured_at'])
    return True


def discover():
    """Register every complete capture under the configured roots (idempotent).

    A capture taken from the app (web_capture.json) refreshes the dataset of the same database in place,
    so the list keeps one entry per load; each plan keeps the folder it was planned on (PlanRun.source_path)."""
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
            if origin == 'LIVE_CAPTURE' and info['database']:
                target = _dataset_of(info['database'])
                if target is not None and target.slug != path.name:
                    _refresh(target, path, info)
                    found.append(Dataset.objects.get(pk=target.pk))
                    continue
            existing = Dataset.objects.filter(slug=path.name).first()
            if existing is not None and existing.path != str(path.resolve()):
                found.append(existing)  # refreshed by a later capture: this folder no longer drives it
                continue
            # 'active' is only chosen when a capture is first found, so hiding one sticks.
            dataset, _created = Dataset.objects.update_or_create(
                slug=path.name, defaults={**info, 'origin': origin, 'path': str(path.resolve())},
                create_defaults={**info, 'origin': origin, 'path': str(path.resolve()),
                                 'active': info['database'] not in HIDDEN_DATABASES})
            found.append(dataset)
    return found


def factory_of(dataset, factory_id):
    return next((f for f in dataset.factories if f['factory_id'] == int(factory_id)), None)


def fold_duplicates(model=Dataset):
    """One listed dataset per database: the oldest keeps its name and takes the newest capture; the rest are
    hidden (their runs still load). Used once to merge entries made before captures refreshed in place."""
    groups = {}
    for dataset in model.objects.filter(active=True).order_by('pk'):
        groups.setdefault(base_database(dataset.database), []).append(dataset)
    for database, rows in groups.items():
        if not database or len(rows) < 2:
            continue
        keeper, newest = rows[0], max(rows, key=lambda d: d.captured_at or '')
        if newest.pk != keeper.pk and (newest.captured_at or '') > (keeper.captured_at or ''):
            model.objects.filter(pk=keeper.pk).update(path=newest.path, reference_time=newest.reference_time,
                                                      factories=newest.factories, captured_at=newest.captured_at)
        model.objects.filter(pk__in=[d.pk for d in rows[1:]]).update(active=False)
