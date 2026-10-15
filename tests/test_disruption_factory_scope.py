from datetime import datetime, timedelta
import pandas as pd

from agent import impact_assessor as impact


T0 = datetime(2026, 9, 13, 8)


def test_alternate_machine_query_is_factory_scoped(monkeypatch):
    captured = {}
    def read_sql(query, _engine, params):
        captured["sql"] = str(query)
        captured["params"] = params
        return pd.DataFrame(columns=["asset_id", "line_id", "factory_id"])
    monkeypatch.setattr(impact.pd, "read_sql", read_sql)
    monkeypatch.setattr(impact, "get_engine", lambda: object())
    impact._get_alternate_machines("Body Assembly", 99, 5)
    assert captured["params"]["factory_id"] == 5
    assert "ma.factory_id = :factory_id" in captured["sql"]
    assert "ma.line_id IS NOT NULL" in captured["sql"]


def test_assessment_threads_owned_factory_and_candidate_line(monkeypatch):
    affected = pd.DataFrame([{
        "schedule_item_id": 1, "production_order_id": "o1",
        "operation_id": "op1", "asset_id": 99, "line_id": 4,
        "owned_factory_id": 2, "planned_start_time": T0,
        "planned_end_time": T0+timedelta(hours=1),
        "execution_status": "Scheduled",
    }])
    calls = []
    monkeypatch.setattr(impact, "_get_affected_operations", lambda _asset: affected)
    monkeypatch.setattr(impact, "require_factory_ownership", lambda _ids: {"o1": 2})
    monkeypatch.setattr(impact, "_get_machine_operation_type", lambda _asset: "Body Assembly")
    def alternates(_optype, _exclude, factory_id):
        calls.append(factory_id)
        return pd.DataFrame([{"asset_id": 22, "line_id": 7, "factory_id": 2}])
    monkeypatch.setattr(impact, "_get_alternate_machines", alternates)
    monkeypatch.setattr(impact, "_get_reserve_context", lambda: ({22}, {"body assembly": [22]}))
    monkeypatch.setattr(impact, "fetch_machine_state", lambda: pd.DataFrame([
        {"asset_id": 22, "line_id": 7, "factory_id": 2}]))
    monkeypatch.setattr(impact, "_get_committed_schedules_bulk", lambda _ids: {})
    monkeypatch.setattr(impact, "_get_order_slack_days_bulk",
                        lambda _ids, now=None: {"o1": 100.0})
    result = impact.assess_machine_breakdown(99)
    assert calls == [2]
    assert result[0]["candidate_asset_id"] == 22
    assert result[0]["candidate_line_id"] == 7


def test_missing_ownership_escalates_without_searching_company_wide(monkeypatch):
    affected = pd.DataFrame([{
        "schedule_item_id": 1, "production_order_id": "o1",
        "operation_id": "op1", "asset_id": 99, "line_id": 4,
        "owned_factory_id": None, "planned_start_time": T0,
        "planned_end_time": T0+timedelta(hours=1),
        "execution_status": "Scheduled",
    }])
    monkeypatch.setattr(impact, "_get_affected_operations", lambda _asset: affected)
    monkeypatch.setattr(impact, "require_factory_ownership",
                        lambda _ids: (_ for _ in ()).throw(impact.FactoryOwnershipError("missing")))
    monkeypatch.setattr(impact, "_get_machine_operation_type", lambda _asset: "Body Assembly")
    monkeypatch.setattr(
        impact, "_get_alternate_machines",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not search")))
    result = impact.assess_machine_breakdown(99)
    assert result[0]["tier"] == impact.Tier.FULL_RESCHEDULE.value
    assert "ownership" in result[0]["reason"].lower()
