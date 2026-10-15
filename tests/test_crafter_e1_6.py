"""Focused contracts for bounded exact-machine E1.6 artifacts."""
import json
from pathlib import Path


ROOT = Path("outputs/crafter/e1_6_exact_machine_local_cp")


def test_e1_6_machine_sets_are_bounded_and_exact_k():
    path = ROOT / "machine_set_candidates.json"
    if not path.exists():
        return
    rows = json.loads(path.read_text(encoding="utf-8"))
    for row in rows:
        k = int(row["k"])
        sets = row.get("sets", [])
        assert len(sets) <= 3
        assert len({tuple(sorted(item)) for item in sets}) == len(sets)
        assert all(len(item) == k for item in sets)


def test_e1_6_cp_models_remain_bounded_and_search():
    path = ROOT / "cp_solver_results.json"
    if not path.exists():
        return
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert rows
    assert all(row["status"] in {"OPTIMAL", "FEASIBLE"} for row in rows)
    assert max(row["model_preflight"]["variables"] for row in rows) < 10000


def test_e1_6_matched_neighborhoods_and_budget():
    if not (ROOT / "neighborhoods.json").exists():
        return
    neighborhoods = json.loads((ROOT / "neighborhoods.json").read_text())
    selectors = json.loads((ROOT / "graph_selector.json").read_text())
    assert len(neighborhoods) == 3
    assert all(len(item["operation_keys"]) == 30 for item in neighborhoods)
    assert all(len(selectors[str(item["seed"])]) == 5 for item in neighborhoods)


def test_e1_6_manifest_is_self_consistent():
    import hashlib
    manifest = ROOT / "artifact_hash_manifest.json"
    if not manifest.exists():
        return
    data = json.loads(manifest.read_text())
    assert all((ROOT / name).exists() and hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
               for name, digest in data["files"].items())
