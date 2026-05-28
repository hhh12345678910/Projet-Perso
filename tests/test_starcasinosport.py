from __future__ import annotations

import json
from pathlib import Path

from src.models import Book, MarketType
from src.scrapers.starcasinosport import StarCasinoSportScraper, parse_get_events


# StarSport reuses the Altenar response shape, so the existing Golden Palace
# fixture exercises the parser. Only thing we verify here is that the book
# tag flips correctly and the scraper carries the StarSport integration.
GP_FIXTURE = Path(__file__).parent / "fixtures" / "goldenpalace_events_sample.json"


def test_scraper_advertises_starcasino_integration():
    assert StarCasinoSportScraper.book == Book.STARCASINO_SPORT
    assert StarCasinoSportScraper.integration == "starcasino.be"


def test_parse_get_events_tags_quotes_as_starcasino_sport():
    payload = json.loads(GP_FIXTURE.read_text())
    quotes = list(parse_get_events(payload))
    assert quotes
    assert all(q.book == Book.STARCASINO_SPORT for q in quotes)
    # And the parsing semantics are unchanged from Golden Palace.
    h2h_labels = {q.outcome.label for q in quotes if q.market == MarketType.H2H}
    assert h2h_labels == {"home", "draw", "away"}
