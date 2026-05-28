from __future__ import annotations

import json
from pathlib import Path

import httpx

from src.models import Book, MarketType
from src.scrapers.goldenpalace import (
    _extract_line,
    _is_retryable,
    _market_type,
    parse_get_events,
)


FIXTURE = Path(__file__).parent / "fixtures" / "goldenpalace_events_sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parse_get_events_emits_1x2_and_totals():
    quotes = list(parse_get_events(_load()))
    assert quotes
    markets = {q.market.value for q in quotes}
    # Only H2H + Totals are mapped; Double Chance / BTTS / etc. are dropped.
    assert markets <= {"h2h", "totals"}
    # Every 1X2 should produce home/draw/away.
    h2h_labels = {q.outcome.label for q in quotes if q.market == MarketType.H2H}
    assert h2h_labels == {"home", "draw", "away"}
    # Every Totals should produce over/under with a line.
    totals = [q for q in quotes if q.market == MarketType.TOTALS]
    assert totals and all(q.outcome.line is not None for q in totals)
    assert {q.outcome.label for q in totals} == {"over", "under"}


def test_parse_get_events_sets_book_and_decimal_odds():
    quotes = list(parse_get_events(_load()))
    assert all(q.book == Book.GOLDEN_PALACE for q in quotes)
    assert all(q.decimal_odd > 1.0 for q in quotes)


def test_parse_get_events_home_then_away_by_competitor_order():
    quotes = list(parse_get_events(_load()))
    # First event in the bundled fixture is "Paris Saint-Germain vs. Arsenal FC"
    # (competitorIds = [PSG, Arsenal] -> home=PSG, away=Arsenal).
    psg_quotes = [q for q in quotes if "parissaintgermain" in q.event_key and "arsenal" in q.event_key]
    assert psg_quotes
    # The 1X2 home odd captured for that event in the fixture is 2.2.
    by = {q.outcome.label: q for q in psg_quotes if q.market == MarketType.H2H}
    assert by["home"].decimal_odd == 2.2
    assert by["draw"].decimal_odd == 3.4
    assert by["away"].decimal_odd == 3.0


def test_parse_get_events_handles_empty():
    assert list(parse_get_events({})) == []
    assert list(parse_get_events({"events": [], "markets": [], "odds": [], "competitors": []})) == []


def test_market_type_by_type_id():
    assert _market_type({"typeId": 1}) == MarketType.H2H
    assert _market_type({"typeId": 18}) == MarketType.TOTALS
    assert _market_type({"typeId": 10}) is None       # Double chance, no Pinnacle reference
    assert _market_type({"typeId": 29}) is None        # BTTS
    assert _market_type({"typeId": None}) is None


def test_extract_line_from_odd_name():
    assert _extract_line("Plus de 2.5") == 2.5
    assert _extract_line("Moins de 3.5") == 3.5
    assert _extract_line("Nul") is None
    assert _extract_line("") is None


def test_is_retryable_only_transient():
    req = httpx.Request("GET", "https://x")
    forbidden = httpx.HTTPStatusError("e", request=req, response=httpx.Response(403, request=req))
    server = httpx.HTTPStatusError("e", request=req, response=httpx.Response(503, request=req))
    assert _is_retryable(httpx.ConnectError("x")) is True
    assert _is_retryable(server) is True
    assert _is_retryable(forbidden) is False
