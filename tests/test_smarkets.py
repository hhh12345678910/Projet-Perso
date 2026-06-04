from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from src.models import Book, MarketType
from src.scrapers.smarkets import (
    _is_retryable,
    _mid_decimal_odd,
    _outcome_label,
    _parse_event_time,
    _parse_line_from_market_name,
    _split_event_name,
    iter_quotes_for_event,
)


def test_mid_decimal_odd_inverts_smarkets_probability_scale():
    # Prices are probability × 10000 — mid (bid+offer)/2 is the fair prob.
    # 2532 / 10000 = 0.2532, 2597 / 10000 = 0.2597, mid = 0.25645 -> odd 3.90.
    odd = _mid_decimal_odd(
        bids=[{"price": 2532, "quantity": 1}],
        offers=[{"price": 2597, "quantity": 1}],
    )
    assert odd == pytest.approx(1.0 / ((2532 + 2597) / 2 / 10000.0), abs=1e-6)


def test_mid_decimal_odd_returns_none_on_empty_book():
    assert _mid_decimal_odd([], [{"price": 2500, "quantity": 1}]) is None
    assert _mid_decimal_odd([{"price": 2500, "quantity": 1}], []) is None
    assert _mid_decimal_odd([], []) is None


def test_mid_decimal_odd_rejects_degenerate_probabilities():
    # A degenerate 100% / 0% probability would map to odd <= 1 or infinity.
    assert _mid_decimal_odd([{"price": 10000, "quantity": 1}], [{"price": 10000, "quantity": 1}]) is None


def test_split_event_name_supports_vs_variants():
    assert _split_event_name("Ivory Coast vs Ecuador") == ("Ivory Coast", "Ecuador")
    assert _split_event_name("Federer vs. Nadal") == ("Federer", "Nadal")
    assert _split_event_name("Madrid v Bayern") == ("Madrid", "Bayern")
    assert _split_event_name("garbage no separator") == (None, None)


def test_parse_line_from_over_under_market_name():
    assert _parse_line_from_market_name("Over/under 2.5") == 2.5
    assert _parse_line_from_market_name("Over/under 188.5") == 188.5
    assert _parse_line_from_market_name("Full-time result") is None


def test_outcome_label_maps_teams_to_home_away():
    assert _outcome_label("Ivory Coast", "Ivory Coast", "Ecuador", MarketType.H2H) == "home"
    assert _outcome_label("Ecuador", "Ivory Coast", "Ecuador", MarketType.H2H) == "away"
    assert _outcome_label("Draw", "Ivory Coast", "Ecuador", MarketType.H2H) == "draw"
    assert _outcome_label("Unknown", "Ivory Coast", "Ecuador", MarketType.H2H) is None
    assert _outcome_label("Over 2.5", None, None, MarketType.TOTALS) == "over"
    assert _outcome_label("Under 2.5", None, None, MarketType.TOTALS) == "under"


def test_parse_event_time_handles_iso_z_suffix():
    t = _parse_event_time("2026-06-14T23:00:00Z")
    assert t == datetime(2026, 6, 14, 23, 0, tzinfo=timezone.utc)
    assert _parse_event_time(None) is None
    assert _parse_event_time("garbage") is None


def _stub_scraper(markets, contracts_by_market, quotes_by_contract):
    sc = MagicMock()
    sc.fetch_event_markets.return_value = markets
    sc.fetch_market_contracts.side_effect = lambda mid: contracts_by_market.get(mid, [])
    sc.fetch_quotes.return_value = quotes_by_contract
    return sc


def test_iter_quotes_yields_3way_winner_and_totals_for_one_event():
    event = {
        "id": 44754796,
        "name": "Ivory Coast vs Ecuador",
        "start_datetime": "2026-06-14T23:00:00Z",
    }
    markets = [
        {
            "id": 119416335,
            "state": "open",
            "name": "Full-time result",
            "market_type": {"name": "WINNER_3_WAY"},
        },
        {
            "id": 119416372,
            "state": "open",
            "name": "Over/under 2.5",
            "market_type": {"name": "OVER_UNDER"},
        },
        {
            "id": 999999,
            "state": "open",
            "name": "Correct score",
            "market_type": {"name": "CORRECT_SCORE"},  # ignored downstream
        },
    ]
    contracts = {
        119416335: [
            {"id": 1, "name": "Ivory Coast"},
            {"id": 2, "name": "Draw"},
            {"id": 3, "name": "Ecuador"},
        ],
        119416372: [
            {"id": 4, "name": "Over 2.5"},
            {"id": 5, "name": "Under 2.5"},
        ],
    }
    quotes = {
        "1": {"bids": [{"price": 2532, "quantity": 1}], "offers": [{"price": 2597, "quantity": 1}]},
        "2": {"bids": [{"price": 3175, "quantity": 1}], "offers": [{"price": 3279, "quantity": 1}]},
        "3": {"bids": [{"price": 3876, "quantity": 1}], "offers": [{"price": 3968, "quantity": 1}]},
        "4": {"bids": [{"price": 5500, "quantity": 1}], "offers": [{"price": 5550, "quantity": 1}]},
        "5": {"bids": [{"price": 4400, "quantity": 1}], "offers": [{"price": 4500, "quantity": 1}]},
    }
    sc = _stub_scraper(markets, contracts, quotes)
    qs = list(iter_quotes_for_event(sc, event))
    # 3 H2H + 2 totals = 5 quotes; the CORRECT_SCORE market is dropped.
    assert len(qs) == 5
    labels = {(q.market.value, q.outcome.label): q for q in qs}
    assert labels[("h2h", "home")].decimal_odd > 1.0
    assert labels[("h2h", "draw")].decimal_odd > 1.0
    assert labels[("h2h", "away")].decimal_odd > 1.0
    assert labels[("totals", "over")].outcome.line == 2.5
    assert labels[("totals", "under")].outcome.line == 2.5
    assert all(q.book == Book.SMARKETS for q in qs)


def test_iter_quotes_skips_event_with_unparseable_name():
    sc = _stub_scraper([], {}, {})
    qs = list(iter_quotes_for_event(sc, {
        "id": 1, "name": "no separator here", "start_datetime": "2026-06-14T23:00:00Z",
    }))
    assert qs == []


def test_is_retryable_only_transient():
    req = httpx.Request("GET", "https://x")
    forbidden = httpx.HTTPStatusError("e", request=req, response=httpx.Response(403, request=req))
    server = httpx.HTTPStatusError("e", request=req, response=httpx.Response(503, request=req))
    assert _is_retryable(httpx.ConnectError("x")) is True
    assert _is_retryable(server) is True
    assert _is_retryable(forbidden) is False
