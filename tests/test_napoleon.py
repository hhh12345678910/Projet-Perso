from __future__ import annotations

import json
from pathlib import Path

from src.models import Book, MarketType
from src.scrapers.napoleon import parse_by_date, _odd

FIXTURE = Path(__file__).parent / "fixtures" / "napoleon_by_date_sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_odd_guard():
    assert _odd(3.65) == 3.65
    assert _odd(1.0) is None
    assert _odd("x") is None
    assert _odd(None) is None


def test_parse_by_date_yields_napoleon_1x2():
    quotes = list(parse_by_date(_load()))
    assert quotes
    assert all(q.book == Book.NAPOLEON_BE for q in quotes)
    assert all(q.market == MarketType.H2H for q in quotes)
    # 1X2 codes map to home/draw/away.
    labels = {q.outcome.label for q in quotes}
    assert labels <= {"home", "draw", "away"}
    assert "home" in labels and "away" in labels


def test_event_keys_and_odds_sane():
    quotes = list(parse_by_date(_load()))
    assert all("::" in q.event_key for q in quotes)
    assert all(q.decimal_odd > 1.0 for q in quotes)
    # matchName "home·away" splits cleanly into the event key.
    assert all("__vs__" in q.event_key for q in quotes)
