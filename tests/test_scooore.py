from __future__ import annotations

import json
from pathlib import Path

from src.models import Book, MarketType
from src.scrapers.scooore import parse_listview

# Scooore is the same Kambi listView shape as Bingoal — reuse that fixture.
FIXTURE = Path(__file__).parent / "fixtures" / "bingoal_listview_sample.json"


def test_parse_listview_tags_scooore():
    quotes = list(parse_listview(json.loads(FIXTURE.read_text(encoding="utf-8"))))
    assert quotes
    assert all(q.book == Book.SCOOORE_BE for q in quotes)
    h2h = [q for q in quotes if q.market == MarketType.H2H]
    assert h2h and {"home", "away"} <= {q.outcome.label for q in h2h}
    assert all(q.decimal_odd > 1.0 for q in quotes)
    assert all("::" in q.event_key for q in quotes)
