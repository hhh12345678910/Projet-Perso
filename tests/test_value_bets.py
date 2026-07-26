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


# ---------------------------------------------------------------------------
# Secondary sharp source is a fallback, never a blend. Pinnacle is the
# reference because it's the most accurate source available; averaging it with
# anything less accurate can only move the estimate away from the truth.
# ---------------------------------------------------------------------------

def _sharp_quote(book, label, odd, key="202607261800::alpha__vs__beta"):
    from datetime import datetime, timezone
    from src.models import OddQuote, Outcome, MarketType

    return OddQuote(
        event_key=key, book=book, market=MarketType.H2H,
        outcome=Outcome(label=label), decimal_odd=odd,
        fetched_at=datetime.now(timezone.utc), source_event_id="x",
    )


def test_pinnacle_line_is_untouched_by_the_secondary():
    from src.main import build_fair_lines
    from src.models import Book, MarketType

    pin = [_sharp_quote(Book.PINNACLE, "home", 2.00),
           _sharp_quote(Book.PINNACLE, "away", 2.00)]
    # A secondary disagreeing sharply must not shift the Pinnacle-derived line.
    sec = [_sharp_quote(Book.SMARKETS, "home", 1.20),
           _sharp_quote(Book.SMARKETS, "away", 6.00)]

    alone = build_fair_lines(pin, "shin")
    withsec = build_fair_lines(pin, "shin", secondary_quotes=sec)
    key = ("202607261800::alpha__vs__beta", MarketType.H2H, None)
    assert withsec[key].outcomes == alone[key].outcomes
    assert withsec[key].reference_book is Book.PINNACLE


def test_secondary_fills_events_pinnacle_does_not_price():
    from src.main import build_fair_lines
    from src.models import Book, MarketType

    pin = [_sharp_quote(Book.PINNACLE, "home", 2.00),
           _sharp_quote(Book.PINNACLE, "away", 2.00)]
    other = "202607261900::gamma__vs__delta"
    sec = [_sharp_quote(Book.SMARKETS, "home", 1.80, other),
           _sharp_quote(Book.SMARKETS, "away", 2.20, other)]

    fair = build_fair_lines(pin, "shin", secondary_quotes=sec)
    sec_key = (other, MarketType.H2H, None)
    assert sec_key in fair
    # Tagged with its real source, so a bet valued against the fallback stays
    # distinguishable from one valued against Pinnacle.
    assert fair[sec_key].reference_book is Book.SMARKETS


def test_secondary_labels_do_not_leak_into_a_pinnacle_market():
    """Pinnacle listing a 2-way market while an exchange lists 3-way must not
    graft a 'draw' onto the Pinnacle fair line."""
    from src.main import build_fair_lines
    from src.models import MarketType, Book

    pin = [_sharp_quote(Book.PINNACLE, "home", 2.00),
           _sharp_quote(Book.PINNACLE, "away", 2.00)]
    sec = [_sharp_quote(Book.SMARKETS, "home", 2.60),
           _sharp_quote(Book.SMARKETS, "draw", 3.40),
           _sharp_quote(Book.SMARKETS, "away", 2.90)]

    fair = build_fair_lines(pin, "shin", secondary_quotes=sec)
    key = ("202607261800::alpha__vs__beta", MarketType.H2H, None)
    assert set(fair[key].outcomes) == {"home", "away"}
