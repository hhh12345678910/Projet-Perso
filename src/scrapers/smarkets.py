from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Iterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..matcher import event_key
from ..models import Book, MarketType, OddQuote, Outcome


# Smarkets is a London-based betting exchange with a public, unauthenticated
# REST API. Used here as a secondary sharp reference alongside Pinnacle —
# their bid/offer mid-prices are essentially margin-free (peer-to-peer
# matching), which cross-validates the Pinnacle-devigged fair line.
BASE = "https://api.smarkets.com/v3"

# Our sport keys -> Smarkets type_domain values.
SPORT_DOMAINS = {
    "soccer": "football",
    "tennis": "tennis",
    "basketball": "basketball",
    "hockey": "ice_hockey",
    "esports": "esports",
}

# Smarkets carries dozens of derivative markets per match (correct score,
# half-time, BTTS, ...). Stick to the few that map cleanly to Pinnacle's
# moneyline + total goals; everything else is filtered server-side.
# WINNER_3_WAY is used for sports with a draw (football, hockey reg-time),
# WINNER_2_WAY for sports without (tennis, basketball moneyline).
MAIN_MARKET_TYPES = "WINNER_3_WAY,WINNER_2_WAY,OVER_UNDER"


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    }


class SmarketsScraper:
    book = Book.SMARKETS

    def __init__(self, timeout: float = 20.0, request_delay: float | None = None):
        self._client = httpx.Client(timeout=timeout, headers=_headers())
        # Smarkets enforces a per-IP rate limit on its public API; a small
        # inter-request delay keeps a multi-sport scrape under the cap.
        # 0.3s seems to be the sustainable floor — anything lower trips 429s
        # mid-tennis after a full football pass.
        self._delay = request_delay if request_delay is not None else float(
            os.getenv("SMARKETS_REQUEST_DELAY", "0.3")
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SmarketsScraper":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(5),
        # Longer max wait so 429s get a real cool-down — the public Smarkets
        # rate limit resets on a ~30 s window.
        wait=wait_exponential(multiplier=2, min=2, max=30),
    )
    def _get(self, path: str, params: dict | None = None) -> dict:
        if self._delay:
            time.sleep(self._delay)
        r = self._client.get(f"{BASE}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def fetch_upcoming_events(
        self, sport: str, *, limit: int = 200, max_events: int = 600
    ) -> list[dict]:
        """Paginate over upcoming single-event matches of a sport and return
        every event whose start time is in the future."""
        domain = SPORT_DOMAINS.get(sport)
        if domain is None:
            return []
        events: list[dict] = []
        last_id: str | None = None
        while len(events) < max_events:
            params: dict = {
                "type_domain": domain,
                "state": "upcoming",
                "type_scope": "single_event",
                "limit": limit,
            }
            if last_id is not None:
                params["pagination_last_id"] = last_id
            data = self._get("/events/", params=params)
            page = data.get("events") or []
            if not page:
                break
            events.extend(page)
            pagination = data.get("pagination") or {}
            next_page = pagination.get("next_page") or ""
            # The next_page URL embeds the `pagination_last_id` query param.
            m = re.search(r"pagination_last_id=(\d+)", next_page)
            if m is None:
                break
            last_id = m.group(1)
        return events[:max_events]

    def fetch_event_markets(self, event_id: int) -> list[dict]:
        data = self._get(
            f"/events/{event_id}/markets/",
            params={"market_types": MAIN_MARKET_TYPES},
        )
        return data.get("markets") or []

    def fetch_market_contracts(self, market_id: int) -> list[dict]:
        data = self._get(f"/markets/{market_id}/contracts/")
        return data.get("contracts") or []

    def fetch_quotes(self, market_ids: list[int]) -> dict:
        """Batch-fetch best-bid / best-offer for many markets at once."""
        if not market_ids:
            return {}
        ids = ",".join(str(i) for i in market_ids)
        return self._get(f"/markets/{ids}/quotes/")


