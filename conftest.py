"""Session-wide MySQL access policy for the test suite.

Capture-only runners (run_factory_test.py, run_bounded.py) forbid legacy
database connections at import time. Without isolation that guard leaks into
every later test in the same session and turns MySQL-backed tests into
misleading failures. The policy is applied per test in ``pytest_runtest_setup``
-- before any fixture of any scope runs -- so it covers module-scoped database
fixtures too:

* default: a test (or fixture) that tries to open MySQL is skipped;
* ``--run-mysql``: such tests may connect, exactly as when run on their own;
* tests that assert the runner guard itself keep the runner's guard.
"""
import sys

import pytest

try:
    import pymysql
    _ORIGINAL_PYMYSQL_CONNECT = pymysql.connect
except ImportError:  # pragma: no cover - scheduler venv always has pymysql
    pymysql = None
    _ORIGINAL_PYMYSQL_CONNECT = None

# Modules whose tests deliberately verify the capture-only connection guard.
_GUARD_TEST_MODULES = {"test_factory46_operation_schedule"}
_runner_pymysql_guard = None


def pytest_addoption(parser):
    parser.addoption("--run-mysql", action="store_true", default=False,
                     help="allow tests to connect to the configured MySQL database")


def _skip_connection(*_args, **_kwargs):
    pytest.skip("requires MySQL; rerun with --run-mysql to allow database access")


def _runner_guards():
    """SQLAlchemy do_connect guards installed by imported capture-only runners."""
    from sqlalchemy.engine import Engine

    for module in list(sys.modules.values()):
        # vars(), not getattr(): some packages (torch.ops) fabricate attributes.
        namespace = getattr(module, "__dict__", None) or {}
        guard = namespace.get("_forbid_sqlalchemy_connection")
        engine = namespace.get("_application_engine")
        if callable(guard) and isinstance(engine, Engine):
            yield engine, guard


def pytest_configure(config):
    if pymysql is not None and not config.getoption("--run-mysql"):
        pymysql.connect = _skip_connection


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    global _runner_pymysql_guard
    if pymysql is None:
        return
    from sqlalchemy import event

    current = pymysql.connect
    if current not in (_skip_connection, _ORIGINAL_PYMYSQL_CONNECT):
        _runner_pymysql_guard = current  # a capture-only runner was imported
    module = getattr(item, "module", None)
    keep_runner_guard = module is not None and \
        module.__name__.rpartition(".")[2] in _GUARD_TEST_MODULES
    for engine, guard in _runner_guards():
        present = event.contains(engine, "do_connect", guard)
        if keep_runner_guard and not present:
            event.listen(engine, "do_connect", guard)
        elif not keep_runner_guard and present:
            event.remove(engine, "do_connect", guard)
    if keep_runner_guard and _runner_pymysql_guard is not None:
        pymysql.connect = _runner_pymysql_guard
    else:
        pymysql.connect = (_ORIGINAL_PYMYSQL_CONNECT if item.config.getoption("--run-mysql")
                           else _skip_connection)
