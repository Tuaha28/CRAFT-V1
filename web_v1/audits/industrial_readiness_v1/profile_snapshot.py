"""Offline full Factory-1 execution normalization profile; not a scheduling run."""
import cProfile
import hashlib
import json
import pstats
import sys
import time
from collections import Counter
from pathlib import Path
import psutil
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from web_v1.backend.integrations.craft.snapshot_adapter import normalize_snapshot

if __name__ == '__main__':
    path = ROOT / 'web_v1/audits/execution_boundary_v1/factory1_source.json'
    manifest = json.loads(path.with_name('artifact_hash_manifest.json').read_text(encoding='utf-8'))
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if manifest['factory1_source.json'] != digest:
        raise RuntimeError('Captured source changed')
    t = time.perf_counter()
    source = json.loads(content)
    load_seconds = time.perf_counter()-t
    cpu = time.process_time()
    profile = cProfile.Profile()
    start = time.perf_counter()
    profile.enable()
    snapshot = normalize_snapshot(source)
    profile.disable()
    elapsed = time.perf_counter()-start
    stats = pstats.Stats(profile)
    rows = sorted([{'function': f'{key[0]}:{key[1]}:{key[2]}', 'calls': v[1], 'self_seconds': v[2],
                    'cumulative_seconds': v[3]} for key,v in stats.stats.items()], key=lambda x:-x['cumulative_seconds'])[:25]
    result = {'source_sha256': digest, 'database_reads': 0, 'database_writes': 0,
              'source': 'PRIOR_CAPTURE', 'load_seconds': load_seconds, 'normalization_seconds': elapsed,
              'cpu_seconds': time.process_time()-cpu, 'rss_mb': psutil.Process().memory_info().rss/1024**2,
              'operations': len(snapshot['operations']), 'states': dict(Counter(x['execution_state'] for x in snapshot['operations'])),
              'snapshot_hash': snapshot['snapshot_hash'], 'profile': rows,
              'not_measured': ['CRAFT global graph', 'resource generation', 'calendar generation', 'Factory-1 solver']}
    target = sys.argv[1] if len(sys.argv) > 1 else 'factory1_normalization_profile.json'
    Path(__file__).with_name(target).write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k != 'profile'}))
