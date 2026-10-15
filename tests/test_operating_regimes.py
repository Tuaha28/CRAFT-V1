import random

import pytest

from data_generation.operating_regimes import BUSY, NOMINAL, STRESS, get_operating_regime
from data_generation.seed_database_fixed import sample_execution_variance_multiplier


def test_stress_profile_is_exact_legacy_phase_b_values():
    assert STRESS.execution_variance_mean == 1.0
    assert STRESS.execution_variance_sd == 0.15
    assert STRESS.execution_variance_min == 0.65
    assert STRESS.execution_variance_max == 1.6
    assert STRESS.dynamic_breakdown_probability == 0.15
    assert (STRESS.repair_min_minutes, STRESS.repair_max_minutes) == (120, 360)
    assert STRESS.breakdown_effect_cap == 0.08
    assert STRESS.breakdown_effect_per_count == 0.01
    assert STRESS.queue_sd_widen_cap == 0.5


def test_pressure_profiles_are_strictly_monotone_and_frozen():
    for field in ('execution_variance_sd', 'dynamic_breakdown_probability', 'repair_min_minutes', 'repair_max_minutes', 'queue_sd_widen_cap'):
        values = [getattr(regime, field) for regime in (NOMINAL, BUSY, STRESS)]
        assert values == sorted(values)
        assert len(set(values)) == 3
    assert get_operating_regime('nominal') is NOMINAL
    assert get_operating_regime('busy') is BUSY
    assert get_operating_regime('stress') is STRESS
    with pytest.raises(ValueError):
        get_operating_regime('invented')


def test_default_stress_variance_matches_explicit_stress_profile():
    kwargs = dict(machine_breakdown_count_at_dispatch=3, on_leave=False, queue_delay_mins=2500)
    a = sample_execution_variance_multiplier(random.Random(42), **kwargs)
    b = sample_execution_variance_multiplier(random.Random(42), regime=STRESS, **kwargs)
    assert a == b
