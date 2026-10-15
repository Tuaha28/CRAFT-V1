from pathlib import Path


PRODUCTION_PATHS = (
    "ga/scheduler.py",
    "ga/fitness.py",
    "ga/operators.py",
    "ga/worker_allocation.py",
    "ga/rolling_horizon.py",
    "agent/decision_engine.py",
)


def test_production_scheduler_has_no_crafter_dependency():
    for name in PRODUCTION_PATHS:
        source = Path(name).read_text(encoding="utf-8").lower()
        assert "import crafter" not in source
        assert "from crafter" not in source


def test_crafter_fixture_has_no_database_dependency():
    source = Path("crafter/data_adapter.py").read_text(encoding="utf-8").lower()
    assert "database." not in source
    assert "get_engine" not in source


def test_ortools_pin_is_recorded_as_reproducibility_debt():
    lines = Path("requirements.txt").read_text(encoding="utf-8").splitlines()
    ortools = next(line.strip() for line in lines if line.strip().startswith("ortools"))
    assert ortools == "ortools"
