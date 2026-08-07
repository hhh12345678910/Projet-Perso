from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..filter import is_noise_event
from ..matcher import event_key
from ..models import Book, MarketType, OddQuote, Outcome
from ..teams import record_pair


# BetFirst's sportsbook backend (separate from sports.betfirst.be).
BASE = "https://d-cf.betfirstplayground.net/api/sb/v1"

# Our sport name -> BetFirst categoryId.
# BetFirst categoryIds discovered by walking widgets/categories/v2 and probing
# events-table responses for each — every ID below was confirmed live.
# Esports has no dedicated BetFirst category (the eSoccer offering is mixed
# into football, surfaced via ESF* marketTemplateIds we currently skip).
CATEGORY_IDS = {
    "soccer": 1,
    "hockey": 2,
    "basketball": 4,
    "tennis": 11,
}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


_BRAND_ID = "4a876283-f28e-4396-bb32-d72b02b2e535"  # BetFirst's brandId

# Guest/anonymous session JWT the frontend ships to every visitor — payload is a
# fixed placeholder identity (userId 111…, jurisdiction "Unknown"), not tied to a
# login, so it's safe to embed. The backend started REQUIRING it: without a
# sessiontoken the header-complete request is silently dropped (ReadTimeout) even
# though the other x-sb-* headers are present — which is why BetFirst went dark.
_GUEST_SESSION_TOKEN = (
    "ew0KICAiYWxnIjogIkhTMjU2IiwNCiAgInR5cCI6ICJKV1QiDQp9."
    "ew0KICAianVyaXNkaWN0aW9uIjogIlVua25vd24iLA0KICAidXNlcklkIjogIjExMTExMTExLTExMTEt"
    "MTExMS0xMTExLTExMTExMTExMTExMSIsDQogICJsb2dpblNlc3Npb25JZCI6ICIxMTExMTExMS0xMTEx"
    "LTExMTEtMTExMS0xMTExMTExMTExMTEiDQp9."
    "yuBO_qNKJHtbCWK3z04cEqU59EKU8pZb2kXHhZ7IeuI"
)
# Guest context identifiers the SB platform stamps on anonymous traffic. Stable
# (derived from brand/jurisdiction/segment, not a login session).
_STATIC_CONTEXT_ID = "stc--1472654605"
_SEGMENT_ID = "cd84f9f8-9d56-4985-94b1-773eaac4868a"


def _headers() -> dict[str, str]:
    # The sportsbook backend validates a set of Entain OBG / SB-platform
    # headers (brandid, marketcode, sessiontoken, x-sb-*); without the full set
    # a request either 400s (E_VALIDATION_INVALIDHEADER) or, when only the
    # sessiontoken is missing, is silently blackholed (ReadTimeout).
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "Origin": "https://sports.betfirst.be",
        "Referer": "https://sports.betfirst.be/",
        "brandid": _BRAND_ID,
        "marketcode": "bx",
        "correlationid": str(uuid.uuid4()),  # per-request trace id, like the browser
        "sessiontoken": _GUEST_SESSION_TOKEN,
        "x-obg-channel": "Web",
        "x-obg-device": "Desktop",
        "x-sb-app-version": "8.0.6.4335-r13a4254",
        "x-sb-channel": "Web",
        "x-sb-content-id": _BRAND_ID,
        "x-sb-country-code": "BE",
        "x-sb-currency-code": "EUR",
        "x-sb-device-type": "Desktop",
        "x-sb-identifier": "EVENT_TABLE_REQUEST",
        "x-sb-jurisdiction": "Bgc",
        "x-sb-language-code": "bx",
        "x-sb-segment-id": _SEGMENT_ID,
        "x-sb-static-context-id": _STATIC_CONTEXT_ID,
        "x-sb-type": "b2b",
        "x-sb-user-context-id": _STATIC_CONTEXT_ID,
    }


