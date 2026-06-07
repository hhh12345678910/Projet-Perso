from __future__ import annotations

import httpx
import pytest

from src.scrapers.pinnacle import _american_to_decimal, _is_retryable


def test_american_to_decimal_negative():
    assert _american_to_decimal(-158) == pytest.approx(1.0 + 100 / 158)
    assert _american_to_decimal(-100) == pytest.approx(2.0)


def test_american_to_decimal_positive():
    assert _american_to_decimal(289) == pytest.approx(3.89)
    assert _american_to_decimal(100) == pytest.approx(2.0)


def test_american_to_decimal_invalid():
    assert _american_to_decimal(0) is None
    assert _american_to_decimal(None) is None
    assert _american_to_decimal("x") is None


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://x")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError("e", request=req, response=resp)


def test_fetch_market_quotes_skips_non_full_match_periods():
    # Pinnacle returns per-period markets sharing the same matchupId. Only
    # period 0 (full match) should make it through to OddQuote — anything
    # else (1st half, 2nd half, ...) would contaminate the (event, market,
    # line) groups and break the fair-line devig.
    from src.scrapers.pinnacle import PinnacleScraper
    from unittest.mock import patch

    matchup_id = 999
    fake_leagues = [{"id": 1, "name": "Test League"}]
    fake_matchups = [{
        "id": matchup_id,
        "startTime": "2026-06-01T20:00:00Z",
        "participants": [
            {"name": "Team A", "alignment": "home"},
            {"name": "Team B", "alignment": "away"},
        ],
    }]
    fake_markets = [
        # Full match moneyline — should emerge.
        {"status": "open", "matchupId": matchup_id, "type": "moneyline",
         "period": 0, "prices": [
            {"designation": "home", "price": -150},
            {"designation": "draw", "price": +350},
            {"designation": "away", "price": +400},
         ]},
        # 1st half moneyline at the same matchup — must be dropped.
        {"status": "open", "matchupId": matchup_id, "type": "moneyline",
         "period": 1, "prices": [
            {"designation": "home", "price": +200},
            {"designation": "draw", "price": +100},
            {"designation": "away", "price": +800},
         ]},
        # 2nd half — dropped too.
        {"status": "open", "matchupId": matchup_id, "type": "moneyline",
         "period": 2, "prices": [
            {"designation": "home", "price": +180},
            {"designation": "draw", "price": +120},
            {"designation": "away", "price": +700},
         ]},
    ]

    def fake_get(self, path, params=None):
        if "leagues" in path and "matchups" in path:
            return fake_matchups
        if "markets/straight" in path:
            return fake_markets
        if "/leagues" in path:
            return fake_leagues
        return []

    with patch.object(PinnacleScraper, "_get", fake_get):
        sc = PinnacleScraper(request_delay=0)
        quotes = list(sc.fetch_market_quotes("soccer"))
        sc.close()

    # Exactly 3 quotes (home/draw/away) from period 0 only — no period
    # 1 or 2 leakage into the same matchup.
    assert len(quotes) == 3
    labels = {q.outcome.label for q in quotes}
    assert labels == {"home", "draw", "away"}
    # Sanity: the odds match the period-0 American prices.
    home = next(q for q in quotes if q.outcome.label == "home")
    assert home.decimal_odd < 2.0   # American -150 -> ~1.67


def test_is_retryable_only_transient():
    assert _is_retryable(httpx.ConnectError("boom")) is True
    assert _is_retryable(_status_error(429)) is True
    assert _is_retryable(_status_error(503)) is True
    assert _is_retryable(_status_error(403)) is False
    assert _is_retryable(_status_error(404)) is False
