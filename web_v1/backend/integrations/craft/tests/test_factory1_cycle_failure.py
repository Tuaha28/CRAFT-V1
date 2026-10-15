"""A failed bounded window must never become a resumable accepted checkpoint."""
from web_v1.backend.integrations.craft import factory1_cycles as cycles


def test_rejected_window_is_terminal_computational_limit(monkeypatch):
    state = {'source_hash': 'source', 'scenario_hash': 'scenario',
             'selected_keys': ['790_972'], 'fingerprint': 'state'}
    monkeypatch.setattr(cycles, 'verify', lambda _state: True)
    monkeypatch.setattr(cycles, 'pinned_base', lambda: {
        'source_hash': 'source', 'scenario_hash': 'scenario'})
    monkeypatch.setattr(cycles, 'approved_boundary', lambda _state: {})
    monkeypatch.setattr(cycles, 'next_earliest_order_keys', lambda: ('790_972',))
    monkeypatch.setattr(cycles, 'run', lambda *args, **kwargs: {
        'scheduling_input_hash': 'input',
        'windows': [{'number': 1, 'keys': ['790_972'], 'status': 'REJECTED',
                     'failure_reason': 'RESOURCE_DOMAIN_EXPANSION_LIMIT'}],
        'total_wall_seconds': 1.0, 'sampled_peak_rss_mb': 20.0})
    result = cycles.advance({
        'contract': 'FACTORY1_GOVERNED_BOUNDED_V1',
        'source': 'PINNED_FACTORY1_SIMULATION_NOT_OBSERVED',
        'resource_policy': 'stage_b_adaptive',
        'global_resource_state': state,
        'source_fingerprint': 'source', 'scenario_hash': 'scenario',
        'selected_keys': ['790_972'], 'cycle_target': 1, 'window_size': 1})
    assert result['controller_status'] == 'COMPUTATIONAL_LIMIT'
    assert result['physical_infeasibility_proven'] is False
    assert result['reason'] == 'RESOURCE_DOMAIN_EXPANSION_LIMIT'
    assert result['existing_schedule_preserved'] is True
    assert result['checkpoint']['accepted_keys'] == []