_LINE_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _parse_line_from_market_name(name: str) -> float | None:
    """Smarkets OVER_UNDER markets encode the line in the display name
    (e.g. 'Over/under 2.5')."""
    match = _LINE_RE.search(name or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_event_time(raw: str | None) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _split_event_name(name: str) -> tuple[str | None, str | None]:
    """Smarkets event names use ' vs ' as separator (case-insensitive)."""
    if not isinstance(name, str):
        return None, None
    # Try variants — Smarkets sometimes uses ' vs ', ' v ', ' @ '.
    for sep in (" vs ", " vs. ", " v ", " @ "):
        if sep in name.lower():
            # Use the lowercase position to split the original string.
            i = name.lower().find(sep)
            return name[:i].strip(), name[i + len(sep):].strip()
    return None, None


def _mid_decimal_odd(bids: list[dict], offers: list[dict]) -> float | None:
    """Best-bid and best-offer prices are scaled probabilities (× 10000);
    the mid is the market's consensus fair probability — invert for the
    decimal odd. Returns None if either side of the book is empty (no
    standing offers means no reliable price)."""
    best_bid = bids[0]["price"] if bids else None
    best_offer = offers[0]["price"] if offers else None
    if not best_bid or not best_offer:
        return None
    mid_prob = (best_bid + best_offer) / 2.0 / 10000.0
    if mid_prob <= 0 or mid_prob >= 1:
        return None
    return 1.0 / mid_prob


def _outcome_label(
    contract_name: str, home: str | None, away: str | None,
    market_type: MarketType,
) -> str | None:
    if market_type == MarketType.H2H:
        s = (contract_name or "").strip().lower()
        if s == "draw":
            return "draw"
        if home and contract_name == home:
            return "home"
        if away and contract_name == away:
            return "away"
        return None
    if market_type == MarketType.TOTALS:
        s = (contract_name or "").strip().lower()
        if s.startswith("over"):
            return "over"
        if s.startswith("under"):
            return "under"
    return None


_MARKET_TYPE_MAP = {
    "WINNER_3_WAY": MarketType.H2H,    # sports with a regulation draw
    "WINNER_2_WAY": MarketType.H2H,    # tennis, basketball, etc.
    "OVER_UNDER": MarketType.TOTALS,
}


def iter_quotes_for_event(
    scraper: SmarketsScraper, event: dict, *, batch_size: int = 10,
) -> Iterator[OddQuote]:
    """Yield OddQuotes for one Smarkets event by fetching its markets,
    their contracts and a batched quotes snapshot. Done as a function (not a
    scraper method) so it can be unit-tested with a stub scraper."""
    home, away = _split_event_name(event.get("name") or "")
    start = _parse_event_time(event.get("start_datetime"))
    if not (home and away and start):
        return
    ek = event_key(home, away, start)
    source_id = str(event.get("id") or "")

    markets = scraper.fetch_event_markets(int(event["id"]))
    # Group by sport-recognisable type and ignore the rest.
    usable = [
        m for m in markets
        if m.get("state") == "open"
        and _MARKET_TYPE_MAP.get((m.get("market_type") or {}).get("name")) is not None
    ]
    if not usable:
        return

    market_ids = [int(m["id"]) for m in usable]
    quotes_by_contract: dict[str, dict] = {}
    for i in range(0, len(market_ids), batch_size):
        quotes_by_contract.update(scraper.fetch_quotes(market_ids[i:i + batch_size]))

    now = datetime.now(timezone.utc)
    for m in usable:
        market_type = _MARKET_TYPE_MAP[(m["market_type"])["name"]]
        line = (
            _parse_line_from_market_name(m.get("name") or "")
            if market_type == MarketType.TOTALS else None
        )
        contracts = scraper.fetch_market_contracts(int(m["id"]))
        for c in contracts:
            quote = quotes_by_contract.get(str(c.get("id")))
            if quote is None:
                continue
            decimal_odd = _mid_decimal_odd(quote.get("bids") or [], quote.get("offers") or [])
            if decimal_odd is None or decimal_odd <= 1.0:
                continue
            label = _outcome_label(c.get("name") or "", home, away, market_type)
            if label is None:
                continue
            yield OddQuote(
                event_key=ek,
                book=Book.SMARKETS,
                market=market_type,
                outcome=Outcome(label=label, line=line),
                decimal_odd=decimal_odd,
                fetched_at=now,
                source_event_id=source_id,
            )


def iter_all_quotes(
    scraper: SmarketsScraper, sport: str, *, max_events: int = 200,
) -> Iterator[OddQuote]:
    """Walk every upcoming event for a sport and yield their OddQuotes."""
    events = scraper.fetch_upcoming_events(sport, max_events=max_events)
    for ev in events:
        yield from iter_quotes_for_event(scraper, ev)
