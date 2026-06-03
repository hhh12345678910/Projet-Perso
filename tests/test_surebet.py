from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models import Book, MarketType, OddQuote, Outcome
from src.surebet import SUSPICIOUS_MARGIN_THRESHOLD, find_surebets


NOW = datetime(2026, 5, 28, tzinfo=timezone.utc)


def _q(book: Book, market: MarketType, label: str, odd: float, line: float | None = None,
       event_key: str = "20260601::a__vs__b") -> OddQuote:
    return OddQuote(
        event_key=event_key,
        book=book,
        market=market,
        outcome=Outcome(label=label, line=line),
        decimal_odd=odd,
        fetched_at=NOW,
        source_event_id="e1",
    )


def test_no_surebet_when_margin_is_positive_for_the_book():
    # Standard 1X2 with each book pricing in a 5% margin -> Σ(1/odd) > 1
    quotes = [
        _q(Book.UNIBET_BE, MarketType.H2H, "home", 2.00),
        _q(Book.BETFIRST, MarketType.H2H, "draw", 3.20),
        _q(Book.LADBROKES_BE, MarketType.H2H, "away", 3.60),
    ]
    # 1/2 + 1/3.2 + 1/3.6 = 0.5 + 0.3125 + 0.2778 = 1.0903 -> NO surebet
    assert find_surebets(quotes) == []


def test_finds_three_way_surebet_across_books():
    quotes = [
        _q(Book.UNIBET_BE, MarketType.H2H, "home", 2.10),
        _q(Book.BETFIRST, MarketType.H2H, "draw", 3.50),
        _q(Book.LADBROKES_BE, MarketType.H2H, "away", 4.20),
    ]
    # 1/2.1 + 1/3.5 + 1/4.2 = 0.4762 + 0.2857 + 0.2381 = 1.000 -> margin ~0%; bump one odd up
    quotes = [
        _q(Book.UNIBET_BE, MarketType.H2H, "home", 2.10),
        _q(Book.BETFIRST, MarketType.H2H, "draw", 3.60),
        _q(Book.LADBROKES_BE, MarketType.H2H, "away", 4.20),
    ]
    out = find_surebets(quotes)
    assert len(out) == 1
    s = out[0]
    assert s.market == MarketType.H2H
    assert s.legs["home"] == (2.10, Book.UNIBET_BE)
    assert s.legs["draw"] == (3.60, Book.BETFIRST)
    assert s.legs["away"] == (4.20, Book.LADBROKES_BE)
    assert s.margin == pytest.approx(0.00792, abs=1e-3)


def test_picks_best_odd_per_outcome():
    # Two books offer "home", we should keep the higher.
    quotes = [
        _q(Book.UNIBET_BE, MarketType.H2H, "home", 1.95),
        _q(Book.BETFIRST, MarketType.H2H, "home", 2.10),       # better
        _q(Book.LADBROKES_BE, MarketType.H2H, "draw", 3.60),
        _q(Book.GOLDEN_PALACE, MarketType.H2H, "away", 4.20),
    ]
    out = find_surebets(quotes)
    assert len(out) == 1
    assert out[0].legs["home"] == (2.10, Book.BETFIRST)


def test_drops_same_book_arbs():
    # All three sides offered by the same book are NOT a surebet by definition.
    quotes = [
        _q(Book.BETFIRST, MarketType.H2H, "home", 2.10),
        _q(Book.BETFIRST, MarketType.H2H, "draw", 3.60),
        _q(Book.BETFIRST, MarketType.H2H, "away", 4.20),
    ]
    assert find_surebets(quotes) == []


def test_two_way_totals_surebet():
    quotes = [
        _q(Book.UNIBET_BE, MarketType.TOTALS, "over", 2.05, line=2.5),
        _q(Book.BETFIRST, MarketType.TOTALS, "under", 2.05, line=2.5),
    ]
    # 1/2.05 + 1/2.05 = 0.9756 -> margin ~2.44%
    out = find_surebets(quotes)
    assert len(out) == 1
    assert out[0].market == MarketType.TOTALS
    assert out[0].line == 2.5
    assert out[0].margin == pytest.approx(0.0244, abs=1e-3)


def test_totals_with_different_lines_does_not_arb():
    # Over 2.5 + Under 3.5 are not opposite — different lines.
    quotes = [
        _q(Book.UNIBET_BE, MarketType.TOTALS, "over", 2.10, line=2.5),
        _q(Book.BETFIRST, MarketType.TOTALS, "under", 2.10, line=3.5),
    ]
    assert find_surebets(quotes) == []


def test_handicap_is_skipped():
    # Handicap line semantics vary across books — we explicitly don't try.
    quotes = [
        _q(Book.UNIBET_BE, MarketType.HANDICAP, "home", 2.10, line=-0.5),
        _q(Book.BETFIRST, MarketType.HANDICAP, "away", 2.10, line=0.5),
    ]
    assert find_surebets(quotes) == []


def test_suspicious_flag():
    # A 25% margin is unrealistic and should be flagged as suspicious.
    quotes = [
        _q(Book.UNIBET_BE, MarketType.H2H, "home", 3.00),
        _q(Book.BETFIRST, MarketType.H2H, "draw", 4.00),
        _q(Book.LADBROKES_BE, MarketType.H2H, "away", 4.00),
    ]
    # 1/3 + 1/4 + 1/4 = 0.833 -> margin 16.7%
    out = find_surebets(quotes)
    assert len(out) == 1
    assert out[0].suspicious is True
    assert out[0].margin > SUSPICIOUS_MARGIN_THRESHOLD


def test_surebet_stakes_and_roi():
    quotes = [
        _q(Book.UNIBET_BE, MarketType.H2H, "home", 2.10),
        _q(Book.BETFIRST, MarketType.H2H, "draw", 3.60),
        _q(Book.LADBROKES_BE, MarketType.H2H, "away", 4.20),
    ]
    out = find_surebets(quotes)
    s = out[0]
    bankroll = 100.0
    stakes = s.stakes(bankroll)
    assert sum(stakes.values()) == pytest.approx(bankroll)
    # Each leg's payout = stake_i * odd_i should be the same (lock-in property).
    payouts = [stakes[label] * odd for label, (odd, _) in s.legs.items()]
    assert max(payouts) - min(payouts) < 1e-6
    # ROI matches margin/(1-margin).
    expected_roi = s.margin / (1.0 - s.margin)
    assert s.roi == pytest.approx(expected_roi)
