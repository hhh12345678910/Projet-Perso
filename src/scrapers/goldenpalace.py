from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..matcher import event_key
from ..models import Book, MarketType, OddQuote, Outcome


# Golden Palace's sportsbook is rendered by the Altenar B2B platform; their
# frontend API serves every prematch market for a sport in one guest call —
# no auth header is required, only the integration + countryCode query params.
BASE = "https://sb2frontend-altenar2.biahosted.com"
INTEGRATION = "goldenpalace"

# Altenar sportId codes (observed via the GetSportMenu call in the captured HAR).
SPORT_IDS = {
    "soccer": 66,
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
        "Accept": "*/*",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Origin": "https://www.goldenpalacesports.be",
        "Referer": "https://www.goldenpalacesports.be/",
    }


class GoldenPalaceScraper:
    book = Book.GOLDEN_PALACE

    def __init__(self, timeout: float = 20.0):
        self._client = httpx.Client(timeout=timeout, headers=_headers())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GoldenPalaceScraper":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self._client.get(f"{BASE}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def fetch_events(self, sport: str = "soccer") -> dict:
        """Fetch every prematch event of one sport in a single bulk call.

        Returns the raw payload with flat events/markets/odds/competitors lists
        — the same shape parse_get_events consumes."""
        sport_id = SPORT_IDS.get(sport, sport)
        return self._get(
            "/api/widget/GetEvents",
            params={
                "culture": "fr-FR",
                "timezoneOffset": -120,
                "integration": INTEGRATION,
                "deviceType": 1,
                "numFormat": "en-GB",
                "countryCode": "BE",
                "eventCount": 0,
                "sportId": sport_id,
            },
        )


# Altenar tags each market with a stable typeId. Map only those that line up
# with Pinnacle (1X2 / moneyline + total goals).
_MARKET_BY_TYPE_ID = {
    1: MarketType.H2H,       # 1x2
    18: MarketType.TOTALS,   # Total de buts
}


def _market_type(market: dict) -> MarketType | None:
    return _MARKET_BY_TYPE_ID.get(market.get("typeId"))


# Per-market the odd typeId is the canonical position role
# (1 = home, 2 = draw, 3 = away for 1X2; 12 = over, 13 = under for Totals).
_LABEL_BY_ODD_TYPE = {
    1: "home", 2: "draw", 3: "away",
    12: "over", 13: "under",
}


_LINE_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _extract_line(odd_name: str) -> float | None:
    """Totals odd name is e.g. 'Plus de 2.5' — pull the numeric line out."""
    match = _LINE_RE.search(odd_name or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_event_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_get_events(payload: dict) -> Iterator[OddQuote]:
    """Walk a GetEvents payload and yield OddQuote objects.

    Shape: payload has flat lists events/markets/odds/competitors. Events join
    to competitors via competitorIds[] (index 0 = home, 1 = away), to markets
    via marketIds[], and markets join to odds via oddIds[]."""
    events = payload.get("events") or []
    markets = {m["id"]: m for m in (payload.get("markets") or []) if "id" in m}
    odds = {o["id"]: o for o in (payload.get("odds") or []) if "id" in o}
    competitors = {c["id"]: c.get("name") for c in (payload.get("competitors") or [])}

    if not events:
        return
    now = datetime.now(timezone.utc)

    for ev in events:
        cids = ev.get("competitorIds") or []
        if len(cids) < 2:
            continue
        home = competitors.get(cids[0])
        away = competitors.get(cids[1])
        start = _parse_event_time(ev.get("startDate"))
        if not (home and away and start):
            continue
        ek = event_key(home, away, start)
        source_id = str(ev.get("id") or ev.get("code") or "")

        for mid in ev.get("marketIds") or []:
            market = markets.get(mid)
            if market is None:
                continue
            market_type = _market_type(market)
            if market_type is None:
                continue

            for oid in market.get("oddIds") or []:
                odd = odds.get(oid)
                if odd is None:
                    continue
                if odd.get("oddStatus", 0) != 0:
                    continue
                label = _LABEL_BY_ODD_TYPE.get(odd.get("typeId"))
                if label is None:
                    continue
                try:
                    decimal_odd = float(odd.get("price"))
                except (TypeError, ValueError):
                    continue
                if decimal_odd <= 1.0:
                    continue
                line = _extract_line(odd.get("name") or "") if market_type == MarketType.TOTALS else None
                yield OddQuote(
                    event_key=ek,
                    book=Book.GOLDEN_PALACE,
                    market=market_type,
                    outcome=Outcome(label=label, line=line),
                    decimal_odd=decimal_odd,
                    fetched_at=now,
                    source_event_id=source_id,
                )
