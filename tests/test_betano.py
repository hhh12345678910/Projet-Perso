from __future__ import annotations

import json
from pathlib import Path

import httpx

from src.models import Book, MarketType
from src.scrapers.betano import (
    BetanoAuthError,
    _extract_home_away,
    _is_retryable,
    _map_market,
    _normalise_outcome_label,
    _parse_cookie_header,
    _parse_datetime,
    parse_overview,
)


FIXTURE = Path(__file__).parent / "fixtures" / "betano_overview_sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parse_overview_extracts_h2h_and_totals():
    quotes = list(parse_overview(_load()))
    assert len(quotes) == 5

    by_market = {(q.market, q.outcome.label): q for q in quotes}
    assert by_market[(MarketType.H2H, "home")].decimal_odd == 2.10
    assert by_market[(MarketType.H2H, "draw")].decimal_odd == 3.40
    assert by_market[(MarketType.H2H, "away")].decimal_odd == 3.80
    assert by_market[(MarketType.TOTALS, "over")].decimal_odd == 2.05
    assert by_market[(MarketType.TOTALS, "under")].decimal_odd == 1.75


def test_parse_overview_sets_book_and_event_key():
    quotes = list(parse_overview(_load()))
    assert all(q.book == Book.BETANO_BE for q in quotes)
    assert all("valencia" in q.event_key for q in quotes)
    assert all("rayovallecano" in q.event_key for q in quotes)


def test_parse_overview_carries_totals_line():
    quotes = list(parse_overview(_load()))
    totals = [q for q in quotes if q.market == MarketType.TOTALS]
    assert totals and all(q.outcome.line == 2.5 for q in totals)


def test_parse_overview_handles_empty():
    assert list(parse_overview({})) == []
    assert list(parse_overview({"events": {}, "markets": {}, "selections": {}})) == []


def test_map_market_french_labels():
    assert _map_market("1X2") == MarketType.H2H
    assert _map_market("Vainqueur du match") == MarketType.H2H
    assert _map_market("Plus/Moins buts") == MarketType.TOTALS
    assert _map_market("Handicap") == MarketType.HANDICAP
    assert _map_market("BTTS") == MarketType.BTTS
    assert _map_market("inconnu") is None


def test_normalise_outcome_label():
    assert _normalise_outcome_label("1", MarketType.H2H) == "home"
    assert _normalise_outcome_label("X", MarketType.H2H) == "draw"
    assert _normalise_outcome_label("Match nul", MarketType.H2H) == "draw"
    assert _normalise_outcome_label("Plus", MarketType.TOTALS) == "over"
    assert _normalise_outcome_label("Moins", MarketType.TOTALS) == "under"


def test_extract_home_away_by_role():
    home, away = _extract_home_away([
        {"name": "Valencia CF", "type": "home"},
        {"name": "Rayo Vallecano", "type": "away"},
    ])
    assert home == "Valencia CF"
    assert away == "Rayo Vallecano"


def test_extract_home_away_positional_fallback():
    home, away = _extract_home_away([{"name": "A"}, {"name": "B"}])
    assert (home, away) == ("A", "B")


def test_parse_datetime_iso_and_epoch():
    assert _parse_datetime("2026-05-17T18:30:00Z").year == 2026
    assert _parse_datetime(1778775134).year >= 2026
    assert _parse_datetime(1778775134000).year >= 2026
    assert _parse_datetime(None) is None
    assert _parse_datetime("garbage") is None


def test_parse_cookie_header():
    c = _parse_cookie_header("a=1; b=2; c = 3")
    assert c == {"a": "1", "b": "2", "c": "3"}


def test_is_retryable_skips_auth_and_4xx():
    req = httpx.Request("GET", "https://x")
    forbidden = httpx.HTTPStatusError(
        "e", request=req, response=httpx.Response(403, request=req)
    )
    server_err = httpx.HTTPStatusError(
        "e", request=req, response=httpx.Response(503, request=req)
    )
    assert _is_retryable(BetanoAuthError("nope")) is False
    assert _is_retryable(forbidden) is False
    assert _is_retryable(server_err) is True
    assert _is_retryable(httpx.ConnectError("boom")) is True
