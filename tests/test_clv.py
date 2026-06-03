from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.clv import (
    aggregate,
    clv_pct,
    event_started,
    group_by,
    index_quotes_by_market,
)
from src.models import Book, MarketType, OddQuote, Outcome


def test_clv_pct_positive_when_we_beat_the_close():
    # Took 1.86, sharp closed at 1.625 -> we beat the close by ~14.5%.
    assert clv_pct(1.86, 1.625) == pytest.approx(0.14462, abs=1e-3)


def test_clv_pct_negative_when_we_chase_the_close():
    # Took 1.50, sharp moved to 1.60 -> we underpriced our pick.
    assert clv_pct(1.50, 1.60) == pytest.approx(-0.0625, abs=1e-4)


def test_clv_pct_zero_when_we_match_the_close():
    assert clv_pct(2.00, 2.00) == 0.0


def test_clv_pct_handles_pathological_close():
    assert clv_pct(2.00, 0.0) == 0.0
    assert clv_pct(2.00, -1.0) == 0.0


def test_event_started_uses_parsed_key():
    past = "202301010000::a__vs__b"
    future = "210001010000::a__vs__b"
    assert event_started(past) is True
    assert event_started(future) is False
    assert event_started("garbage") is False


def test_event_started_respects_now_override():
    key = "202606010000::a__vs__b"
    before = datetime(2026, 5, 31, tzinfo=timezone.utc)
    after = datetime(2026, 6, 1, 1, tzinfo=timezone.utc)
    assert event_started(key, now=before) is False
    assert event_started(key, now=after) is True


NOW = datetime(2026, 5, 28, tzinfo=timezone.utc)


def _q(label: str, odd: float, fetched_at: datetime = NOW) -> OddQuote:
    return OddQuote(
        event_key="20260601::a__vs__b",
        book=Book.PINNACLE,
        market=MarketType.H2H,
        outcome=Outcome(label=label),
        decimal_odd=odd,
        fetched_at=fetched_at,
        source_event_id="1",
    )


def test_index_keeps_freshest_quote_per_key():
    old = _q("home", 1.90, fetched_at=datetime(2026, 5, 28, 12, tzinfo=timezone.utc))
    new = _q("home", 1.85, fetched_at=datetime(2026, 5, 28, 13, tzinfo=timezone.utc))
    idx = index_quotes_by_market([old, new])
    assert idx[("20260601::a__vs__b", "h2h", "home", None)].decimal_odd == 1.85


def test_aggregate_empty_is_zero():
    s = aggregate([])
    assert s.n == 0 and s.mean_clv_pct == 0.0 and s.positive_rate == 0.0


def test_aggregate_mean_and_positive_rate():
    # Three bets: CLV = +5%, +0%, -3%.
    pairs = [(1.05, 1.0), (1.00, 1.0), (0.97, 1.0)]
    s = aggregate(pairs)
    assert s.n == 3
    assert s.n_positive == 1
    assert s.mean_clv_pct == pytest.approx((5 + 0 - 3) / 3, abs=1e-9)
    assert s.median_clv_pct == 0.0
    assert s.positive_rate == pytest.approx(1 / 3)
    assert s.expected_pnl_per_unit == pytest.approx(s.mean_clv_pct / 100)


def test_group_by_partitions_pairs_by_attribute():
    rows = [
        {"book": "unibet_be", "odd_taken": 2.0, "closing_odd": 1.9},
        {"book": "betfirst",  "odd_taken": 3.0, "closing_odd": 2.9},
        {"book": "unibet_be", "odd_taken": 1.8, "closing_odd": 1.7},
    ]
    parts = group_by(rows, "book")
    assert set(parts) == {"unibet_be", "betfirst"}
    assert len(parts["unibet_be"]) == 2
    assert len(parts["betfirst"]) == 1
