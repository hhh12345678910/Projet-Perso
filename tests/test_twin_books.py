from __future__ import annotations

from datetime import datetime, timezone

from src.main import merge_twin_book_value_bets
from src.alerter import format_value_bet
from src.models import Book, MarketType, Outcome, ValueBet

NOW = datetime(2026, 6, 16, 18, 0, tzinfo=timezone.utc)
EK = "202606161800::a__vs__b"


def _vb(book: Book, odd: float = 2.40, ev: float = 9.0,
        label: str = "home", line=None) -> ValueBet:
    return ValueBet(
        event_key=EK, book=book, market=MarketType.H2H,
        outcome=Outcome(label=label, line=line), odd_taken=odd,
        fair_prob=1 / (odd / (1 + ev / 100)), fair_odd=odd / (1 + ev / 100),
        ev_pct=ev, kelly_stake_pct=1.0, detected_at=NOW,
    )


def test_unibet_and_711_same_price_merge_into_one():
    merged = merge_twin_book_value_bets([_vb(Book.UNIBET_BE), _vb(Book.SEVEN_ELEVEN_BE)])
    assert len(merged) == 1
    b = merged[0]
    assert b.book == Book.UNIBET_BE                      # primary kept
    assert b.also_books == (Book.SEVEN_ELEVEN_BE,)       # twin rides along


def test_merge_renders_both_book_names():
    merged = merge_twin_book_value_bets([_vb(Book.UNIBET_BE), _vb(Book.SEVEN_ELEVEN_BE)])
    msg = format_value_bet(merged[0])
    assert "Unibet / 711" in msg


def test_different_price_does_not_merge():
    merged = merge_twin_book_value_bets([_vb(Book.UNIBET_BE, odd=2.40),
                                         _vb(Book.SEVEN_ELEVEN_BE, odd=2.30)])
    assert len(merged) == 2
    assert all(b.also_books == () for b in merged)


def test_solo_twin_book_passes_through():
    merged = merge_twin_book_value_bets([_vb(Book.SEVEN_ELEVEN_BE)])
    assert len(merged) == 1
    assert merged[0].book == Book.SEVEN_ELEVEN_BE
    assert merged[0].also_books == ()


def test_non_twin_books_untouched():
    bets = [_vb(Book.BETFIRST), _vb(Book.LADBROKES_BE)]
    merged = merge_twin_book_value_bets(bets)
    assert len(merged) == 2
    assert all(b.also_books == () for b in merged)
