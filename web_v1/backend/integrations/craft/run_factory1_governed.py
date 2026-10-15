"""Run pinned Factory 1 simulation through CRAFT for a governed TEST request.

This subprocess is deliberately separate from Django. It never connects to
the legacy database and it never persists or promotes a schedule.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
import pymysql
pymysql.connect = lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError('LEGACY_DB_FORBIDDEN'))

from web_v1.backend.integrations.craft.factory1_test_state import approved_boundary, pinned_base
from web_v1.backend.integrations.craft.global_resource_state import verify, validate_candidate
from web_v1.audits.factory1_realistic_neighborhood_v1.analyze_scope import SOURCE, SNAPSHOT, SCENARIO
from web_v1.audits.factory1_realistic_neighborhood_v1.run_bounded import next_earliest_order_keys, run
from web_v1.audits.factory1_realistic_neighborhood_v1.validate_bounded import validate_payload


def solve(snapshot):
    if (snapshot.get('contract') != 'FACTORY1_GOVERNED_BOUNDED_V1' or
            snapshot.get('source') != 'PINNED_FACTORY1_SIMULATION_NOT_OBSERVED'):
        raise ValueError('FACTORY1_GOVERNED_CONTRACT')
    state = snapshot['global_resource_state']
    verify(state)
    base = pinned_base()
    if (state['source_hash'] != base['source_hash'] or
            state['scenario_hash'] != base['scenario_hash'] or
            snapshot['source_fingerprint'] != base['source_hash'] or
            snapshot['scenario_hash'] != base['scenario_hash']):
        raise ValueError('FACTORY1_GOVERNED_PROVENANCE')
    keys = tuple(snapshot['selected_keys'])
    full = next_earliest_order_keys()
    first = full[:12]
    if keys not in (first, full[12:]):
        raise ValueError('FACTORY1_GOVERNED_SELECTION_UNSUPPORTED')
    if set(keys) != set(state['selected_keys']):
        raise ValueError('FACTORY1_GOVERNED_SELECTION_MISMATCH')
    resource_policy = snapshot.get('resource_policy', 'incumbent_prefix')
    if resource_policy not in ('incumbent_prefix', 'stage_b_diverse', 'stage_b_adaptive'):
        raise ValueError('FACTORY1_GOVERNED_RESOURCE_POLICY_UNSUPPORTED')
    boundary = approved_boundary(state)
    output = run(keys, domain_width=4,
                 approved_boundary=boundary, global_resource_state=state,
                 resource_policy=resource_policy)
    if len(output['accepted_keys']) != len(keys):
        return {'source': 'PINNED_FACTORY1_SIMULATION_NOT_OBSERVED',
                'controller_status': 'COMPUTATIONAL_LIMIT',
                'physical_infeasibility_proven': False,
                'acceptance': {'status': 'REJECTED', 'selected_schedule': []},
                'reason': 'FACTORY1_BOUNDED_WINDOW_NOT_ACCEPTED',
                'accepted_keys': output['accepted_keys'],
                'remaining_keys': output['remaining_keys'],
                'windows': [{name: window.get(name) for name in (
                    'number', 'keys', 'status', 'replay', 'physical', 'scope',
                    'calendar_regimes', 'stages', 'regime_bounds',
                    'resource_domain_v2', 'resource_configurations')}
                    for window in output['windows']],
                'runtime_seconds': output['total_wall_seconds']}
    source_bytes = SOURCE.read_bytes()
    independent = validate_payload(json.loads(source_bytes),
        json.loads(SNAPSHOT.read_text(encoding='utf-8')),
        json.loads(SCENARIO.read_text(encoding='utf-8')),
        output, source_bytes, boundary)
    rows = [row for window in output['windows'] for row in window['selected_schedule']]
    global_report = validate_candidate(state, rows)
    windows = [{name: window[name] for name in (
        'number', 'keys', 'status', 'replay', 'physical', 'scope',
        'materialization_mismatch_count', 'calendar_seconds',
        'solve_and_validation_seconds', 'calendar_regimes',
        'resource_domain_v2', 'resource_configurations')}
        for window in output['windows']]
    accepted = (len(output['accepted_keys']) == len(keys) and not output['remaining_keys']
                and independent['status'] == 'PASS' and global_report['status'] == 'PASS'
                and all(w['status'] == 'ACCEPTED' and w['replay'] == 'PASS'
                        and w['physical'] == 'VALID' and w['scope'] == 'SCOPE_VALID'
                        and w['materialization_mismatch_count'] == 0 for w in windows))
    return {'source': 'PINNED_FACTORY1_SIMULATION_NOT_OBSERVED',
            'controller_status': 'ACCEPTED' if accepted else 'REJECTED',
            'acceptance': {'status': 'ACCEPTED' if accepted else 'REJECTED',
                           'selected_schedule': rows,
                           'winning_stage': {'status': output['windows'][-1]['stages'][-1]['status']
                                            if windows else 'NOT_REACHED'}},
            'quantity': {'status': 'PASS' if independent['status'] == 'PASS' else 'FAIL'},
            'multi_window': {'operations_selected': len(keys),
                             'windows_accepted': sum(w['status'] == 'ACCEPTED' for w in windows),
                             'windows': windows, 'independent_assembled': independent},
            'global_conflict_validation': global_report,
            'source_sha256': output['source_sha256'],
            'scenario_hash': output['scenario_hash'],
            'scheduling_input_hash': output['scheduling_input_hash'],
            'global_state_fingerprint': state['fingerprint'],
            'domain': output['domain'],
            'runtime_seconds': output['total_wall_seconds'],
            'sampled_peak_rss_mb': output['sampled_peak_rss_mb']}


def main():
    if len(sys.argv) not in (3, 4):
        raise SystemExit('Expected output and request snapshot paths')
    start = time.perf_counter()
    snapshot = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
    if snapshot.get('cycle_target'):
        if len(sys.argv) != 4:
            raise SystemExit('CYCLE_CHECKPOINT_PATH_REQUIRED')
        from web_v1.backend.integrations.craft.factory1_cycles import advance
        checkpoint = json.loads(Path(sys.argv[3]).read_text(encoding='utf-8'))
        result = advance(snapshot, checkpoint)
    else:
        if len(sys.argv) != 3:
            raise SystemExit('UNEXPECTED_CYCLE_CHECKPOINT')
        result = solve(snapshot)
    result['worker_total_seconds'] = time.perf_counter() - start
    Path(sys.argv[1]).write_text(json.dumps(result, sort_keys=True, default=str), encoding='utf-8')


if __name__ == '__main__':
    main()
