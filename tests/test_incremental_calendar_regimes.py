from crafter.repair import calendar_regimes as regimes_module


def test_incremental_regime_merge_matches_full_domain_and_reuses_unchanged_operation(monkeypatch):
    state = {"keys": ("op-a", "op-b")}
    calls = []

    def fake_generate(state, configurations, *, cache=None,
                      additional_breakpoints_by_operation=None,
                      operation_keys=None):
        keys = tuple(sorted(operation_keys or state["keys"]))
        calls.append(keys)
        regimes = {
            key: tuple((key, config, tuple((additional_breakpoints_by_operation or {}).get(key, ())))
                       for config in configurations.get(key, ()))
            for key in keys
        }
        summary = [{"operation_key": key, "regime_count": len(regimes[key])}
                   for key in keys]
        checks = [{"operation_key": key, "equivalent": True} for key in keys]
        return regimes, summary, sum(map(len, regimes.values())), {
            "checks": checks, "mismatches": 0, "all_equivalent": True,
            "cache_profile": {},
        }

    monkeypatch.setattr(regimes_module, "generate_calendar_regimes", fake_generate)
    builder = regimes_module.IncrementalCalendarRegimeBuilder(state)
    base_configs = {"op-a": ("a1",), "op-b": ("b1",)}
    base_regimes, base_summary, _total, base_equivalence = fake_generate(
        state, base_configs)
    builder.seed(base_configs, base_regimes, base_summary, base_equivalence)

    expanded = {"op-a": ("a1", "a2"), "op-b": ("b1",)}
    merged, _summary, _total, evidence = builder.build(expanded)
    full, *_ = fake_generate(state, expanded)

    assert merged == full
    assert calls[-2] == ("op-a",)
    assert evidence["incremental_profile"] == {
        "operation_hits": 1,
        "operation_misses": 1,
        "regimes_reused": 1,
        "regimes_generated_new": 2,
        "build_seconds": evidence["incremental_profile"]["build_seconds"],
    }


def test_changed_authoritative_start_probe_invalidates_only_that_operation(monkeypatch):
    state = {"keys": ("op-a", "op-b")}
    calls = []

    def fake_generate(state, configurations, *, cache=None,
                      additional_breakpoints_by_operation=None,
                      operation_keys=None):
        keys = tuple(sorted(operation_keys or state["keys"]))
        calls.append(keys)
        regimes = {key: ((key, tuple((additional_breakpoints_by_operation or {}).get(key, ()))),)
                   for key in keys}
        return regimes, [{"operation_key": key} for key in keys], len(keys), {
            "checks": [], "mismatches": 0, "all_equivalent": True,
            "cache_profile": {},
        }

    monkeypatch.setattr(regimes_module, "generate_calendar_regimes", fake_generate)
    configs = {"op-a": ("a",), "op-b": ("b",)}
    builder = regimes_module.IncrementalCalendarRegimeBuilder(state)
    initial, summary, _total, equivalence = fake_generate(state, configs)
    builder.seed(configs, initial, summary, equivalence)
    builder.build(configs, additional_breakpoints_by_operation={"op-a": (42,)})
    assert calls[-1] == ("op-a",)
