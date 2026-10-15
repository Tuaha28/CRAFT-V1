import json
from pathlib import Path

from crafter.authoritative_semantics import (
    authoritative_changeover_seconds,
    authoritative_productive_seconds,
)
from crafter.lns_probe.exact_line_k_probe import (
    E13_DIR,
    _machine_accepts,
    build_candidates,
    select_probe_operations,
)


def test_probe_selection_is_frozen_and_unique():
    selected = select_probe_operations()
    assert len(selected) == 20
    assert len({row["operation_key"] for row in selected}) == 20
    assert selected[0]["operation_key"] == "556_2974"
    assert selected[-1]["operation_key"] == "563_1583"


def test_candidate_classes_keep_their_declared_freedom_boundary():
    selected = select_probe_operations(limit=3)
    candidates = build_candidates(E13_DIR, selected)
    for key, groups in candidates.items():
        incumbent = groups["incumbent"]
        assert all(row["k"] == incumbent["k"] for row in groups["LINE_ONLY"])
        assert all(row["line_id"] == incumbent["line_id"] for row in groups["K_ONLY"])
        assert all(
            (row["line_id"], row["k"]) != (incumbent["line_id"], incumbent["k"])
            for row in groups["LINE_K"]
        )


def test_candidate_provenance_matches_e13_artifacts():
    selected = select_probe_operations(limit=1)
    candidates = build_candidates(E13_DIR, selected)
    key = selected[0]["operation_key"]
    source = json.loads(
        (Path(E13_DIR) / "top_line_k_candidates.json").read_text())
    assert candidates[key]["LINE_K"]
    assert candidates[key]["LINE_K"][0]["source"].startswith("E1.3")
    assert source["top"][0]["operation_key"] == key


def test_machine_transition_boundaries_are_authoritative():
    assert _machine_accepts([(0, 100, "A", "a")], 100, 200, "A")[0]
    assert not _machine_accepts([(0, 100, "A", "a")], 100, 200, "B")[0]
    assert authoritative_changeover_seconds("A", "A") == 0
    assert authoritative_changeover_seconds("A", "B") == 1500


def test_authoritative_duration_changes_with_machine_count():
    detail = {"setup_mins": 15.0, "serial_processing_mins": 120.0}
    assert authoritative_productive_seconds(detail, 1) == 7200
    assert authoritative_productive_seconds(detail, 2) == 3600
    assert authoritative_productive_seconds(detail, 4) == 1800
