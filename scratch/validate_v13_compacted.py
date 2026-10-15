import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import datetime
from dataclasses import replace
from crafter.disruptions.p2_core_guided_prototype import _prepare_state
from crafter.disruptions.p2_final_repair_attempt import _config_key, _validate_selected
from crafter.lns_probe.run_e1_7_final_verified import _phenotype
from crafter.phenotype import validate

data = json.loads(open('outputs/crafter/p2_final_repair_attempt_v13/materialized_schedule.json', encoding='utf-8').read())
state = _prepare_state()
selected = {}
for key, row in data['selected'].items():
    base = next(m for m in state['modes'][key]
                if int(m.line_id) == int(row['line'])
                and int(m.machine_count) == int(row['k'])
                and tuple(map(int, m.machine_ids)) == tuple(map(int, row['machines']))
                and tuple(map(str, m.worker_ids)) == tuple(map(str, row['workers'])))
    segs = []
    for a, b in row['worker_segments']:
        aa, bb = datetime.fromisoformat(a), datetime.fromisoformat(b)
        if bb > aa:
            segs.append((aa, bb))
    start = int(row['start'])
    end = int(round((segs[-1][1] - state['problem'].origin).total_seconds())) if segs else start + int(base.setup_seconds + base.productive_seconds)
    prod = sum(int(round((b-a).total_seconds())) for a,b in segs)
    selected[key] = replace(base, mode_id=f"{base.mode_id}|COMPACTED",
        start_seconds=start, productive_start_seconds=int(round((segs[0][0]-state['problem'].origin).total_seconds())) if segs else start+base.setup_seconds,
        end_seconds=end, elapsed_seconds=end-start, productive_seconds=prod,
        pause_seconds=max(0, end-start-base.setup_seconds-prod), worker_segments=tuple(segs),
        machine_occupancy_start_seconds=start, machine_occupancy_end_seconds=end,
        incumbent=False, provenance=tuple(base.provenance)+('COMPACTED_DIRECT_TIME_DIAGNOSTIC',),
        recovery_level='BOUNDED_CRAFT_REPAIR', recovery_reason='P2_DIRECT_TIME_COMPACTED',
        source_event_id=state['event'].event_id)
replay, validator = _validate_selected(state, selected)
# Rebind each operation to the authoritative replay walk, preserving only the
# CP-selected physical configuration/start. This is a diagnostic materializer;
# it never invents workers or machines.
for key, mode in list(selected.items()):
    end, segs, _ = __import__('crafter.lns_probe.run_e1_7_final_verified', fromlist=['_replay_mode'])._replay_mode(
        state['problem'], state['operator_index'], state['after_index'], state['neighborhood'], mode)
    if segs:
        selected[key] = replace(mode, end_seconds=int(end), elapsed_seconds=int(end-mode.start_seconds),
            productive_start_seconds=int(round((segs[0][0]-state['problem'].origin).total_seconds())),
            pause_seconds=max(0, int(end-round((segs[0][0]-state['problem'].origin).total_seconds()))-int(mode.productive_seconds)),
            worker_segments=tuple(segs), machine_occupancy_end_seconds=int(end),
            mode_id=f"{mode.mode_id}|AUTHORITATIVE_REPLAY")
replay2, validator2 = _validate_selected(state, selected)
print(json.dumps({'operations': len(selected), 'initial_replay': replay,
                  'initial_validator': validator, 'authoritative_materialization_replay': replay2,
                  'authoritative_materialization_validator': validator2,
                  'validator_report': getattr(validate(state['problem'], _phenotype(state['problem'], state['baseline'], selected), state['operator_index']), 'validation_report', None)}, indent=2, default=str))
