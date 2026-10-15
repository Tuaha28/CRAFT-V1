"""Read-only bounded verification of source and key benchmark fingerprints.

The historical manifest has 7,640 paths, mostly large output artifacts. This
check deliberately reports its subset instead of claiming a complete scan.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BEFORE = ROOT / "web_v1/audits/industrial_readiness_v1/protected_before.json"
CORE_PREFIXES = ("crafter/", "ga/", "models/", "graph/", "agent/")
F2_FILENAMES = {"run_manifest.json", "rolling_progress.json", "factory_accounting.json",
                "final_replay.json", "physical_validation.json", "scope_validation.json"}


def check():
    expected = json.loads(BEFORE.read_text(encoding="utf-8"))
    selected = [key for key in expected if key.startswith(CORE_PREFIXES)]
    selected += [key for key in expected if key.startswith("outputs/crafter/factory2_172")
                 and key.rsplit("/", 1)[-1] in F2_FILENAMES]
    changed, missing = [], []
    for key in selected:
        path = ROOT / key
        if not path.is_file():
            missing.append(key)
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected[key]:
            changed.append(key)
    return {"manifest_paths": len(expected), "subset_paths": len(selected),
            "content_changed": changed, "missing": missing,
            "full_manifest_verification": "NOT_COMPLETED",
            "subset_status": "PASS" if not changed and not missing else "DIFFERENCES"}


if __name__ == "__main__":
    result = check()
    target = Path(__file__).with_name("protected_subset_check.json")
    target.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"manifest_paths": result["manifest_paths"],
                      "subset_paths": result["subset_paths"],
                      "content_changed_count": len(result["content_changed"]),
                      "missing_count": len(result["missing"]),
                      "subset_status": result["subset_status"]}))
