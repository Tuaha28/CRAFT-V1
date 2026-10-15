import json
from unittest.mock import patch

import pytest

from crafter.production.checkpoints import save_checkpoint, load_checkpoint


def test_roundtrip_immutable_and_stale(tmp_path):
    path = tmp_path / 'checkpoint.json'
    identity = {'factory': 4, 'source': 'pinned', 'policy': 'bounded'}
    save_checkpoint(path, identity=identity, payload={'accepted': ['a']})
    original = path.read_bytes()
    assert load_checkpoint(path, identity=identity) == {'accepted': ['a']}
    with pytest.raises(FileExistsError):
        save_checkpoint(path, identity=identity, payload={'accepted': ['b']})
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match='CHECKPOINT_STALE_IDENTITY'):
        load_checkpoint(path, identity={**identity, 'factory': 6})


def test_torn_corrupt_and_failed_publish_leave_previous_usable(tmp_path):
    path = tmp_path / 'good.json'
    save_checkpoint(path, identity={}, payload={'accepted': []})
    with patch('crafter.production.checkpoints.os.link', side_effect=OSError('crash')):
        with pytest.raises(OSError, match='crash'):
            save_checkpoint(tmp_path / 'next.json', identity={}, payload={})
    assert not (tmp_path / 'next.json').exists()
    assert load_checkpoint(path, identity={}) == {'accepted': []}
    assert not list(tmp_path.glob('.craft-checkpoint-*'))
    packet = json.loads(path.read_text())
    packet['payload']['accepted'] = ['forged']
    path.write_text(json.dumps(packet))
    with pytest.raises(ValueError, match='CHECKPOINT_INTEGRITY'):
        load_checkpoint(path, identity={})
