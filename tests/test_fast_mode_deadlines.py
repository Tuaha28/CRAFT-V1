import crafter.repair.fast_mode as fast_mode


def test_stage_budgets_share_one_absolute_deadline(monkeypatch):
    ticks = iter([117.0, 145.0, 161.0])
    monkeypatch.setattr(fast_mode.time, "perf_counter", lambda: next(ticks))
    deadline = 160.0
    assert fast_mode.deadline_stage_budget(deadline, 60.0) == 43.0
    assert fast_mode.deadline_stage_budget(deadline, 60.0) == 15.0
    assert fast_mode.deadline_stage_budget(deadline, 60.0) == 0.0


def test_stage_is_not_started_when_shared_budget_is_exhausted(monkeypatch):
    monkeypatch.setattr(fast_mode.time, "perf_counter", lambda: 200.0)
    assert fast_mode.deadline_stage_budget(199.0, 60.0) == 0.0


def test_adaptive_budget_is_one_total_budget(monkeypatch):
    monkeypatch.setenv("CRAFT_FAST_MODE", "1")
    monkeypatch.setenv("CRAFT_ADAPTIVE_SOLVE_LIMIT_SECONDS", "90")
    config = fast_mode.fast_mode_config()
    assert config.adaptive_solve_limit_seconds == 90.0
    assert fast_mode.fast_search_plan(adaptive=True) == ((1, 7, 90.0),
                                                          (2, 7, 90.0),
                                                          (3, 7, 90.0))
    # The plan entries are per-stage ceilings; the solver's shared absolute
    # deadline (tested above) is what prevents 90 seconds being reset.


def test_nested_canonical_calls_reuse_one_normal_deadline():
    state = {}
    first = fast_mode.shared_attempt_deadline(state, 60.0, "normal", now=100.0)
    second = fast_mode.shared_attempt_deadline(state, 60.0, "normal", now=300.0)
    assert first == 160.0
    assert second == first
    adaptive = fast_mode.shared_attempt_deadline(state, 90.0, "adaptive", now=400.0)
    assert adaptive == 490.0


def test_outer_watchdog_is_monotonic_and_bounded(monkeypatch):
    ticks = iter([10.0, 12.0, 21.0])
    monkeypatch.setattr(fast_mode.time, "perf_counter", lambda: next(ticks))
    watchdog = fast_mode.OptimizationWindowWatchdog.start(10.0)
    assert watchdog.deadline == 20.0
    assert watchdog.remaining() == 8.0
    assert watchdog.expired() is True
