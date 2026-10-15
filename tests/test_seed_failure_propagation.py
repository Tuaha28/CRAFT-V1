"""Failure injection only: these tests never connect to the database."""
from unittest.mock import Mock
import pytest
from data_generation import seed_database_fixed as seed


def test_connection_failure_reaches_caller(monkeypatch):
    error = RuntimeError("database unavailable")
    monkeypatch.setattr(seed.mysql.connector, "connect", Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        seed.execute_complete_seed()
    assert caught.value is error


def test_cursor_failure_preserved_and_connection_closed(monkeypatch):
    error = RuntimeError("cursor unavailable")
    conn = Mock()
    conn.cursor.side_effect = error
    monkeypatch.setattr(seed.mysql.connector, "connect", Mock(return_value=conn))
    with pytest.raises(RuntimeError) as caught:
        seed.execute_complete_seed()
    assert caught.value is error
    conn.rollback.assert_called_once()
    conn.close.assert_called_once()


@pytest.mark.parametrize("broken_cleanup", [False, True])
def test_seed_failure_never_returns_success(monkeypatch, broken_cleanup):
    error = RuntimeError("SQL failed")
    conn, cursor = Mock(), Mock()
    conn.cursor.return_value = cursor
    cursor.execute.side_effect = error
    if broken_cleanup:
        conn.rollback.side_effect = RuntimeError("rollback failed")
        cursor.close.side_effect = RuntimeError("cursor close failed")
        conn.close.side_effect = RuntimeError("connection close failed")
    monkeypatch.setattr(seed.mysql.connector, "connect", Mock(return_value=conn))
    with pytest.raises(RuntimeError) as caught:
        seed.execute_complete_seed()
    assert caught.value is error
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()
    cursor.close.assert_called_once()
    conn.close.assert_called_once()
    if broken_cleanup:
        assert any("Rollback also failed" in n for n in error.__notes__)
        assert any("Cleanup also failed" in n for n in error.__notes__)
