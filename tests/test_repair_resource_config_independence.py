from dataclasses import fields
from pathlib import Path

from crafter.repair.resource_configs import ResourceConfiguration


def test_resource_configuration_has_no_timing_fields():
    names = {item.name for item in fields(ResourceConfiguration)}
    assert not names.intersection({'start', 'start_seconds', 'end', 'end_seconds',
                                   'worker_segments', 'pause_seconds'})


def test_production_repair_does_not_consume_joint_modes():
    root = Path(__file__).resolve().parents[1] / 'crafter' / 'repair'
    source = '\n'.join(path.read_text(encoding='utf-8') for path in root.glob('*.py'))
    assert 'generate_joint_modes' not in source
    assert 'configurations_from_modes' not in source
    assert "state['modes']" not in source
    assert 'state["modes"]' not in source
