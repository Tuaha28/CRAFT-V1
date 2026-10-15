"""Dataset-owned versioned calibration, committed with the generated rows.

No process cache or global output file: each read uses the connected database.
A missing legacy artifact warns; a corrupt or foreign artifact aborts.
"""
import json
from sqlalchemy import text
from utils.logger import get_logger
logger = get_logger('derived_constants', 'events.log')
_warned = set()


def load_derived_constants(refresh=False, *, engine=None):
    from database.db import get_engine
    with (engine or get_engine()).connect() as conn:
        database = conn.execute(text('SELECT DATABASE()')).scalar_one()
        exists = conn.execute(text("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=:db AND TABLE_NAME='Dataset_Calibration'"), {'db': database}).scalar_one()
        if not exists:
            return {}
        raw = conn.execute(text('SELECT payload FROM Dataset_Calibration WHERE singleton_id=1')).scalar_one()
    values = json.loads(raw)
    validate_calibration(values, database)
    return values


def get_derived(name, config_default, description=''):
    values = load_derived_constants()
    if name in values:
        return values[name]
    if values:
        raise ValueError(f'Dataset calibration is missing required value: {name}')
    if name not in _warned:
        _warned.add(name)
        logger.warning(f'Legacy database has no Dataset_Calibration; using config fallback for {name}. {description}')
    return config_default


def validate_calibration(values, database):
    """Validate exactly the duration factor and slack quantiles runtime reads."""
    import math
    import re
    from datetime import datetime
    if not isinstance(values, dict) or values.get('artifact_version') != 1:
        raise ValueError('Unsupported dataset calibration artifact version')
    if not isinstance(values.get('dataset_version'), str) or not values['dataset_version']:
        raise ValueError('Missing dataset version')
    if values.get('source_database') != database:
        raise ValueError(f"Calibration database mismatch: {values.get('source_database')} != {database}")
    factor = values.get('duration_calibration_factor')
    if isinstance(factor, bool) or not isinstance(factor, (float, int)) or not math.isfinite(factor) or factor <= 0:
        raise ValueError('Invalid duration_calibration_factor')
    q = values.get('slack_days_quantiles', {})
    points = [q.get(k) for k in ('p25', 'p50', 'p75')]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in points) or not points[0] < points[1] < points[2]:
        raise ValueError('Invalid slack_days_quantiles')
    count = values.get('order_count')
    if type(count) is not int or count <= 0:
        raise ValueError('Invalid expected order_count')
    datetime.fromisoformat(values['reference_time'])
    hashes = values.get('source_hashes', {})
    for name in ('grid.xls', 'Plan.xlsx', 'PPC-1.xlsx', 'WorkStation.xlsx'):
        if not re.fullmatch('[0-9a-f]{64}', hashes.get(name, '')):
            raise ValueError(f'Missing or invalid pre-generation hash for {name}')
    provenance = values.get('clone_provenance')
    if provenance is not None:
        if not isinstance(provenance, dict) or not provenance.get('source_database') or not provenance.get('source_dataset_version'):
            raise ValueError('Invalid clone provenance')
        datetime.fromisoformat(provenance['cloned_at'])
    return values


def calibration_for_clone(values, source_database, destination_database):
    """New dataset identity with explicit ancestry; source must itself validate."""
    import copy
    import uuid
    from datetime import datetime
    validate_calibration(values, source_database)
    cloned = copy.deepcopy(values)
    cloned['source_database'] = destination_database
    cloned['dataset_version'] = str(uuid.uuid4())
    cloned['clone_provenance'] = {
        'source_database': source_database,
        'source_dataset_version': values['dataset_version'],
        'cloned_at': datetime.now().isoformat(),
    }
    cloned['origin_database'] = values.get('origin_database', source_database)
    validate_calibration(cloned, destination_database)
    return cloned
