from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..filter import is_noise_event
from ..matcher import event_key
from ..models import Book, MarketType, OddQuote, Outcome
from ..teams import record_pair


# Bwin Belgium uses the Entain CDS API.
# The access token is bound to the site's JS and must be captured from browser
# DevTools (Network tab) — set it as BWIN_ACCESS_ID in your .env.
BASE = "https://cds-api.bwin.com/bettingoffer/fixtures"

SPORT_IDS = {
    "soccer": "4",
    "tennis": "5",
    "basketball": "7",
    "hockey": "12",
}

_H2H_RE = re.compile(r"1\s*[Xx×]\s*2|Match\s+Result|Résultat", re.IGNORECASE)
_TOTALS_RE = re.compile(r"Total\s+Goals?|Over\s*/\s*Under|Plus\s*/\s*[Mm]oins", re.IGNORECASE)
_HANDICAP_RE = re.compile(r"(?<!\w)Handicap|Asian\s+Handicap", re.IGNORECASE)

_OUTCOME_LABELS: dict[str, str] = {
    "1": "home", "home": "home",
    "x": "draw", "draw": "draw",
    "2": "away", "away": "away",
    "over": "over",
    "under": "under",
}


def _name_str(v: Any) -> str:
    """Entain CDS name fields can be plain strings or {'value': '...'}."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return v.get("value") or v.get("short") or ""
    return ""


def _headers(access_id: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.8",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "Origin": "https://sports.bwin.be",
        "Referer": "https://sports.bwin.be/",
        "x-bwin-accessid": access_id,
    }


def _parse_datetime(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _market_type(game_name: str) -> MarketType | None:
    if _H2H_RE.search(game_name):
        return MarketType.H2H
    if _TOTALS_RE.search(game_name):
        return MarketType.TOTALS
    if _HANDICAP_RE.search(game_name):
        return MarketType.HANDICAP
    return None


def _extract_line(game_name: str) -> float | None:
    """Pull the numeric line from a market name like 'Over/Under 2.5'."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*$", game_name)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    return None


class BwinScraper:
    book = Book.BWIN_BE

    def __init__(self, access_id: str, timeout: float = 15.0):
        self._access_id = access_id
        self._client = httpx.Client(timeout=timeout, headers=_headers(access_id))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BwinScraper":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def fetch_fixtures(self, sport: str = "soccer") -> dict:
        sport_id = SPORT_IDS.get(sport, "4")
        r = self._client.get(
            BASE,
            params={
                "x-bwin-accessid": self._access_id,
                "lang": "fr",
                "country": "BE",
                "userCountry": "BE",
                "fixtureTypes": "Standard",
                "state": "Latest",
                "offerMapping": "Filtered",
                "offerCategories": "Gridable",
                "fixtureCategories": "Gridable",
                "sportIds": sport_id,
                "skip": "0",
                "take": "1000",
                "sortBy": "Tags",
            },
        )
        r.raise_for_status()
        return r.json()


def parse_fixtures(data: dict) -> Iterator[OddQuote]:
    """Walk a Bwin CDS fixtures payload and yield OddQuote objects."""
    now = datetime.now(timezone.utc)
    for fix in data.get("fixtures") or []:
        participants = fix.get("participants") or []
        if len(participants) < 2:
            continue
        home = _name_str(participants[0].get("name"))
        away = _name_str(participants[1].get("name"))

        start = _parse_datetime(fix.get("startDate"))
        if not (home and away and start):
            continue
        if is_noise_event(home, away):
            continue
        record_pair(home, away)
        ek = event_key(home, away, start)
        source_id = str(fix.get("id", ""))

        for game in fix.get("games") or []:
            game_name = _name_str(game.get("name"))
            if not game_name:
                continue
            mtype = _market_type(game_name)
            if mtype is None:
                continue

            line: float | None = None
            if mtype in (MarketType.TOTALS, MarketType.HANDICAP):
                line = _extract_line(game_name)
                if line is None:
                    try:
                        line = float(game.get("attr") or "")
                    except (TypeError, ValueError):
                        pass

            for res in game.get("results") or []:
                try:
                    odd = float(res.get("odds") or res.get("price") or 0)
                except (TypeError, ValueError):
                    continue
                if odd <= 1.0:
                    continue

                result_name = _name_str(res.get("name") or res.get("id") or "").lower().strip()
                label = _OUTCOME_LABELS.get(result_name)
                if label is None:
                    continue

                yield OddQuote(
                    event_key=ek,
                    book=Book.BWIN_BE,
                    market=mtype,
                    outcome=Outcome(label=label, line=line),
                    decimal_odd=odd,
                    fetched_at=now,
                    source_event_id=source_id,
                )


def build_scraper() -> "BwinScraper | None":
    """Construct a BwinScraper from env, or None when BWIN_ACCESS_ID is not set."""
    access_id = os.getenv("BWIN_ACCESS_ID")
    if not access_id:
        return None
    return BwinScraper(access_id=access_id)
