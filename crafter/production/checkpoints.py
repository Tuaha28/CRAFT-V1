"""Immutable JSON checkpoints for tentative CRAFT computation, never approval.

Checksums detect corruption, not hostile tampering. Callers must revalidate
restored assignments against the pinned physical state before using them.
Publishing uses a same-directory hard link: a crash cannot expose partial JSON
and an existing checkpoint is never overwritten. Database leases remain the
application's responsibility; this is not a multi-manager transaction service.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from crafter.production.cycles import seal


CONTRACT = 'CRAFT_TENTATIVE_CHECKPOINT_V1'


def save_checkpoint(path, *, identity, payload):
    path = Path(path)
    packet = seal({'contract': CONTRACT, 'approval_status': 'TENTATIVE_NOT_APPROVED',
                   'identity': identity, 'payload': payload})
    raw = json.dumps(packet, sort_keys=True, allow_nan=False, indent=2).encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.craft-checkpoint-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # atomic, fails if destination exists
    finally:
        os.unlink(temporary)
    return packet


def load_checkpoint(path, *, identity):
    try:
        packet = json.loads(Path(path).read_text(encoding='utf-8'))
        if packet != seal(packet):
            raise ValueError('CHECKPOINT_INTEGRITY')
        if (packet.get('contract') != CONTRACT or
                packet.get('approval_status') != 'TENTATIVE_NOT_APPROVED'):
            raise ValueError('CHECKPOINT_NOT_TENTATIVE')
        if packet.get('identity') != identity:
            raise ValueError('CHECKPOINT_STALE_IDENTITY')
        return packet['payload']
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        raise ValueError('CHECKPOINT_UNREADABLE') from exc
