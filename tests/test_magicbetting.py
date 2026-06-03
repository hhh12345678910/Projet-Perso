from __future__ import annotations

import json
from pathlib import Path

import httpx

from src.models import Book, MarketType
from src.scrapers.magicbetting import (
    _is_retryable,
    decode_response,
    parse_events,
)


FIXTURE = Path(__file__).parent / "fixtures" / "magicbetting_decoded_sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_decode_response_xor_with_null_padding():
    # XOR each byte of "Football" with 0x02, then sprinkle a \x00 in the middle.
    encoded = bytes(b ^ 0x02 for b in b'{"x":"Football"}')
    encoded_with_pad = encoded[:5] + b"\x00" + encoded[5:]
    assert decode_response(encoded_with_pad) == {"x": "Football"}


def test_parse_events_emits_1x2_with_correct_labels():
    quotes = list(parse_events(_load()))
    mexico = [q for q in quotes if "mexico" in q.event_key]
    by = {(q.market, q.outcome.label): q for q in mexico if q.market == MarketType.H2H}
    # Real captured Mexico - South Africa odds.
    assert by[(MarketType.H2H, "home")].decimal_odd == 1.37
    assert by[(MarketType.H2H, "draw")].decimal_odd == 4.09
    assert by[(MarketType.H2H, "away")].decimal_odd == 6.75


def test_parse_events_totals_carries_line():
    quotes = list(parse_events(_load()))
    totals = [q for q in quotes if q.market == MarketType.TOTALS]
    assert totals
    assert all(q.outcome.line is not None for q in totals)
    assert all(q.outcome.label in ("over", "under") for q in totals)
    # The captured event has alternative lines at 0.5, 1, 1.5, ...
    lines = {q.outcome.line for q in totals}
    assert {0.5, 1.0, 1.5}.issubset(lines)


def test_parse_events_handicap_signed_line():
    quotes = list(parse_events(_load()))
    hc = [q for q in quotes if q.market == MarketType.HANDICAP]
    assert hc
    # BetConstruct already signs each side of a handicap line correctly: for
    # every (event, |line|) pair, the home/away lines are opposite signs.
    by_abs: dict = {}
    for q in hc:
        if q.outcome.line is None:
            continue
        by_abs.setdefault((q.event_key, abs(q.outcome.line)), {})[q.outcome.label] = q.outcome.line
    for pair in by_abs.values():
        if "home" in pair and "away" in pair:
            assert pair["home"] == -pair["away"]


def test_parse_events_sets_book_tag():
    quotes = list(parse_events(_load()))
    assert quotes and all(q.book == Book.MAGIC_BETTING for q in quotes)


def test_parse_events_handles_empty():
    assert list(parse_events({})) == []
    assert list(parse_events({"events": []})) == []


def test_is_retryable_only_transient():
    req = httpx.Request("GET", "https://x")
    forbidden = httpx.HTTPStatusError("e", request=req, response=httpx.Response(403, request=req))
    server = httpx.HTTPStatusError("e", request=req, response=httpx.Response(503, request=req))
    assert _is_retryable(httpx.ConnectError("x")) is True
    assert _is_retryable(server) is True
    assert _is_retryable(forbidden) is False
