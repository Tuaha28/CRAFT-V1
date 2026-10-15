"""Checkpointed TEST-only Factory 1 cycles over the existing bounded solver.

One invocation advances at most one window. The caller owns durable storage;
the returned checkpoint is tentative and cannot be promoted as a schedule.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from crafter.production.cycles import new_checkpoint, record_window, verify_checkpoint

from web_v1.backend.integrations.craft.factory1_test_state import approved_boundary, pinned_base
from web_v1.backend.integrations.craft.global_resource_state import seal, validate_candidate, verify
from web_v1.audits.factory1_realistic_neighborhood_v1.analyze_scope import SOURCE, SNAPSHOT, SCENARIO
from web_v1.audits.factory1_realistic_neighborhood_v1.run_bounded import next_earliest_order_keys, run
from web_v1.audits.factory1_realistic_neighborhood_v1.validate_bounded import validate_payload


def _accepted_view(output):
    """Validate a planning prefix using the independent source validator.

    This view is not a candidate. It limits operation accounting to work whose
    windows actually passed, retaining the pinned source and scenario hashes.
    """
    view = deepcopy(output)
    keys = [key for window in output['windows'] if window['status'] == 'ACCEPTED'
            for key in window['keys']]
    if len(keys) != len(set(keys)):
        raise ValueError('CYCLE_DUPLICATE_OPERATION')
    view['windows'] = [w for w in view['windows'] if w['status'] == 'ACCEPTED']
    view['selected_keys'] = keys
    view['accepted_keys'] = sorted(keys)
    view['remaining_keys'] = []
    view['ledger'] = {k: output['ledger'][k] for k in keys}
    source_input = deepcopy(view['scheduling_input'])
    source_input['keys'] = keys
    source_input['ledger'] = view['ledger']
    source_input['configs'] = {k: source_input['configs'][k] for k in keys}
    view['scheduling_input'] = source_input
    view['scheduling_input_hash'] = sha256(json.dumps(
        source_input, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()
    return view


def _selected_global_state(state, keys):
    packet = deepcopy({k: v for k, v in state.items() if k != 'fingerprint'})
    selected = set(keys)
    packet['selected_keys'] = sorted(selected)
    for key in state['selected_keys']:
        if key not in selected:
            packet['schedule_disposition'][key] = 'DEFERRED'
            packet['disposition_reasons'][key] = 'OUTSIDE_VALIDATED_CYCLE_PREFIX'
    return seal(packet)


def advance(snapshot, checkpoint=None):
    if (snapshot.get('contract') != 'FACTORY1_GOVERNED_BOUNDED_V1' or
            snapshot.get('source') != 'PINNED_FACTORY1_SIMULATION_NOT_OBSERVED' or
            snapshot.get('resource_policy') != 'stage_b_adaptive'):
        raise ValueError('CYCLE_FACTORY1_CONTRACT')
    state = snapshot['global_resource_state']
    verify(state)
    base = pinned_base()
    if (state['source_hash'] != base['source_hash'] or
            state['scenario_hash'] != base['scenario_hash'] or
            snapshot['source_fingerprint'] != base['source_hash'] or
            snapshot['scenario_hash'] != base['scenario_hash']):
        raise ValueError('CYCLE_SOURCE_PROVENANCE')
    keys = tuple(snapshot['selected_keys'])
    ranked = next_earliest_order_keys()
    if (not keys or len(set(keys)) != len(keys) or
            not set(keys) <= set(ranked) or
            keys != tuple(key for key in ranked if key in set(keys)) or
            set(keys) != set(state['selected_keys'])):
        raise ValueError('CYCLE_SELECTION_OUTSIDE_ADMITTED_DOMAIN')
    options = {'cycle_target': snapshot['cycle_target'],
               'window_size': snapshot['window_size']}
    adaptive_retries = snapshot.get('adaptive_retries', False)
    if adaptive_retries:
        options['adaptive_retries'] = True
    boundary = approved_boundary(state)
    output = run(keys, domain_width=4, approved_boundary=boundary,
                 global_resource_state=state, resource_policy='stage_b_adaptive',
                 cycle_target=options['cycle_target'], window_size=options['window_size'],
                 checkpoint=checkpoint, max_new_windows=1,
                 adaptive_retries=adaptive_retries)
    identity = output['scheduling_input_hash']
    if checkpoint is None:
        checkpoint = new_checkpoint(factory_id=1, input_fingerprint=identity,
                                    operation_keys=keys, options=options)
    else:
        verify_checkpoint(checkpoint, factory_id=1, input_fingerprint=identity,
                          operation_keys=keys, options=options)
    if len(output['windows']) != checkpoint['revision'] + 1:
        raise ValueError('CYCLE_NO_NEW_WINDOW')
    newest = output['windows'][-1]
    source_bytes = SOURCE.read_bytes()
    source = json.loads(source_bytes)
    normalized = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
    scenario = json.loads(SCENARIO.read_text(encoding='utf-8'))
    independent = {'status': 'NOT_REACHED'}
    global_check = {'status': 'NOT_REACHED'}
    if newest['status'] == 'ACCEPTED':
        prefix = _accepted_view(output)
        independent = validate_payload(source, normalized, scenario,
                                       prefix, source_bytes, boundary)
        if independent['status'] != 'PASS':
            raise ValueError('CYCLE_INDEPENDENT_PREFIX_INVALID:' + ','.join(independent['errors']))
        rows = [row for window in prefix['windows'] for row in window['selected_schedule']]
        global_check = validate_candidate(_selected_global_state(state, prefix['selected_keys']), rows)
        if global_check['status'] != 'PASS':
            raise ValueError('CYCLE_GLOBAL_PREFIX_INVALID')
    elif not newest.get('failure_reason'):
        newest = {**newest, 'failure_reason':
                  'RESOURCE_OR_TEMPORAL_WINDOW_LIMIT_NOT_PHYSICAL_INFEASIBILITY'}
    checkpoint = record_window(checkpoint, newest,
                               independent_status=independent['status'])
    verify_checkpoint(checkpoint, factory_id=1, input_fingerprint=identity,
                      operation_keys=keys, options=options)
    if ((newest['status'] != 'ACCEPTED' and not adaptive_retries)
            or checkpoint['status'] == 'PARTIAL'):
        # A rejected window has no accepted assignment to carry forward. The
        # bounded runner cannot resume past it, so leave the run terminal and
        # report the limit rather than issuing a checkpoint that loops forever.
        return {'source': 'PINNED_FACTORY1_SIMULATION_NOT_OBSERVED',
                'controller_status': 'COMPUTATIONAL_LIMIT',
                'physical_infeasibility_proven': False,
                'reason': newest.get('failure_reason') or 'EXPLICIT_DEFERRED_OPERATIONS_REMAIN',
                'failed_window': newest['number'],
                'checkpoint': checkpoint,
                'progress': {'accepted': len(checkpoint['accepted_keys']),
                             'remaining': len(keys) - len(checkpoint['accepted_keys']),
                             'deferred': checkpoint['deferred']},
                'existing_schedule_preserved': True,
                'runtime_seconds': output['total_wall_seconds'],
                'sampled_peak_rss_mb': output['sampled_peak_rss_mb']}
    if checkpoint['status'] != 'COMPLETE':
        return {'source': 'PINNED_FACTORY1_SIMULATION_NOT_OBSERVED',
                'controller_status': 'CHECKPOINTED', 'checkpoint': checkpoint,
                'progress': {'accepted': len(checkpoint['accepted_keys']),
                             'remaining': len(keys) - len(checkpoint['accepted_keys']),
                             'cycle_number': checkpoint['windows'][-1]['cycle_number'],
                             'window_number': checkpoint['windows'][-1]['number'],
                             'first_failure': checkpoint['deferred'] or None},
                'independent_prefix': independent, 'global_prefix': global_check,
                'restored_prefix_validation': output.get('restored_prefix_validation'),
                'runtime_seconds': output['total_wall_seconds'],
                'sampled_peak_rss_mb': output['sampled_peak_rss_mb']}
    final_view = _accepted_view(output) if adaptive_retries else output
    rows = [row for window in final_view['windows'] for row in window['selected_schedule']]
    final_independent = validate_payload(source, normalized, scenario,
                                         final_view, source_bytes, boundary)
    final_global = validate_candidate(state, rows)
    if (final_independent['status'] != 'PASS' or final_global['status'] != 'PASS'):
        raise ValueError('CYCLE_ASSEMBLED_VALIDATION_FAILED')
    windows = [{name: window.get(name) for name in (
        'number', 'keys', 'status', 'replay', 'physical', 'scope',
        'materialization_mismatch_count', 'resource_configurations',
        'calendar_regimes', 'resource_domain_v2', 'calendar_seconds',
        'solve_and_validation_seconds', 'objective_status', 'objective_stages')}
        for window in final_view['windows']]
    return {'source': 'PINNED_FACTORY1_SIMULATION_NOT_OBSERVED',
            'controller_status': 'ACCEPTED', 'checkpoint': checkpoint,
            'acceptance': {'status': 'ACCEPTED', 'selected_schedule': rows,
                           'winning_stage': {'status': output['windows'][-1]['stages'][-1]['status']}},
            'quantity': {'status': 'PASS'},
            'multi_window': {'operations_selected': len(keys),
                             'windows_attempted': len(output['windows']),
                             'rejected_attempts': sum(w['status'] != 'ACCEPTED' for w in output['windows']),
                             'windows_accepted': len(windows), 'windows': windows,
                             'independent_assembled': final_independent},
            'global_conflict_validation': final_global,
            'restored_prefix_validation': output.get('restored_prefix_validation'),
            'source_sha256': output['source_sha256'],
            'scenario_hash': output['scenario_hash'],
            'scheduling_input_hash': identity,
            'global_state_fingerprint': state['fingerprint'],
            'domain': output['domain'],
            'runtime_seconds': output['total_wall_seconds'],
            'sampled_peak_rss_mb': output['sampled_peak_rss_mb']}
