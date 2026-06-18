from __future__ import annotations

import json
from pathlib import Path

from src.models import Book, MarketType
from src.scrapers.meridianbet import parse_offer, _odd

FIXTURE = Path(__file__).parent / "fixtures" / "meridian_offer_sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_odd_guard():
    assert _odd(2.02) == 2.02
    assert _odd(1.0) is None      # an odd of 1.0 is not a real price
    assert _odd("x") is None
    assert _odd(None) is None


def test_parse_offer_yields_meridian_1x2_and_totals():
    quotes = list(parse_offer(_load()))
    assert quotes
    assert all(q.book == Book.MERIDIAN_BE for q in quotes)

    h2h = [q for q in quotes if q.market == MarketType.H2H]
    labels = {q.outcome.label for q in h2h}
    assert {"home", "draw", "away"} <= labels
    # 1X2 odds come through as plain decimals.
    assert any(abs(q.decimal_odd - 2.02) < 1e-9 for q in h2h)

    totals = [q for q in quotes if q.market == MarketType.TOTALS]
    assert totals
    assert all(q.outcome.line is not None for q in totals)
    assert {"over", "under"} <= {q.outcome.label for q in totals}


def test_event_keys_and_odds_sane():
    quotes = list(parse_offer(_load()))
    assert all("::" in q.event_key for q in quotes)
    assert all(q.decimal_odd > 1.0 for q in quotes)
