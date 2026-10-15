"""Controlled reservation-signature overhead benchmark; not factory throughput."""
import json
import sys
import time
from pathlib import Path
from dataclasses import replace
from datetime import timedelta
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from web_v1.backend.integrations.craft.tests.test_residual_execution import fixture
from crafter.repair.resource_configs import ResourceConfiguration
from crafter.repair.calendar_regimes import _execution

if __name__ == '__main__':
    p, _, index = fixture()
    config = ResourceConfiguration('1_2', 1, 1, 1, (1,), ('1',), 0, 600, (), 10)
    state = {'problem': p, 'operator_index': index, '_worker_res': {
        str(i): [(p.origin + timedelta(seconds=1000), p.origin + timedelta(seconds=1200))] for i in range(2, 9002)}}
    timings = []
    result = None
    for trial in range(5):
        start = time.perf_counter()
        for _ in range(20):
            result = _execution(state, config, 0)
        timings.append(time.perf_counter()-start)
    payload = {'fixture': '9000 unrelated worker reservations, 20 uncached executions, 5 trials',
               'seconds': timings, 'execution': result, 'is_factory_benchmark': False}
    target = Path(__file__).with_name(sys.argv[1])
    target.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    print(json.dumps(payload))
