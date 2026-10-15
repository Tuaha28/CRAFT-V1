"""Restoration must independently validate before a new CP model is solved."""
from copy import deepcopy

import pytest

from crafter.production.cycles import new_checkpoint, record_window, seal
from web_v1.audits.factory1_realistic_neighborhood_v1 import run_bounded as runner


@pytest.mark.parametrize('adaptive_retries', [False, True])
def test_resealed_invalid_assignment_is_rejected_before_next_solve(monkeypatch, adaptive_retries):
    keys = runner.KEYS_8
    options = {'cycle_target': 4, 'window_size': 2}
    if adaptive_retries:
        options['adaptive_retries'] = True
    result = runner.run(keys, domain_width=4, resource_policy='stage_b_adaptive',
                        max_new_windows=1, **options)
    assert result['windows'][0]['status'] == 'ACCEPTED'
    cp = new_checkpoint(factory_id=1, input_fingerprint=result['scheduling_input_hash'],
                        operation_keys=keys, options=options)
    cp = record_window(cp, result['windows'][0], independent_status='PASS')
    corrupted = deepcopy(cp)
    row = corrupted['windows'][0]['selected_schedule'][0]
    row['end_seconds'] = row['start_seconds'] + 1
    corrupted = seal(corrupted)  # checksum alone must not authorize acceptance

    def forbidden(*args, **kwargs):
        pytest.fail('CP solve reached before invalid checkpoint was rejected')
    monkeypatch.setattr(runner, 'run_adaptive_planning_schedule', forbidden)
    with pytest.raises(ValueError, match='CYCLE_CHECKPOINT_PHYSICAL_INVALID'):
        runner.run(keys, domain_width=4, resource_policy='stage_b_adaptive',
                   checkpoint=corrupted, max_new_windows=1, **options)
