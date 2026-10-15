"""Run only the isolated five-order semantic smoke gate."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .data_adapter import build_smoke_fixture
from .diagnostics import phenotype_dict, solution_dict
from .phenotype import exported_rows, validate
from .solver import solve
from .worker_bridge import allocate_exact_workers


def run_smoke(output_dir="outputs/crafter", time_limit_seconds=10.0):
    fixture = build_smoke_fixture()
    solution = solve(fixture.problem, time_limit_seconds=time_limit_seconds)
    phenotype = allocate_exact_workers(
        fixture.problem, solution, fixture.operator_index)
    phenotype = validate(fixture.problem, phenotype, fixture.operator_index)
    source_paths = sorted([
        *Path("crafter").glob("*.py"),
        *Path("tests").glob("test_crafter_*.py"),
    ], key=str)
    result = {
        "experiment": "isolated-crafter-semantic-smoke-v1",
        "problem_id": fixture.problem.problem_id,
        "database_access": "none",
        "orders": len(fixture.problem.orders),
        "operations": len(fixture.problem.operations),
        "solution": solution_dict(solution),
        "phenotype": phenotype_dict(phenotype),
        "frozen_export_rows": list(exported_rows(fixture.problem, phenotype)),
        "reproducibility_debt": "requirements.txt lists ortools without an exact version pin",
        "promotion_status": "experimental-only; not integrated",
        "source_sha256": {
            str(path).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source_paths
        },
    }
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "crafter_smoke_result.json"
    raw = json.dumps(result, indent=2, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    sha_path = path.with_suffix(path.suffix + ".sha256")
    sha_path.write_text(digest + "\n", encoding="utf-8")
    return result, path, digest


if __name__ == "__main__":
    result, path, digest = run_smoke()
    print(json.dumps({
        "status": result["phenotype"]["status"],
        "cp_status": result["solution"]["statistics"]["cp_status"],
        "artifact": str(path),
        "sha256": digest,
    }, indent=2))
