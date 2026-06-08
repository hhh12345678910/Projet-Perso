from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..filter import is_noise_event
from ..matcher import event_key
from ..models import Book, MarketType, OddQuote, Outcome
from ..teams import record_pair


# BetCenter.be uses the "Cashpoint" proprietary platform from the Gauselmann /
# MERKUR GROUP. The odds API lives on a separate sub-domain; every request
# requires a handful of x-* context headers (language, country, system).
# The "9" path segment on getGames controls how many market groups are returned
# per event; 9 gives the full market depth.
BASE = "https://oddsservice.betcenter.be"
JURISDICTION_ID = 30   # Belgium
_GAMES_DEPTH = 9       # number of market groups returned per event

# sportId values observed in HAR captures; tennis/hockey are educated guesses
# based on common Cashpoint platform conventions — update if wrong.
SPORT_IDS = {
    "soccer":     1,
    "tennis":     2,
    "basketball": 12,
    "hockey":     10,
}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Origin": "https://www.betcenter.be",
        "Referer": "https://www.betcenter.be/",
        "x-language": "7",        # 7 = French
        "x-system-id": "0",
        "x-location": "21",       # 21 = Belgium
        "x-client-country": "21",
    }


class BetCenterScraper:
    book = Book.BETCENTER

    def __init__(self, timeout: float = 20.0):
        self._client = httpx.Client(timeout=timeout, headers=_headers())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BetCenterScraper":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    def _post(self, path: str, body: dict) -> dict:
        r = self._client.post(f"{BASE}{path}", json=body)
        r.raise_for_status()
        return r.json()

    def fetch_games_page(
        self,
        sport_id: int,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> dict:
        """Fetch one page of prematch events for a sport."""
        return self._post(
            f"/odds/getGames/{_GAMES_DEPTH}",
            {
                "sportId": sport_id,
                "gameTypes": [1, 4, 7],
                "limit": limit,
                "offset": offset,
                "jurisdictionId": JURISDICTION_ID,
            },
        )

    def fetch_all_quotes(self, sport: str = "soccer") -> list[OddQuote]:
        """Paginate through all prematch events for a sport and return OddQuote objects."""
        sport_id = SPORT_IDS.get(sport, 1)
        out: list[OddQuote] = []
        limit = 200
        offset = 0
        seen_ids: set = set()
        while True:
            payload = self.fetch_games_page(sport_id, limit=limit, offset=offset)
            games = payload.get("games") or []
            new_games = [g for g in games if g.get("id") not in seen_ids]
            seen_ids.update(g.get("id") for g in new_games if g.get("id") is not None)
            out.extend(parse_games(new_games))
            if len(games) < limit:
                break
            offset += limit
        return out


# ── Odds parsing ──────────────────────────────────────────────────────────────

def _odd(raw: Any) -> float | None:
    """BetCenter odds are decimal odds × 100 (e.g. 155 → 1.55)."""
    if raw is None:
        return None
    try:
        return int(raw) / 100.0
    except (TypeError, ValueError):
        return None


def _parse_event_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


# Only full-match over/under markets; excludes per-team, per-half sub-markets
# that share the same `anchor` field but have inflated odds (e.g., "Equipe 1 p/m 1,5").
_FULLMATCH_TOTALS_RE = re.compile(r"^Plus \+ / [Mm]oins\b", re.IGNORECASE)

# Cashpoint tip text -> normalised outcome label for H2H / handicap markets.
_TIP_LABEL = {"1": "home", "X": "draw", "2": "away"}


def _market_type(market: dict) -> MarketType | None:
    """Identify market type from the Cashpoint market dict.

    Totals markets carry a non-null `anchor` float (the over/under threshold).
    We additionally require the market text to match the standard full-match
    "Plus + / Moins" pattern to exclude per-team and per-half sub-markets that
    share the same `anchor` field structure but have wildly inflated odds.
    Handicap markets carry `hcAnchor` / `hc` (the handicap value/string).
    The primary match-result market has `isPrimary: true`."""
    raw_anchor = market.get("anchor")
    if raw_anchor is not None:
        if _FULLMATCH_TOTALS_RE.match(market.get("text", "")):
            return MarketType.TOTALS
        return None
    if "hcAnchor" in market or "hc" in market:
        return MarketType.HANDICAP
    if market.get("isPrimary"):
        return MarketType.H2H
    return None


def _totals_label(tip_text: str) -> str | None:
    """Map a Cashpoint totals tip text to 'over' / 'under'.

    Over tips end with '+' (e.g. '3+' for over-2.5).
    Under tips match a numeric range pattern (e.g. '0-2')."""
    if tip_text.endswith("+"):
        return "over"
    if tip_text and (tip_text[0].isdigit() or tip_text.startswith("0")):
        return "under"
    return None


def parse_games(
    games: list[dict], *, book: Book = Book.BETCENTER
) -> Iterator[OddQuote]:
    """Walk a list of Cashpoint game dicts and yield OddQuote objects."""
    now = datetime.now(timezone.utc)
    for game in games:
        teams = sorted(game.get("teams") or [], key=lambda t: t.get("order", 99))
        if len(teams) < 2:
            continue
        home = teams[0].get("name")
        away = teams[1].get("name")
        start = _parse_event_time(game.get("startTime"))
        if not (home and away and start):
            continue
        league_name = (game.get("leagueInfo") or {}).get("name", "")
        if is_noise_event(home, away, league_name):
            continue
        record_pair(home, away)
        ek = event_key(home, away, start)
        source_id = str(game.get("id") or "")

        for market in game.get("markets") or []:
            market_type = _market_type(market)
            if market_type is None:
                continue

            if market_type == MarketType.TOTALS:
                raw_anchor = market.get("anchor")
                line: float | None = float(raw_anchor) if raw_anchor is not None else None
            elif market_type == MarketType.HANDICAP:
                raw_hc = market.get("hcAnchor")
                line = float(raw_hc) if raw_hc is not None else None
            else:
                line = None

            for tip in market.get("tips") or []:
                tip_text = str(tip.get("text") or "")
                decimal_odd = _odd(tip.get("odds"))
                if decimal_odd is None or decimal_odd <= 1.0:
                    continue

                if market_type == MarketType.H2H:
                    label = _TIP_LABEL.get(tip_text)
                elif market_type == MarketType.HANDICAP:
                    label = _TIP_LABEL.get(tip_text)
                else:  # TOTALS
                    label = _totals_label(tip_text)

                if label is None:
                    continue

                yield OddQuote(
                    event_key=ek,
                    book=book,
                    market=market_type,
                    outcome=Outcome(label=label, line=line),
                    decimal_odd=decimal_odd,
                    fetched_at=now,
                    source_event_id=source_id,
                )
