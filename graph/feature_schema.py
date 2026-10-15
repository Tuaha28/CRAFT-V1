"""Versioned operation input schema bound to the exact checkpoint bytes."""
import hashlib
import json
from pathlib import Path

OPERATION_FEATURE_NAMES = (
    'sam_norm', 'qty_norm', 'machine_parallelism_norm', 'deadline_budget_norm',
    'decisive_lot_breakdown_count_norm', 'mean_queue_delay_norm',
    'routing_step_norm', 'steps_remaining_norm', 'pool_occupancy_norm',
    'mean_breakdown_count_norm', 'has_dispatch_context', 'has_assignment_history',
)

def operation_schema():
    return {'version': 2, 'names': list(OPERATION_FEATURE_NAMES), 'count': len(OPERATION_FEATURE_NAMES),
            'semantics': {'has_dispatch_context': '1 only if every supplied lot has finite queue_delay_mins_at_dispatch and machine_breakdown_count_at_dispatch and a nonnull planned_end_time. Otherwise the decisive breakdown and both aggregate context features are unavailable and zero-filled with this indicator set to 0; missing lots never count as measured zeros.'}}

def validate_schema(schema):
    if schema != operation_schema():
        raise ValueError('Checkpoint operation feature schema differs from this feature builder; retraining required')
    return schema['count']

def save_preprocessing(checkpoint, schema, breakdown_scale, delay_pos_weight):
    import math
    validate_schema(schema)
    if breakdown_scale is None or not math.isfinite(float(breakdown_scale)) or breakdown_scale <= 0:
        raise ValueError('Missing or invalid training breakdown scale')
    path = Path(checkpoint)
    if delay_pos_weight is None or not math.isfinite(float(delay_pos_weight)) or delay_pos_weight <= 0:
        raise ValueError('Missing or invalid training delay pos_weight')
    payload = {'version': 2, 'operation_schema': schema, 'breakdown_scale': float(breakdown_scale),
               'delay_pos_weight': float(delay_pos_weight),
               'checkpoint_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    artifact = Path(str(path) + '.preprocessing.json')
    tmp = artifact.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    tmp.replace(artifact)

class PreprocessingArtifactError(RuntimeError):
    """The checkpoint's preprocessing artifact is missing, unreadable or foreign."""


def load_preprocessing(checkpoint):
    import math
    path = Path(checkpoint)
    artifact = Path(str(path) + '.preprocessing.json')
    # A missing artifact used to escape as a bare pathlib FileNotFoundError
    # from read_text(), which says nothing about WHY a checkpoint is
    # unusable. Every other failure in this file is an explicit ValueError;
    # this one now matches, so "checkpoint has no preprocessing" reads as
    # the deliberate refusal it is rather than an incidental IO error.
    if not artifact.exists():
        raise PreprocessingArtifactError(
            f"No preprocessing artifact for checkpoint {path}: expected {artifact}. "
            f"Inference must use the feature schema and normalisation scales the "
            f"checkpoint was trained with -- without them the model would silently "
            f"receive differently-shaped or differently-scaled inputs. Re-run "
            f"models/train.py to regenerate both together."
        )
    try:
        payload = json.loads(artifact.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreprocessingArtifactError(
            f"Preprocessing artifact {artifact} is unreadable: {exc}"
        ) from exc
    validate_schema(payload['operation_schema'])
    scale = payload.get('breakdown_scale')
    pos_weight = payload.get('delay_pos_weight')
    if (payload.get('version') != 2 or scale is None
            or not math.isfinite(float(scale)) or scale <= 0
            or pos_weight is None or not math.isfinite(float(pos_weight))
            or pos_weight <= 0):
        raise ValueError('Missing or invalid checkpoint preprocessing scale/version')
    if hashlib.sha256(path.read_bytes()).hexdigest() != payload.get('checkpoint_sha256'):
        raise ValueError('Preprocessing artifact does not belong to this checkpoint')
    return payload
