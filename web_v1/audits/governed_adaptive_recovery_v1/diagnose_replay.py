"""Offline diagnostic using saved TEST assignments, not a new DB approval."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from web_v1.backend.integrations.craft.factory1_test_state import export_state, approved_boundary, _payload_hash
from web_v1.audits.factory1_realistic_neighborhood_v1 import run_bounded as runner


def main():
    first = json.loads(Path(sys.argv[1]).read_text())['result']
    version = SimpleNamespace(pk='OFFLINE_REPLAY_OF_SAVED_TEST_ASSIGNMENTS', status='ACTIVE',
                              operations=first['acceptance']['selected_schedule'], evidence=first)
    version.payload_hash = _payload_hash(version)
    keys = runner.next_earliest_order_keys()[12:25]
    state = export_state(SimpleNamespace(active_version=version, resource_revision=0), keys)
    reports = []
    original = runner.run_adaptive_planning_schedule

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        acceptance = result['acceptance']
        reports.append({key: acceptance.get(key) for key in
                        ('status', 'replay', 'materialization', 'validator', 'selected_schedule')})
        return result

    runner.run_adaptive_planning_schedule = capture
    output = runner.run(keys, domain_width=4, resource_policy='stage_b_adaptive',
                        approved_boundary=approved_boundary(state), global_resource_state=state,
                        cycle_target=8, window_size=4, adaptive_retries=True, max_new_windows=2)
    packet = {'classification': 'OFFLINE_DIAGNOSTIC_NOT_DATABASE_APPROVAL',
              'result': output, 'acceptance_diagnostics': reports}
    with Path(sys.argv[2]).open('x', encoding='utf-8') as stream:
        json.dump(packet, stream, default=str, sort_keys=True)
    print(json.dumps([{'status': r['status'], 'replay': r['replay']} for r in reports], default=str))


if __name__ == '__main__':
    main()
