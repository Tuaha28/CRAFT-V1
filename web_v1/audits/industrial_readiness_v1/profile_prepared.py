"""Profile trusted frozen Window-1 preparation offline; no solve or DB access."""
import cProfile
import hashlib
import json
import os
import pickle
import pstats
import sys
import threading
import time
from pathlib import Path
from dataclasses import asdict
import psutil
ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
checkpoint = ROOT / ('outputs/crafter/first_unknown_cp_checkpoint_v2/checkpoint_000_stage_1.prepared.pkl'
                     if 'factory2' in sys.argv[2:] else
                     'outputs/crafter/factory1_cp_parallelism_audit_v1/checkpoint/checkpoint_000_stage_1.prepared.pkl')
profile_enabled = '--no-profile' not in sys.argv
protected = json.loads((OUT / 'protected_before.json').read_text(encoding='utf-8'))
content = checkpoint.read_bytes()
assert hashlib.sha256(content).hexdigest() == protected[checkpoint.relative_to(ROOT).as_posix()]
# This is a repository-owned, hash-verified pickle, never a client-supplied input.
report = {'source': checkpoint.relative_to(ROOT).as_posix(), 'source_hash': hashlib.sha256(content).hexdigest(),
          'stages': [], 'database_writes': 0, 'database_reads': 0, 'solve_executed': False,
          'live_admission': False, 'profiler_enabled': profile_enabled, 'status': 'STARTED'}
process = psutil.Process()
started = time.perf_counter()
peak = [process.memory_info().rss]
stop = threading.Event()


def save():
    report['peak_rss_mb'] = peak[0]/1024**2
    (OUT / (sys.argv[1] if len(sys.argv) > 1 else 'factory1_prepared_profile.json')).write_text(json.dumps(report, indent=2), encoding='utf-8')


def watchdog():
    while not stop.wait(.1):
        peak[0] = max(peak[0], process.memory_info().rss)
        if peak[0] > 1536*1024**2 or time.perf_counter()-started > 300:
            report['status'] = 'COMPUTATIONAL_LIMIT'
            save()
            os._exit(3)


def stage(name, function):
    profiler = cProfile.Profile() if profile_enabled else None
    mark = time.perf_counter()
    cpu = time.process_time()
    if profiler is not None:
        profiler.enable()
    result = function()
    if profiler is not None:
        profiler.disable()
    stats = pstats.Stats(profiler) if profiler is not None else None
    report['stages'].append({'name': name, 'seconds': time.perf_counter()-mark,
        'cpu_seconds': time.process_time()-cpu, 'rss_mb': process.memory_info().rss/1024**2,
        'top_functions': sorted([{'function': f'{k[0]}:{k[1]}:{k[2]}', 'calls':v[1], 'cumulative_seconds':v[3]}
            for k,v in stats.stats.items()], key=lambda x:-x['cumulative_seconds'])[:12] if stats is not None else []})
    save()
    print(name, report['stages'][-1]['seconds'], flush=True)
    return result


if __name__ == '__main__':
    threading.Thread(target=watchdog, daemon=True).start()
    # Connection guard applies before imports from the trusted checkpoint.
    import pymysql
    def no_database(*args, **kwargs):
        raise RuntimeError('Offline profile forbids database connections')
    pymysql.connect = no_database
    prepared = stage('checkpoint_deserialize_and_imports', lambda: pickle.loads(content))
    from crafter.repair.resource_configs import generate_resource_configurations
    from crafter.repair.calendar_regimes import generate_calendar_regimes, CalendarRegimeCache, IncrementalCalendarRegimeBuilder
    from crafter.repair.cp_repair import build_repair_model
    state = prepared['state']
    report.update(global_state_operations=len(state['problem'].operations), neighborhood_operations=len(state['keys']),
                  machines=len(state['problem'].machines))
    orders = state['problem'].order_by_id
    from collections import Counter
    report['factory_operation_counts'] = dict(Counter(orders[op.order_id].factory_id for op in state['problem'].operations))
    configs, audit, summary = stage('resource_configurations', lambda: generate_resource_configurations(
        state, max_machine_sets=1, max_worker_crews=3, machine_pool_limit=30))
    cache = CalendarRegimeCache()
    regimes, regime_summary, count, equivalence = stage('calendar_regimes', lambda: generate_calendar_regimes(state, configs, cache=cache))
    report.update(configurations=sum(map(len, configs.values())), regimes=count, calendar_equivalence=equivalence)
    canonical = {key: [{**r, 'config': asdict(r['config']), 'base': asdict(r['base'])} for r in values]
                 for key, values in regimes.items()}
    report['regime_payload_sha256'] = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    incremental = IncrementalCalendarRegimeBuilder(state, cache=cache)
    incremental.seed(configs, regimes, regime_summary, equivalence)
    reused, _, _, _ = stage('incremental_unchanged_domain', lambda: incremental.build(configs))
    assert {k: tuple(v) for k,v in reused.items()} == {k: tuple(v) for k,v in regimes.items()}
    model = stage('cp_model_construction', lambda: build_repair_model(state, regimes))
    report['model_validation'] = model['model'].Validate()
    report['status'] = 'PREPARATION_COMPLETE_NOT_SOLVED'
    stop.set()
    save()
