import hashlib
import json
from pathlib import Path

root = Path("outputs/crafter/resource_domain_v3/quality_validation_v1")
exclude = {"artifact_hash_manifest.json"}
files = {}
for path in sorted(root.rglob("*")):
    if not path.is_file() or path.name in exclude:
        continue
    files[str(path.relative_to(root)).replace("\\", "/")] = hashlib.sha256(path.read_bytes()).hexdigest()
(root / "artifact_hash_manifest.json").write_text(
    json.dumps({"algorithm": "SHA-256", "files": files, "hash_errors": 0}, indent=2) + "\n",
    encoding="utf-8")
print(json.dumps({"files": len(files), "hash_errors": 0}))
