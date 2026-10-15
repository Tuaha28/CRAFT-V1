import pytest

from agent import event_handler


class _Result:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _Session:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(self, *_args, **_kwargs):
        if self.error is not None:
            raise self.error
        return self.result

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _handler():
    handler = object.__new__(event_handler.ShopFloorEventHandler)
    handler.decision_engine = type('Engine', (), {'manager_forced_orders': set()})()
    return handler


@pytest.mark.parametrize(
    "session",
    [
        _Session(error=RuntimeError("database unavailable")),
        _Session(result=_Result(0)),
    ],
)
def test_breakdown_aborts_when_machine_state_was_not_persisted(monkeypatch, session):
    assessed = []
    monkeypatch.setattr(event_handler, "get_db_session", lambda: iter([session]))
    monkeypatch.setattr(
        event_handler,
        "assess_machine_breakdown",
        lambda *_args, **_kwargs: assessed.append(True),
    )

    with pytest.raises(RuntimeError, match="was not persisted"):
        _handler().handle_machine_breakdown(999)

    assert session.rolled_back
    assert session.closed
    assert not session.committed
    assert assessed == []


def test_breakdown_assesses_only_after_exactly_one_machine_was_updated(monkeypatch):
    session = _Session(result=_Result(1))
    assessed = []
    monkeypatch.setattr(event_handler, "get_db_session", lambda: iter([session]))
    monkeypatch.setattr(
        event_handler,
        "assess_machine_breakdown",
        lambda machine_id, **_kwargs: assessed.append(machine_id) or [],
    )

    _handler().handle_machine_breakdown(42)

    assert session.committed
    assert not session.rolled_back
    assert session.closed
    assert assessed == [42]


class _Agent:
    def __init__(self):
        self.reschedules = 0

    def trigger_rescheduling(self):
        self.reschedules += 1


class _RaisingHandler:
    def __init__(self, error):
        self.error = error

    def handle_machine_breakdown(self, *_args, **_kwargs):
        raise self.error


def _monitor_with(error):
    from agent.monitor import FactoryMonitor
    monitor = object.__new__(FactoryMonitor)
    monitor.handler = _RaisingHandler(error)
    monitor.agent = _Agent()
    return monitor


def test_monitor_does_not_reschedule_against_unpersisted_breakdown_state():
    from agent.event_handler import BreakdownPersistenceError
    monitor = _monitor_with(BreakdownPersistenceError("write failed"))
    monitor._handle_detected_breakdown(42)
    assert monitor.agent.reschedules == 0


def test_monitor_retains_full_reschedule_fallback_after_persistence():
    monitor = _monitor_with(ValueError("assessment failed"))
    monitor._handle_detected_breakdown(42)
    assert monitor.agent.reschedules == 1
