from __future__ import annotations

import json
from pathlib import Path

import httpx

from src.models import Book, MarketType
from src.scrapers.betfirst import (
    _extract_home_away,
    _is_retryable,
    _market_type,
    _selection_label,
    parse_events_table,
)


FIXTURE = Path(__file__).parent / "fixtures" / "betfirst_events_table_sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parse_events_table_yields_1x2_with_home_draw_away():
    quotes = list(parse_events_table(_load()))
    assert len(quotes) == 3
    by = {q.outcome.label: q for q in quotes}
    assert by["home"].decimal_odd == 2.02
    assert by["draw"].decimal_odd == 3.25
    assert by["away"].decimal_odd == 3.90
    assert all(q.market == MarketType.H2H for q in quotes)
    assert all(q.book == Book.BETFIRST for q in quotes)


def test_parse_events_table_event_key_uses_home_away_order():
    quotes = list(parse_events_table(_load()))
    # Nice (side=1) is home, Saint-Etienne (side=2) is away.
    assert all("nice" in q.event_key for q in quotes)
    assert all("saintetienne" in q.event_key for q in quotes)
    assert all(q.event_key.startswith("202605291845") for q in quotes)


def test_parse_events_table_handles_empty():
    assert list(parse_events_table({})) == []
    assert list(parse_events_table({"data": {"events": [], "markets": [], "selections": []}})) == []


def test_market_type_by_template_id():
    assert _market_type({"marketTemplateId": "MW3W"}) == MarketType.H2H
    assert _market_type({"marketTemplateId": "MW2W"}) == MarketType.H2H
    assert _market_type({"marketTemplateId": "MTG2W"}) == MarketType.TOTALS
    assert _market_type({"marketTemplateId": "HCAP"}) == MarketType.HANDICAP
    assert _market_type({"marketTemplateId": "BTTS"}) is None       # Pinnacle doesn't price BTTS
    assert _market_type({"marketTemplateId": "DC"}) is None          # double chance
    assert _market_type({"marketTemplateId": None}) is None


def test_selection_label_uses_template_id():
    assert _selection_label({"selectionTemplateId": "HOME"}) == "home"
    assert _selection_label({"selectionTemplateId": "DRAW"}) == "draw"
    assert _selection_label({"selectionTemplateId": "AWAY"}) == "away"
    assert _selection_label({"selectionTemplateId": "OVER"}) == "over"
    assert _selection_label({"selectionTemplateId": "UNDER"}) == "under"
    # Unknown template falls back to isHomeTeam flag.
    assert _selection_label({"selectionTemplateId": "X", "isHomeTeam": True}) == "home"
    assert _selection_label({"selectionTemplateId": "X", "isHomeTeam": False}) == "away"


def test_extract_home_away_by_side():
    home, away = _extract_home_away([
        {"label": "Nice", "side": 1},
        {"label": "Saint-Etienne", "side": 2},
    ])
    assert home == "Nice"
    assert away == "Saint-Etienne"


def test_extract_home_away_positional_fallback():
    home, away = _extract_home_away([{"label": "A"}, {"label": "B"}])
    assert (home, away) == ("A", "B")


def test_is_retryable_only_transient():
    req = httpx.Request("GET", "https://x")
    forbidden = httpx.HTTPStatusError("e", request=req, response=httpx.Response(403, request=req))
    server = httpx.HTTPStatusError("e", request=req, response=httpx.Response(503, request=req))
    assert _is_retryable(httpx.ConnectError("boom")) is True
    assert _is_retryable(server) is True
    assert _is_retryable(forbidden) is False
