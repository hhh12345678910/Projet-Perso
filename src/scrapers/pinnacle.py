from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Iterable, Iterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..matcher import event_key
from ..models import Book, Event, MarketType, OddQuote, Outcome


PINNACLE_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"

# Public frontend key (used by their browser app). Override via env if needed.
PINNACLE_API_KEY = os.getenv(
    "PINNACLE_API_KEY",
    "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R",
)

SPORT_IDS = {
    "soccer": 29,
    "tennis": 33,
    "basketball": 4,
}


def _is_retryable(exc: BaseException) -> bool:
    """Retry transient failures only — network errors, 429, and 5xx. A 403/404
    won't fix itself on retry (and retrying a 403 just deepens a rate-limit ban)."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _american_to_decimal(price: object) -> float | None:
    """Pinnacle's arcadia API returns moneyline prices as American odds
    (e.g. -158, +289). Convert to decimal odds for the rest of the engine."""
    try:
        a = float(price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    return 1.0 + (a / 100.0 if a > 0 else 100.0 / abs(a))


def _headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.pinnacle.com/",
        "Origin": "https://www.pinnacle.com",
        "X-API-Key": PINNACLE_API_KEY,
        "Accept": "application/json",
    }


class PinnacleScraper:
    book = Book.PINNACLE

    def __init__(self, timeout: float = 10.0, request_delay: float | None = None):
        self._client = httpx.Client(timeout=timeout, headers=_headers())
        # Light throttle between requests to stay under Pinnacle's rate limit.
        self._delay = request_delay if request_delay is not None else float(
            os.getenv("PINNACLE_REQUEST_DELAY", "0.3")
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PinnacleScraper":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    def _get(self, path: str, params: dict | None = None) -> list | dict:
        if self._delay:
            time.sleep(self._delay)
        r = self._client.get(f"{PINNACLE_BASE}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def list_leagues(self, sport: str) -> list[dict]:
        sport_id = SPORT_IDS[sport]
        data = self._get(f"/sports/{sport_id}/leagues", {"all": "false", "brandId": "0"})
        return data if isinstance(data, list) else []

    def list_matchups(self, league_id: int) -> list[dict]:
        data = self._get(f"/leagues/{league_id}/matchups")
        return [m for m in (data if isinstance(data, list) else []) if not m.get("parent")]

    def list_straight_markets(self, league_id: int) -> list[dict]:
        data = self._get(f"/leagues/{league_id}/markets/straight")
        return data if isinstance(data, list) else []

    def fetch_events(self, sport: str) -> Iterator[Event]:
        for league in self.list_leagues(sport):
            league_id = league.get("id")
            league_name = league.get("name", "?")
            if not league_id:
                continue
            try:
                matchups = self.list_matchups(league_id)
            except httpx.HTTPError:
                continue
            for m in matchups:
                participants = m.get("participants") or []
                if len(participants) < 2:
                    continue
                home = next((p["name"] for p in participants if p.get("alignment") == "home"), None)
                away = next((p["name"] for p in participants if p.get("alignment") == "away"), None)
                if not home or not away:
                    continue
                start_raw = m.get("startTime")
                if not start_raw:
                    continue
                start = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
                yield Event(
                    sport=sport,
                    league=league_name,
                    home=home,
                    away=away,
                    start_time=start,
                    source_id=str(m["id"]),
                    book=Book.PINNACLE,
                )

    def fetch_quotes(self, event: Event) -> Iterable[OddQuote]:
        # Cheaper to fetch all markets per league once; kept per-event for interface symmetry.
        # See orchestrator for the batch path.
        return []

    def fetch_market_quotes(self, sport: str) -> Iterator[OddQuote]:
        now = datetime.now(timezone.utc)
        for league in self.list_leagues(sport):
            league_id = league.get("id")
            if not league_id:
                continue
            try:
                matchups_by_id = {m["id"]: m for m in self.list_matchups(league_id)}
                markets = self.list_straight_markets(league_id)
            except httpx.HTTPError:
                # One league being denied/rate-limited shouldn't abort the whole scan.
                continue
            for market in markets:
                if market.get("status") != "open":
                    continue
                matchup_id = market.get("matchupId")
                matchup = matchups_by_id.get(matchup_id)
                if not matchup:
                    continue
                participants = matchup.get("participants") or []
                home = next((p["name"] for p in participants if p.get("alignment") == "home"), None)
                away = next((p["name"] for p in participants if p.get("alignment") == "away"), None)
                start_raw = matchup.get("startTime")
                if not (home and away and start_raw):
                    continue
                start = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
                ek = event_key(home, away, start)

                market_type = self._map_market(market.get("type"))
                if market_type is None:
                    continue

                for p in market.get("prices") or []:
                    if p.get("points") is not None and market_type == MarketType.H2H:
                        continue
                    decimal_odd = _american_to_decimal(p.get("price"))
                    if decimal_odd is None:
                        continue
                    designation = p.get("designation") or "?"
                    label = self._designation_label(designation, market_type, home, away)
                    yield OddQuote(
                        event_key=ek,
                        book=Book.PINNACLE,
                        market=market_type,
                        outcome=Outcome(label=label, line=p.get("points")),
                        decimal_odd=decimal_odd,
                        fetched_at=now,
                        source_event_id=str(matchup_id),
                    )

    @staticmethod
    def _map_market(t: str | None) -> MarketType | None:
        return {
            "moneyline": MarketType.H2H,
            "total": MarketType.TOTALS,
            "spread": MarketType.HANDICAP,
        }.get(t or "")

    @staticmethod
    def _designation_label(designation: str, market: MarketType, home: str, away: str) -> str:
        d = designation.lower()
        if market == MarketType.H2H:
            return {"home": "home", "away": "away", "draw": "draw"}.get(d, d)
        if market == MarketType.TOTALS:
            return {"over": "over", "under": "under"}.get(d, d)
        return d
