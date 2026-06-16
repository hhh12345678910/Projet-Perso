from __future__ import annotations

from datetime import datetime, timezone

from src.config import ScanConfig
from src.main import find_value_bets
from src.matcher import event_key
from src.models import Book, FairLine, MarketType, OddQuote, Outcome

NOW = datetime(2026, 6, 16, 20, 0, tzinfo=timezone.utc)
EK = event_key("Home FC", "Away FC", NOW)


def _q(book: Book, market: MarketType, label: str, odd: float,
       line: float | None = None) -> OddQuote:
    return OddQuote(
        event_key=EK,
        book=book,
        market=market,
        outcome=Outcome(label=label, line=line),
        decimal_odd=odd,
        fetched_at=NOW,
        source_event_id="x",
    )


def _fair(market: MarketType, outcomes: dict[str, float],
          line: float | None = None) -> dict:
    key = (EK, market, line)
    return {key: FairLine(event_key=EK, market=market, outcomes=outcomes)}


def test_h2h_value_bet_is_detected():
    # Soft book prices away at 2.50 while the fair away prob is 0.50 (fair odd
    # 2.00) -> ~25% EV, a genuine moneyline value bet.
    quotes = [
        _q(Book.BETFIRST, MarketType.H2H, "home", 1.60),
        _q(Book.BETFIRST, MarketType.H2H, "away", 2.50),
    ]
    fair = _fair(MarketType.H2H, {"home": 0.50, "away": 0.50})
    bets = find_value_bets(quotes, fair, ScanConfig())
    assert [b.outcome.label for b in bets] == ["away"]
    assert bets[0].market == MarketType.H2H


def test_handicap_value_bet_is_excluded():
    # Same shape but on a handicap line: even with a huge apparent edge, the
    # mismatched line semantics across books make this a phantom. It must not
    # surface as a value bet.
    quotes = [
        _q(Book.BETFIRST, MarketType.HANDICAP, "home", 1.60, line=-1.0),
        _q(Book.BETFIRST, MarketType.HANDICAP, "away", 3.75, line=-1.0),
    ]
    fair = _fair(MarketType.HANDICAP, {"home": 0.55, "away": 0.45}, line=-1.0)
    bets = find_value_bets(quotes, fair, ScanConfig())
    assert bets == []
