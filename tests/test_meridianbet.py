from __future__ import annotations

import json
from pathlib import Path

from src.models import Book, MarketType
from src.scrapers.meridianbet import MeridianScraper, parse_offer, _odd

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


def test_fetch_all_events_starts_at_page_zero():
    """Regression: the offer endpoint is 0-indexed. fetch_all_events used to
    start at page 1, silently dropping the whole first page of leagues. It must
    request page 0 first, then keep paging until a page is empty."""
    requested_pages: list[int] = []

    # Page 0 and 1 carry distinct leagues; page 2 is empty (stop signal).
    pages = {
        0: {"payload": {"leagues": [{"leagueName": "L0", "events": []}]}},
        1: {"payload": {"leagues": [{"leagueName": "L1", "events": []}]}},
        2: {"payload": {"leagues": []}},
    }

    scraper = MeridianScraper.__new__(MeridianScraper)  # skip httpx client setup

    def fake_fetch(sport, page):
        requested_pages.append(page)
        return pages.get(page, {"payload": {"leagues": []}})

    scraper.fetch_offer_page = fake_fetch  # type: ignore[method-assign]
    merged = scraper.fetch_all_events("soccer")

    assert requested_pages[0] == 0                     # page 0 fetched first
    assert requested_pages == [0, 1, 2]                # stops on the empty page
    names = {lg["leagueName"] for lg in merged["payload"]["leagues"]}
    assert names == {"L0", "L1"}                       # page-0 leagues retained