class BetFirstScraper:
    book = Book.BETFIRST

    def __init__(self, timeout: float = 15.0):
        self._client = httpx.Client(timeout=timeout, headers=_headers())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BetFirstScraper":
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

    def fetch_events_table(
        self,
        sport: str = "soccer",
        *,
        days_ahead: int = 7,
        max_market_count: int = 10,
        page_number: int = 1,
    ) -> dict:
        """Fetch one page of the prematch events-table widget."""
        cat = CATEGORY_IDS.get(sport, sport)
        now = datetime.now(timezone.utc)
        starts_after = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        starts_before = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._get(
            "/widgets/events-table/v2",
            params={
                "categoryIds": cat,
                "eventPhase": "Prematch",
                "eventSortBy": "StartDate",
                "includeSkeleton": "true",
                "maxMarketCount": max_market_count,
                "pageNumber": page_number,
                "priceFormats": 1,  # 1 = decimal
                "startsOnOrAfter": starts_after,
                "startsBefore": starts_before,
            },
        )

    def fetch_all_events(
        self,
        sport: str = "soccer",
        *,
        days_ahead: int = 3,
        max_market_count: int = 10,
        max_pages: int = 50,
        max_workers: int = 4,
    ) -> dict:
        """Iterate over every page of the events table and merge into a single
        events/markets/selections payload — the same shape parse_events_table
        consumes.

        Les pages sont récupérées EN PARALLÈLE après la première. En série sur
        sept jours, la collecte prenait 80 secondes — et comme le cycle de scan
        attend tous les books avant de rendre la main, elle aurait fait passer
        les cycles de 20 s à 80 s. C'est exactement ce qui a fait retirer
        Smarkets (§5) : une source lente ne coûte pas seulement son temps, elle
        silencie tout le reste pendant qu'elle travaille.

        `max_workers` reste bas volontairement. Ce book avait été coupé sur un
        403 d'anti-bot ; cinquante requêtes simultanées seraient le meilleur
        moyen de le retrouver. Quatre suffisent à ramener la durée dans le
        budget d'un cycle.

        `days_ahead` passe de 7 à 3 jours : la mesure du 05/08 donne une médiane
        de 23,5 h avant coup d'envoi sur les opportunités suivies, et la tranche
        au-delà de 48 h est la moins rentable de toutes. Payer quatre jours de
        pagination supplémentaire pour la queue la plus faible n'a pas de sens.
        """
        first = self.fetch_events_table(
            sport, days_ahead=days_ahead,
            max_market_count=max_market_count, page_number=1,
        )
        data = first.get("data") or {}
        events = list(data.get("events") or [])
        markets = list(data.get("markets") or [])
        selections = list(data.get("selections") or data.get("marketSelections") or [])
        total_pages = min(int(data.get("totalPages") or 1), max_pages)

        if total_pages > 1:
            from concurrent.futures import ThreadPoolExecutor
            def _page(n: int) -> dict:
                try:
                    return self.fetch_events_table(
                        sport, days_ahead=days_ahead,
                        max_market_count=max_market_count, page_number=n,
                    )
                except Exception:
                    # Une page perdue ampute la couverture ; la faire échouer
                    # entière la supprimerait. On garde ce qu'on a.
                    return {}
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                for payload in ex.map(_page, range(2, total_pages + 1)):
                    d = payload.get("data") or {}
                    events.extend(d.get("events") or [])
                    markets.extend(d.get("markets") or [])
                    selections.extend(d.get("selections") or d.get("marketSelections") or [])
        return {"data": {"events": events, "markets": markets, "selections": selections}}


# BetFirst tags every market with a stable `marketTemplateId`. Map only those
# that line up with Pinnacle (1X2 / moneyline, total goals, handicap).
_MARKET_BY_TEMPLATE = {
    "MW3W": MarketType.H2H,       # 1X2 (3-way match winner) - main soccer market
    "MW2W": MarketType.H2H,       # 2-way moneyline (no draw sports)
    "MTG2W": MarketType.TOTALS,   # Total goals over/under (line in lineValueRaw)
    "MTG2W25": MarketType.TOTALS, # Total goals at 2.5
    "MWOU": MarketType.TOTALS,    # Over/under variants
    "TGOUOT": MarketType.TOTALS,
    "M3WHCP": MarketType.HANDICAP,
    "HCAP": MarketType.HANDICAP,
    "FAHC": MarketType.HANDICAP,
    "FHOT": MarketType.HANDICAP,
}


def _market_type(market: dict) -> MarketType | None:
    return _MARKET_BY_TEMPLATE.get(str(market.get("marketTemplateId") or "").upper())


