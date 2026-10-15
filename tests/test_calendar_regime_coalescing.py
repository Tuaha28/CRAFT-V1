from crafter.repair.calendar_regimes import _coalesce_regimes


def _reg(config, lo, hi, formula):
    return {"config": config, "lo": lo, "hi": hi, "formula": formula}


def test_coalesces_only_contiguous_equivalent_regimes():
    formula = ((1, 10), (1, 0), (1, 10))
    rows = [
        _reg("a", 20, 30, formula),
        _reg("a", 0, 10, formula),
        _reg("a", 10, 20, formula),
    ]
    assert _coalesce_regimes(rows) == [_reg("a", 0, 30, formula)]


def test_does_not_bridge_gaps_or_merge_different_meaning():
    f1 = ((1, 10),)
    f2 = ((1, 11),)
    rows = [
        _reg("a", 0, 10, f1),
        _reg("a", 11, 20, f1),
        _reg("a", 20, 30, f2),
        _reg("b", 30, 40, f2),
    ]
    assert _coalesce_regimes(rows) == rows
