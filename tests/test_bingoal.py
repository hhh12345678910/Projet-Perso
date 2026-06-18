from __future__ import annotations

import json
from pathlib import Path

from src.models import Book, MarketType
from src.scrapers.bingoal import parse_listview

FIXTURE = Path(__file__).parent / "fixtures" / "bingoal_listview_sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parse_listview_yields_bingoal_h2h_quotes():
    quotes = list(parse_listview(_load()))
    assert quotes, "fixture should yield quotes"
    # Every quote is tagged as Bingoal.
    assert all(q.book == Book.BINGOAL_BE for q in quotes)
    # The Suisse vs Bosnie 1X2 market is present with scaled decimal odds.
    h2h = [q for q in quotes if q.market == MarketType.H2H]
    assert h2h
    home = next(q for q in h2h if q.outcome.label == "home")
    assert home.decimal_odd == 1.52          # 1520 milli-units -> 1.52
    labels = {q.outcome.label for q in h2h}
    assert {"home", "draw", "away"} <= labels


def test_quotes_carry_event_key_and_odds_above_one():
    quotes = list(parse_listview(_load()))
    assert all("::" in q.event_key for q in quotes)
    assert all(q.decimal_odd > 1.0 for q in quotes)
