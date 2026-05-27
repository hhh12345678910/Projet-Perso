from __future__ import annotations

import json
from pathlib import Path

from src.models import Book, MarketType
from src.scrapers.unibet import _line, _odd, parse_listview


FIXTURE = Path(__file__).parent / "fixtures" / "unibet_listview_sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_odd_and_line_scaling():
    assert _odd(1350) == 1.35
    assert _odd(61000) == 61.0
    assert _odd(None) is None
    assert _odd("garbage") is None
    assert _line(2500) == 2.5
    assert _line(None) is None


def test_parse_listview_h2h_and_totals():
    quotes = list(parse_listview(_load()))
    by = {(q.event_key, q.market, q.outcome.label): q for q in quotes}

    # First event: full 1X2 + O/U.
    ek = next(q.event_key for q in quotes if "heips" in q.event_key)
    assert by[(ek, MarketType.H2H, "home")].decimal_odd == 61.0
    assert by[(ek, MarketType.H2H, "draw")].decimal_odd == 10.0
    assert by[(ek, MarketType.H2H, "away")].decimal_odd == 1.05
    assert by[(ek, MarketType.TOTALS, "over")].decimal_odd == 1.75
    assert by[(ek, MarketType.TOTALS, "over")].outcome.line == 1.5


def test_parse_listview_skips_null_odds():
    quotes = list(parse_listview(_load()))
    vestri = [q for q in quotes if "vestri" in q.event_key]
    # OT_TWO has null odds -> dropped; home + draw + over + under remain.
    labels = sorted((q.market.value, q.outcome.label) for q in vestri)
    assert ("h2h", "away") not in labels
    assert ("h2h", "home") in labels


def test_parse_listview_sets_book():
    quotes = list(parse_listview(_load()))
    assert quotes and all(q.book == Book.UNIBET_BE for q in quotes)


def test_parse_listview_handles_empty():
    assert list(parse_listview({})) == []
    assert list(parse_listview({"events": []})) == []
