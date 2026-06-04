from __future__ import annotations

import json
from pathlib import Path

import httpx

from src.models import Book, MarketType
from src.scrapers.ladbrokes import (
    _extract_line,
    _home_away,
    _is_retryable,
    _market_type,
    _odd_from_int,
    _selection_label,
    parse_prematch,
)


FIXTURE = Path(__file__).parent / "fixtures" / "ladbrokes_next_sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parse_prematch_yields_h2h_and_totals():
    quotes = list(parse_prematch(_load()))
    # 11 football events × (3 home/draw/away + 2 over/under) = 55 quotes max,
    # but only live=False events count.
    assert quotes
    by_market = {q.market.value for q in quotes}
    assert by_market == {"h2h", "totals"}
    # Every 1X2 has home/draw/away set, every totals has over/under.
    h2h_labels = {q.outcome.label for q in quotes if q.market == MarketType.H2H}
    assert h2h_labels == {"home", "draw", "away"}
    totals_labels = {q.outcome.label for q in quotes if q.market == MarketType.TOTALS}
    assert totals_labels == {"over", "under"}


def test_parse_prematch_sets_book_and_decimal_odds():
    quotes = list(parse_prematch(_load()))
    assert all(q.book == Book.LADBROKES_BE for q in quotes)
    # All Eurobet odds should be > 1.0 after the /100 conversion.
    assert all(q.decimal_odd > 1.0 for q in quotes)


def test_parse_prematch_carries_totals_line():
    quotes = list(parse_prematch(_load()))
    totals = [q for q in quotes if q.market == MarketType.TOTALS]
    assert totals and all(q.outcome.line is not None for q in totals)
    # The standard "Plus/Moins de buts 2.5" should appear.
    assert any(q.outcome.line == 2.5 for q in totals)


def test_parse_prematch_skips_live_and_non_football():
    payload = {
        "result": {
            "events": [
                {  # Live -> skipped
                    "eventInfo": {
                        "disciplineDescription": "FOOTBALL",
                        "eventDescription": "A - B",
                        "eventData": 1779998400000,
                        "live": True,
                        "teamHome": {"description": "A"},
                        "teamAway": {"description": "B"},
                    },
                    "betGroupList": [{"oddGroupList": [{"betId": 24, "oddList": [{"boxTitle": "1", "oddValue": 200}]}]}],
                },
                {  # Non-football -> skipped
                    "eventInfo": {
                        "disciplineDescription": "TENNIS",
                        "eventDescription": "A - B",
                        "eventData": 1779998400000,
                        "live": False,
                        "teamHome": {"description": "A"},
                        "teamAway": {"description": "B"},
                    },
                    "betGroupList": [{"oddGroupList": [{"betId": 24, "oddList": [{"boxTitle": "1", "oddValue": 200}]}]}],
                },
            ]
        }
    }
    # Without a sport filter the live event is still skipped; the tennis event
    # only gets dropped when we explicitly scope to football.
    assert list(parse_prematch(payload, sport_description="FOOTBALL")) == []


def test_parse_prematch_handles_empty():
    assert list(parse_prematch({})) == []
    assert list(parse_prematch({"result": {"events": []}})) == []


def test_odd_from_int_scaling():
    assert _odd_from_int(240) == 2.40
    assert _odd_from_int(625) == 6.25
    assert _odd_from_int(None) is None
    assert _odd_from_int("garbage") is None


def test_market_type_by_bet_id():
    assert _market_type({"betId": 24}) == MarketType.H2H
    assert _market_type({"betId": 4243}) == MarketType.TOTALS
    assert _market_type({"betId": 1550}) is None        # BTTS, not Pinnacle-priced
    assert _market_type({"betId": 9999, "alternativeDescription": "1X2"}) == MarketType.H2H
    assert _market_type({"betId": 9999}) is None


def test_selection_label_french_and_english():
    assert _selection_label("1", MarketType.H2H) == "home"
    assert _selection_label("X", MarketType.H2H) == "draw"
    assert _selection_label("2", MarketType.H2H) == "away"
    assert _selection_label("PLUS DE", MarketType.TOTALS) == "over"
    assert _selection_label("MOINS DE", MarketType.TOTALS) == "under"
    assert _selection_label("OUI", MarketType.H2H) is None


def test_extract_line_from_group_description():
    assert _extract_line(
        {"oddGroupDescription": "PLUS/MOINS DE BUTS 2.5"}, MarketType.TOTALS
    ) == 2.5
    assert _extract_line({"oddGroupDescription": "RÉSULTAT FINAL"}, MarketType.H2H) is None
    assert _extract_line({"oddGroupDescription": ""}, MarketType.TOTALS) is None


def test_home_away_fallback_from_description():
    home, away = _home_away({"eventDescription": "Real Madrid - Barcelone"})
    assert home == "Real Madrid"
    assert away == "Barcelone"


def test_is_retryable_only_transient():
    req = httpx.Request("GET", "https://x")
    forbidden = httpx.HTTPStatusError("e", request=req, response=httpx.Response(403, request=req))
    server = httpx.HTTPStatusError("e", request=req, response=httpx.Response(503, request=req))
    assert _is_retryable(httpx.ConnectError("x")) is True
    assert _is_retryable(server) is True
    assert _is_retryable(forbidden) is False
