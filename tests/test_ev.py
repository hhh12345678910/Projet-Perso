from __future__ import annotations

import math

import pytest

from src.ev import ev_pct, fair_odd, kelly_fraction, kelly_stake


def test_fair_odd_inverse_of_prob():
    assert math.isclose(fair_odd(0.5), 2.0)
    assert math.isclose(fair_odd(0.25), 4.0)


def test_ev_pct_zero_when_odd_equals_fair():
    assert math.isclose(ev_pct(2.0, 0.5), 0.0, abs_tol=1e-9)


def test_ev_pct_positive_above_fair():
    assert ev_pct(2.10, 0.5) > 0
    assert ev_pct(1.90, 0.5) < 0


def test_kelly_fraction_zero_or_negative_when_no_edge():
    assert kelly_fraction(2.0, 0.5) <= 0
    assert kelly_fraction(1.9, 0.5) < 0


def test_kelly_fraction_positive_with_edge():
    k = kelly_fraction(2.10, 0.55)
    assert k > 0


def test_quarter_kelly_stake_scaling():
    full = kelly_fraction(2.10, 0.55) * 1000.0
    quarter = kelly_stake(2.10, 0.55, 1000.0, fraction=0.25)
    assert math.isclose(quarter, full * 0.25, rel_tol=1e-9)


def test_fair_odd_rejects_invalid():
    with pytest.raises(ValueError):
        fair_odd(0.0)
    with pytest.raises(ValueError):
        fair_odd(1.0)
