from __future__ import annotations

import json
from pathlib import Path

import httpx

from src.models import Book, MarketType
from src.scrapers.betano import (
    BetanoAuthError,
    _extract_home_away,
    _h2h_label,
    _is_retryable,
    _market_type,
    _normalise_outcome_label,
    _parse_cookie_header,
    _parse_datetime,
    _side_from_team,
    parse_overview,
)


FIXTURE = Path(__file__).parent / "fixtures" / "betano_overview_sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parse_overview_extracts_1x2_with_correct_labels():
    quotes = list(parse_overview(_load()))
    catania = {
        (q.market, q.outcome.label): q
        for q in quotes
        if "calciocatania" in q.event_key
    }
    assert catania[(MarketType.H2H, "home")].decimal_odd == 1.45
    assert catania[(MarketType.H2H, "draw")].decimal_odd == 3.65
    assert catania[(MarketType.H2H, "away")].decimal_odd == 8.25
    assert catania[(MarketType.TOTALS, "over")].decimal_odd == 2.22
    assert catania[(MarketType.TOTALS, "under")].decimal_odd == 1.57


def test_parse_overview_carries_totals_line():
    totals = [q for q in parse_overview(_load()) if q.market == MarketType.TOTALS]
    assert totals and all(q.outcome.line == 2.5 for q in totals)


def test_parse_overview_handicap_labels_and_signed_line():
    hc = {q.outcome.label: q for q in parse_overview(_load()) if q.market == MarketType.HANDICAP}
    assert hc["home"].outcome.line == -1.5
    assert hc["away"].outcome.line == 1.5
    assert hc["home"].decimal_odd == 3.7


def test_parse_overview_two_way_winner_mapped_to_home_away():
    # H2HT market labelled with team names -> resolved to home/away.
    monaco = {q.outcome.label: q for q in parse_overview(_load()) if "monaco" in q.event_key}
    assert monaco["home"].decimal_odd == 5.8   # Bourg-en-Bresse (isHome)
    assert monaco["away"].decimal_odd == 1.11  # Monaco
    assert "draw" not in monaco


def test_parse_overview_sets_book():
    quotes = list(parse_overview(_load()))
    assert quotes and all(q.book == Book.BETANO_BE for q in quotes)


def test_parse_overview_handles_empty():
    assert list(parse_overview({})) == []
    assert list(parse_overview({"events": {}, "markets": {}, "selections": {}})) == []


def test_market_type_by_code():
    assert _market_type({"type": "MRES"}) == MarketType.H2H
    assert _market_type({"type": "H2HT"}) == MarketType.H2H
    assert _market_type({"type": "HCTG"}) == MarketType.TOTALS
    assert _market_type({"type": "HCAP"}) == MarketType.HANDICAP
    assert _market_type({"type": "DNOB"}) is None       # draw-no-bet, not moneyline
    assert _market_type({"type": "BTSC"}) is None        # BTTS, no Pinnacle reference
    assert _market_type({"type": None}) is None


def test_h2h_label_direct_and_by_team():
    assert _h2h_label("1", "Catania", "Ascoli") == "home"
    assert _h2h_label("X", "Catania", "Ascoli") == "draw"
    assert _h2h_label("2", "Catania", "Ascoli") == "away"
    assert _h2h_label("Monaco", "Bourg-en-Bresse", "Monaco") == "away"


def test_side_from_team():
    assert _side_from_team("Toronto Blue Jays -1.5", "Toronto Blue Jays", "Miami Marlins") == "home"
    assert _side_from_team("Miami Marlins +1.5", "Toronto Blue Jays", "Miami Marlins") == "away"
    assert _side_from_team("Unrelated FC", "Toronto Blue Jays", "Miami Marlins") is None


def test_normalise_outcome_label():
    assert _normalise_outcome_label("Plus de 2.5", MarketType.TOTALS) == "over"
    assert _normalise_outcome_label("Moins de 2.5", MarketType.TOTALS) == "under"


def test_extract_home_away_by_is_home_flag():
    home, away = _extract_home_away([
        {"name": "Calcio Catania", "isHome": True},
        {"name": "Ascoli Calcio 1898"},
    ])
    assert home == "Calcio Catania"
    assert away == "Ascoli Calcio 1898"


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
