from __future__ import annotations

import math

import pytest

from src.devig import devig, devig_multiplicative, devig_shin, overround


def test_overround_positive_for_real_odds():
    assert overround([2.10, 3.40, 3.80]) > 0


def test_multiplicative_sums_to_one():
    p = devig_multiplicative([2.10, 3.40, 3.80])
    assert math.isclose(sum(p), 1.0, abs_tol=1e-12)


def test_shin_corrects_favourite_longshot_bias():
    # Shin attributes part of the overround to insiders → favourites are
    # under-priced by the book, so Shin gives them MORE probability than
    # multiplicative, and less to longshots.
    odds = [1.50, 4.20, 7.50]
    p_shin = devig_shin(odds)
    p_mult = devig_multiplicative(odds)
    assert math.isclose(sum(p_shin), 1.0, abs_tol=1e-8)
    assert p_shin[0] > p_mult[0]
    assert p_shin[-1] < p_mult[-1]


@pytest.mark.parametrize("method", ["multiplicative", "additive", "power", "shin"])
def test_all_methods_sum_to_one(method):
    p = devig([2.05, 3.50, 3.90], method=method)
    assert math.isclose(sum(p), 1.0, abs_tol=1e-6)
    assert all(0 < x < 1 for x in p)


def test_devig_fair_book_is_identity_up_to_normalisation():
    # A book with zero overround should return ~normalized implied probs.
    odds = [2.00, 2.00]
    p = devig_shin(odds)
    assert math.isclose(p[0], 0.5, abs_tol=1e-6)
    assert math.isclose(p[1], 0.5, abs_tol=1e-6)