# selectionTemplateId is BetFirst's stable, language-independent outcome code.
_LABEL_BY_TEMPLATE = {
    "HOME": "home",
    "DRAW": "draw",
    "AWAY": "away",
    "OVER": "over",
    "UNDER": "under",
}


def _selection_label(sel: dict) -> str | None:
    code = str(sel.get("selectionTemplateId") or "").upper()
    if code in _LABEL_BY_TEMPLATE:
        return _LABEL_BY_TEMPLATE[code]
    # Defensive fallback for handicap-style "home/away with line": some
    # template IDs include a side hint without using HOME/AWAY directly.
    if sel.get("isHomeTeam") is True:
        return "home"
    if sel.get("isHomeTeam") is False and code:
        return "away"
    return None


def _parse_datetime(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _extract_home_away(participants: list) -> tuple[str | None, str | None]:
    if not isinstance(participants, list) or len(participants) < 2:
        return None, None
    home = away = None
    # `side` is the canonical signal: 1 = home, 2 = away.
    for p in participants:
        if not isinstance(p, dict):
            continue
        name = p.get("label")
        if not name:
            continue
        if p.get("side") == 1:
            home = name
        elif p.get("side") == 2:
            away = name
    if home and away:
        return home, away
    # Positional fallback if side is missing.
    names = [p["label"] for p in participants if isinstance(p, dict) and p.get("label")]
    if len(names) >= 2:
        return home or names[0], away or names[1]
    return None, None


def parse_events_table(payload: dict) -> Iterator[OddQuote]:
    """Walk a BetFirst events-table/v2 payload and yield OddQuote objects.

    Shape: payload["data"]["events"|"markets"|"selections"] are flat lists.
    Markets join to events via `eventId` (the suffix of `globalId`), and
    selections join to markets via `marketId`.
    """
    data = payload.get("data") or {}
    events = data.get("events") or []
    markets = data.get("markets") or []
    # The events-table endpoint uses `selections`; per-event endpoints use
    # `marketSelections`. Accept either so the parser is symmetric.
    selections = data.get("selections") or data.get("marketSelections") or []
    if not (events and markets and selections):
        return

    now = datetime.now(timezone.utc)

    # eventId -> (event_key, source_event_id)
    event_index: dict[str, tuple[str, str]] = {}
    for ev in events:
        global_id = ev.get("globalId") or ""
        # globalId is "event.<cat>.<region>.<comp>.<eventId>"; the eventId is
        # whatever follows the last dot.
        eid = global_id.rsplit(".", 1)[-1] if global_id else ""
        if not eid:
            continue
        home, away = _extract_home_away(ev.get("participants") or [])
        start = _parse_datetime(ev.get("startDate"))
        if not (home and away and start):
            continue
        if is_noise_event(home, away, ev.get("competitionName", "")):
            continue
        record_pair(home, away)
        event_index[eid] = (event_key(home, away, start), eid)

    # marketId -> (event_key, MarketType, line, source_event_id)
    market_index: dict[str, tuple[str, MarketType, float | None, str]] = {}
    for m in markets:
        eid = str(m.get("eventId") or "")
        ev_data = event_index.get(eid)
        if ev_data is None:
            continue
        mt = _market_type(m)
        if mt is None:
            continue
        if m.get("status") != "Open":
            continue
        line_raw = m.get("lineValueRaw")
        try:
            line = float(line_raw) if line_raw not in (None, 0, 0.0) else None
        except (TypeError, ValueError):
            line = None
        market_index[str(m.get("id") or "")] = (ev_data[0], mt, line, ev_data[1])

    for sel in selections:
        mkey = str(sel.get("marketId") or "")
        m_data = market_index.get(mkey)
        if m_data is None:
            continue
        if sel.get("status") != "Open":
            continue
        try:
            decimal_odd = float(sel.get("odds"))
        except (TypeError, ValueError):
            continue
        if decimal_odd <= 1.0:
            continue
        label = _selection_label(sel)
        if label is None:
            continue
        ek, market_type, line, source_eid = m_data
        yield OddQuote(
            event_key=ek,
            book=Book.BETFIRST,
            market=market_type,
            outcome=Outcome(label=label, line=line),
            decimal_odd=decimal_odd,
            fetched_at=now,
            source_event_id=source_eid,
        )
