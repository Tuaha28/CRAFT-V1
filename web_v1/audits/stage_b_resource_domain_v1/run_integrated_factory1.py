"""Pinned 25-operation integrated adaptive benchmark; output is simulation-only.

Writes only new Stage-B audit artifacts. It never opens the legacy database or
updates an application schedule. The governed TEST approval path is exercised
separately by Django tests.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import pymysql
pymysql.connect = lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError('LEGACY_DB_FORBIDDEN'))

from web_v1.audits.factory1_realistic_neighborhood_v1.analyze_scope import SOURCE, SNAPSHOT, SCENARIO, SOURCE_SHA
from web_v1.audits.factory1_realistic_neighborhood_v1.run_bounded import next_earliest_order_keys, run
from web_v1.audits.factory1_realistic_neighborhood_v1.validate_bounded import validate_payload
from web_v1.audits.global_resource_state_stage_a.run_factory1 import build_snapshot
from web_v1.backend.integrations.craft.global_resource_state import GlobalStateError, validate_candidate

HERE = Path(__file__).resolve().parent


def main():
    scale35 = '--scale-35' in sys.argv[1:]
    source_bytes = SOURCE.read_bytes()
    if sha256(source_bytes).hexdigest() != SOURCE_SHA:
        raise RuntimeError('PINNED_SOURCE_CHANGED')
    source = json.loads(source_bytes)
    normalized = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
    scenario = json.loads(SCENARIO.read_text(encoding='utf-8'))
    keys = next_earliest_order_keys()
    if scale35:
        by_order = defaultdict(list)
        for row in normalized['operations']:
            by_order[row['order_id']].append(row)
        released = sorted((row for row in source['orders']
                           if row['scheduled_start_date'] <= source['reference_time'][:10]
                           and any(op['execution_state'] != 'FULLY_COMPLETED'
                                   for op in by_order[str(row['production_order_id'])])),
                          key=lambda row: (row['delivery_deadline'], row['production_order_id']))
        if [row['production_order_id'] for row in released[:4]] != [790, 33, 469, 979]:
            raise RuntimeError('PINNED_NEXT_RELEASED_ROUTE_CHANGED')
        fourth = sorted(by_order['979'], key=lambda row: row['routing_sequence'])
        if len(fourth) != 10 or len(keys) != 25:
            raise RuntimeError('PINNED_SCALE_ROUTE_CHANGED')
        keys = keys + tuple(row['operation_key'] for row in fourth)
    state = build_snapshot(source, normalized, scenario, keys)
    output = run(keys, domain_width=4, global_resource_state=state,
                 resource_policy='stage_b_adaptive')
    path = HERE / ('factory1_35_integrated_adaptive_v4.json' if scale35 else
                   'factory1_25_integrated_adaptive_v3.json')
    if path.exists():
        raise RuntimeError('INTEGRATED_ARTIFACT_ALREADY_EXISTS')
    checkpoint = path.with_name(path.stem + '_raw_checkpoint.json')
    if checkpoint.exists():
        raise RuntimeError('INTEGRATED_CHECKPOINT_ALREADY_EXISTS')
    checkpoint.write_text(json.dumps(output, indent=2, sort_keys=True, default=str) + '\n',
                          encoding='utf-8')
    independent = validate_payload(source, normalized, scenario, output, source_bytes)
    rows = [row for window in output['windows'] for row in window['selected_schedule']]
    try:
        global_check = validate_candidate(state, rows)
    except GlobalStateError as exc:
        global_check = {'status': 'FAIL', 'reason': str(exc)}
    complete = (
        len(output['accepted_keys']) == len(keys) and
        len(output['windows']) == (len(keys) + 3) // 4 and
        all(window['status'] == 'ACCEPTED' and window['replay'] == 'PASS' and
            window['physical'] == 'VALID' and window['scope'] == 'SCOPE_VALID' and
            window['calendar_equivalence_mismatches'] == 0
            for window in output['windows']) and
        independent['status'] == 'PASS' and global_check['status'] == 'PASS')
    output['integrated_validation'] = {
        'status': 'PASS' if complete else 'FAIL',
        'independent_assembled': independent,
        'global_conflict': global_check,
        'fixed_simulated_holds': len(state['fixed_commitments']),
        'approved_outside_assignments': len(state['approved_outside']),
        'source_hash_verified': True,
        'first_unaccepted_window': next((window['number'] for window in output['windows']
                                         if window['status'] != 'ACCEPTED'), None),
    }
    path.write_text(json.dumps(output, indent=2, sort_keys=True, default=str) + '\n', encoding='utf-8')
    print(json.dumps({'status': output['integrated_validation']['status'],
                      'accepted': len(output['accepted_keys']),
                      'windows': len(output['windows']),
                      'first_unaccepted_window': output['integrated_validation']['first_unaccepted_window'],
                      'remaining': len(output['remaining_keys']),
                      'wall_seconds': output['total_wall_seconds'],
                      'peak_rss_mb': output['sampled_peak_rss_mb'],
                      'path': str(path)}))
    return 0 if complete else 2


if __name__ == '__main__':
    raise SystemExit(main())
