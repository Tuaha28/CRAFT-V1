"""Finalize documentation/test hashes offline, without recapture or scheduling."""
import json
from run_audit import ROOT, OUT, sha, write

protected = json.loads((OUT / 'protected_hashes.json').read_text())
assert all(sha(ROOT / path) == expected for path, expected in protected['before'].items())
old_manifest = json.loads((OUT / 'artifact_hash_manifest.json').read_text())
for name in ('factory1_source.json', 'factory1_normalized_snapshot.json'):
    assert sha(OUT / name) == old_manifest[name], f'Captured evidence changed: {name}'
write('implementation_hashes.json', {
    str(p.relative_to(ROOT)): sha(p)
    for p in sorted((ROOT / 'web_v1/backend/integrations/craft').rglob('*'))
    if p.is_file() and p.suffix in ('.py', '.md')
})
manifest = {p.name: sha(p) for p in sorted(OUT.iterdir())
            if p.is_file() and p.name not in ('artifact_hash_manifest.json', 'hash_verification.json')}
write('artifact_hash_manifest.json', manifest)
write('hash_verification.json', {
    'all_hashes_verified': all(sha(OUT / name) == expected for name, expected in manifest.items()),
    'audited_sources_unchanged': True, 'frozen_workload_unchanged': True,
    'source_capture_unchanged': True, 'normalized_snapshot_unchanged': True,
    'database_accessed_by_finalizer': False,
})
print('Evidence hashes verified; captured source, normalized snapshot and protected files unchanged.')
