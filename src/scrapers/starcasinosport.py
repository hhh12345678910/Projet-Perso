from __future__ import annotations

# StarCasino Sport (starsport.be) runs on the same Altenar B2B sportsbook as
# Golden Palace, so the GoldenPalaceScraper class + parse_get_events parser
# are reused unchanged — only the operator integration code differs.
from ..models import Book
from .goldenpalace import GoldenPalaceScraper, parse_get_events as _parse_get_events


class StarCasinoSportScraper(GoldenPalaceScraper):
    book = Book.STARCASINO_SPORT
    integration = "starcasino.be"
    origin = "https://starsport.be"


def parse_get_events(payload: dict):
    return _parse_get_events(payload, book=Book.STARCASINO_SPORT)
