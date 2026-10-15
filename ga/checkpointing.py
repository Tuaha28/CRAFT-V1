"""Atomic, fail-closed persistence for rolling GA window boundaries."""
from __future__ import annotations
import base64, hashlib, json, os, pickle, random, tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
import numpy as np
SCHEMA_VERSION = 1
class CheckpointError(RuntimeError): pass
def _norm(v: Any) -> Any:
    if isinstance(v, Mapping): return {str(k): _norm(x) for k,x in v.items()}
    if isinstance(v, (list, tuple)): return [_norm(x) for x in v]
    if hasattr(v, 'isoformat'): return v.isoformat()
    if isinstance(v, np.generic): return v.item()
    return v
def digest(v: Any) -> str: return hashlib.sha256(json.dumps(_norm(v), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
def _atomic(path: Path, raw: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True); fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f: f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    return hashlib.sha256(raw).hexdigest()
def save(directory: str|Path,name: str,payload: Mapping[str,Any]) -> Path:
    required={'identity','rolling_state','ga_state','resource_state','phase'}; missing=required-set(payload)
    if missing: raise CheckpointError(f'missing fields: {sorted(missing)}')
    d=dict(payload); d['schema_version']=SCHEMA_VERSION; d['created_at']=datetime.now(timezone.utc).isoformat()
    d['rng_state']=encode_rng_state()
    d['integrity']={'chromosome':digest(d['ga_state'].get('selected_chromosome',[])),'phenotype':digest(d['ga_state'].get('phenotype',[])),'resources':digest(d['resource_state'])}
    d['checkpoint_digest']=digest({k:v for k,v in d.items() if k!='checkpoint_digest'}); raw=json.dumps(_norm(d),sort_keys=True,indent=2).encode(); path=Path(directory)/(name+'.json'); sha=_atomic(path,raw)
    _atomic(path.with_suffix('.json.sha256'),(sha+'\n').encode()); _atomic(Path(directory)/'LATEST_VALID_CHECKPOINT.json',json.dumps({'path':str(path),'sha256':sha}).encode()); return path
def load(path: str|Path,expected: Mapping[str,Any]) -> dict:
    path=Path(path)
    try: raw=path.read_bytes(); d=json.loads(raw)
    except Exception as e: raise CheckpointError(f'unreadable checkpoint: {path}') from e
    side=path.with_suffix('.json.sha256')
    if not side.exists() or side.read_text().strip()!=hashlib.sha256(raw).hexdigest(): raise CheckpointError('checkpoint SHA mismatch')
    if d.get('schema_version')!=SCHEMA_VERSION: raise CheckpointError('unsupported schema')
    if d.get('checkpoint_digest')!=digest({k:v for k,v in d.items() if k!='checkpoint_digest'}): raise CheckpointError('internal checksum mismatch')
    for k,v in expected.items():
        if d['identity'].get(k)!=v: raise CheckpointError(f'identity mismatch: {k}')
    return d


def encode_rng_state() -> dict:
    return {"python_pickle_b64": base64.b64encode(pickle.dumps(random.getstate(), protocol=4)).decode(),
            "numpy_pickle_b64": base64.b64encode(pickle.dumps(np.random.get_state(), protocol=4)).decode()}

def restore_rng_state(state: Mapping[str, str]) -> None:
    try:
        random.setstate(pickle.loads(base64.b64decode(state["python_pickle_b64"])))
        np.random.set_state(pickle.loads(base64.b64decode(state["numpy_pickle_b64"])))
    except Exception as exc:
        raise CheckpointError("invalid RNG state") from exc

def encode_individual(ind) -> dict:
    return {"genes": list(ind), "fitness": list(ind.fitness.values)}

def decode_individual(record):
    from deap import creator
    if not isinstance(record, Mapping) or not isinstance(record.get("genes"), list) or not isinstance(record.get("fitness"), list):
        raise CheckpointError("invalid DEAP individual record")
    result = creator.Individual(record["genes"]); result.fitness.values = tuple(record["fitness"]); return result

def encode_evolution(population, hall_of_fame, selected, logbook, generation, evaluations) -> dict:
    return {"population": [encode_individual(x) for x in population], "hall_of_fame": [encode_individual(x) for x in hall_of_fame],
            "selected": encode_individual(selected), "generation": int(generation), "evaluations": int(evaluations),
            "logbook": [dict(x) for x in logbook]}

def decode_evolution(state):
    required={"population","hall_of_fame","selected","generation","evaluations","logbook"}
    if not required.issubset(state): raise CheckpointError("incomplete evolution state")
    return {"population":[decode_individual(x) for x in state["population"]], "hall_of_fame":[decode_individual(x) for x in state["hall_of_fame"]],
            "selected":decode_individual(state["selected"]), "generation":int(state["generation"]), "evaluations":int(state["evaluations"]), "logbook":list(state["logbook"])}
