from option_b.scenarios.non_final_pilot import NOW, build


def test_non_final_builder_matches_documented_alns_fixture_contract():
    scheduler = build(seed=42)
    assert scheduler.now == NOW
    assert scheduler.num_ops == 6
    assert scheduler.factory.op_keys == ["1001_1", "1001_2", "1002_1", "1002_2", "1003_1", "1003_2"]
    assert scheduler.factory.eligibility_map == {key: [1, 2] for key in scheduler.factory.op_keys}
    assert scheduler.factory.machines_per_line_map["1001_1"] == {1: 3, 2: 2}
    assert scheduler.present_operator_count == 10
    assert scheduler.factory.legacy_mode == "legacy_company_wide"
