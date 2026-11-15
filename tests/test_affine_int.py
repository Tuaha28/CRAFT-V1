"""_affine_int must return exactly what _affine returns for integer samples."""
import random

import pytest

from crafter.repair.calendar_regimes import _affine, _affine_int


def samples(rng):
    lo = rng.randrange(0, 20_000_000)
    hi = lo + rng.choice((1, 2, 3, rng.randrange(1, 5_000_000)))
    xs = sorted({lo, hi, (lo + hi) // 2, lo + 1, hi - 1})
    width = rng.choice((1, 3, 5, 7))
    slopes = [rng.choice((0, 1, 1, 2, -1)) for _ in range(width)]
    consts = [rng.randrange(-5_000_000, 30_000_000) for _ in range(width)]
    rows = []
    for x in xs:
        vector = [s * x + c for s, c in zip(slopes, consts)]
        if rng.random() < 0.15:
            vector[rng.randrange(width)] += rng.choice((-1, 1, 3600))
        if rng.random() < 0.05:
            vector = vector + [vector[-1] + 10, vector[-1] + 20]
        rows.append((x, tuple(vector)))
    return rows


@pytest.mark.parametrize("seed", range(20))
def test_matches_float_affine(seed):
    rng = random.Random(seed)
    for _ in range(2000):
        rows = samples(rng)
        assert _affine_int(rows) == _affine(rows)


def test_non_integer_values_use_original():
    rows = [(0, (1.5,)), (10, (1.5,)), (5, (1.5,))]
    assert _affine_int(rows) == _affine(rows)
