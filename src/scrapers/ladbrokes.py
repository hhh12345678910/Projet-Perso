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


# Ladbrokes.be runs on the Eurobet sport-schedule platform (Entain Italy
# codebase). The "next" prematch homepage endpoint returns the upcoming
# highlighted events with their main markets (1X2 + Totals + BTTS).
BASE = "https://www.ladbrokes.be"

# Our sport name -> Eurobet's discipline description (displayed in the menu).
SPORT_DESCRIPTIONS = {
    "soccer": "FOOTBALL",
    "tennis": "TENNIS",
    "basketball": "BASKETBALL",
    "hockey": "HOCKEY",
    "esports": "ESPORTS",   # best-effort; Eurobet may not expose it for BE.
}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _headers() -> dict[str, str]:
    # The Eurobet sport-schedule service requires x-eb-* validation headers;
    # without them every prematch call 404s.
    return {
        "Accept": "application/json",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Referer": "https://www.ladbrokes.be/fr/sports/",
        "x-eb-accept-language": "fr_BE",
        "x-eb-marketid": "5",      # 5 = Belgium
        "x-eb-platformid": "2",    # 2 = web/desktop
    }


class LadbrokesScraper:
    book = Book.LADBROKES_BE

    def __init__(self, timeout: float = 15.0):
        self._client = httpx.Client(timeout=timeout, headers=_headers())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LadbrokesScraper":
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

    def fetch_prematch_next(self) -> dict:
        """Fetch the highlighted upcoming prematch events (homepage 'next' tab)."""
        return self._get("/prematch-homepage-service/sport-schedule/services/prematch-homepage/next")

    def fetch_prematch_highlight(self) -> dict:
        """Fetch the curated featured prematch events."""
        return self._get("/prematch-homepage-service/sport-schedule/services/prematch-homepage/highlight")

    def fetch_prematch_menu(self) -> dict:
        """Fetch the navigation tree: disciplines -> regions -> meetings."""
        return self._get(
            "/prematch-menu-service/sport-schedule/services/prematch-menu",
            params={"prematch": 1, "live": 0},
        )

    def fetch_meeting(self, sport_alias: str, meeting_alias: str) -> dict:
        """Fetch every prematch event of one competition (1X2 + Totals + BTTS)."""
        return self._get(
            f"/detail-service/sport-schedule/services/meeting/{sport_alias}/{meeting_alias}",
            params={"prematch": 1, "live": 0},
        )

    def iter_leaf_meetings(
        self, menu_payload: dict, sport_description: str = "FOOTBALL"
    ) -> Iterator[tuple[str, str, int]]:
        """Walk the menu tree under one discipline and yield every leaf meeting
        as (sport_alias, meeting_alias, eventsNr)."""
        sport_list = (menu_payload.get("result") or {}).get("sportList") or []
        target = sport_description.strip().upper()
        sport = next(
            (s for s in sport_list if str(s.get("description") or "").strip().upper() == target),
            None,
        )
        if sport is None:
            return
        sport_alias = sport.get("aliasUrl") or ""

        def _walk(node: dict) -> Iterator[tuple[str, str, int]]:
            if str(node.get("itemType") or "").lower() == "meeting":
                events_nr = int(node.get("eventsNr") or 0)
                alias = node.get("aliasUrl") or ""
                if alias and events_nr > 0:
                    yield sport_alias, alias, events_nr
                return
            for child in node.get("itemList") or []:
                yield from _walk(child)

        yield from _walk(sport)

    def fetch_all_meetings(
        self,
        sport: str = "soccer",
        *,
        max_meetings: int = 80,
        min_events: int = 1,
    ) -> dict:
        """Discover every meeting of a sport and fetch each one. Returns a
        merged {'result': {'events': [...]}} payload so parse_prematch consumes
        it directly. Best-effort: HTTP errors on a single meeting are skipped."""
        # Accept both our sport keys ("soccer") and Eurobet descriptions
        # ("FOOTBALL") so older callers keep working.
        sport_description = SPORT_DESCRIPTIONS.get(sport, sport)
        menu = self.fetch_prematch_menu()
        leaves = [
            (s, m, n) for s, m, n in self.iter_leaf_meetings(menu, sport_description)
            if n >= min_events
        ]
        leaves.sort(key=lambda t: t[2], reverse=True)
        leaves = leaves[:max_meetings]

        merged_events: list = []
        for sport_alias, meeting_alias, _ in leaves:
            try:
                payload = self.fetch_meeting(sport_alias, meeting_alias)
            except httpx.HTTPError:
                continue
            for group in (payload.get("result") or {}).get("dataGroupList") or []:
                merged_events.extend(group.get("itemList") or [])
        return {"result": {"events": merged_events}}


# Map Eurobet bet group ids / alternativeDescriptions to our market types.
# betId is stable across language; alternativeDescription is the SPA's display
# name and is a useful fallback ("1X2", "Totals", "Winner"). Each sport has
# its own betIds for the "match winner" market (soccer 24 = 1X2 with draw,
# tennis 15288 = 2-way winner, etc.); they all reduce to H2H downstream.
_MARKET_BY_BET_ID = {
    24: MarketType.H2H,         # Soccer RÉSULTAT FINAL (1X2)
    98: MarketType.H2H,         # Basketball MONEYLINE
    478: MarketType.H2H,        # Hockey VAINQUEUR (2-way moneyline)
    15288: MarketType.H2H,      # Tennis "Winner" (2-way moneyline)
    4243: MarketType.TOTALS,    # Soccer Plus/Moins de buts (over/under)
    1532: MarketType.TOTALS,    # Basketball Plus/Moins de (line in description)
    19388: MarketType.TOTALS,   # Hockey Plus/Moins de (incl. OT)
}
_MARKET_BY_ALT_DESC = {
    "1X2": MarketType.H2H,
    "Winner": MarketType.H2H,
    "H/H": MarketType.H2H,      # basketball moneyline shorthand
    "Totals": MarketType.TOTALS,
}


def _market_type(odd_group: dict) -> MarketType | None:
    bet_id = odd_group.get("betId")
    if bet_id in _MARKET_BY_BET_ID:
        return _MARKET_BY_BET_ID[bet_id]
    return _MARKET_BY_ALT_DESC.get(str(odd_group.get("alternativeDescription") or ""))


_LABEL_BY_BOX = {
    "1": "home", "X": "draw", "2": "away",
    "PLUS DE": "over", "MOINS DE": "under",
    "OVER": "over", "UNDER": "under",
}


def _selection_label(box_title: str, market: MarketType) -> str | None:
    key = box_title.strip().upper()
    return _LABEL_BY_BOX.get(key)


_LINE_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _extract_line(odd_group: dict, market: MarketType) -> float | None:
    """Eurobet encodes the totals line in the group description, e.g.
    'PLUS/MOINS DE BUTS 2.5' -> 2.5."""
    if market != MarketType.TOTALS:
        return None
    desc = odd_group.get("oddGroupDescription") or ""
    match = _LINE_RE.search(desc)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _odd_from_int(raw: Any) -> float | None:
    """Eurobet odds are decimal odds * 100 (e.g. 240 -> 2.40)."""
    if raw is None:
        return None
    try:
        return int(raw) / 100.0
    except (TypeError, ValueError):
        return None


def _parse_event_time(event_info: dict) -> datetime | None:
    raw = event_info.get("eventData")
    if raw is None:
        return None
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    # eventData is epoch milliseconds.
    if ts > 1e12:
        ts /= 1000.0
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _home_away(event_info: dict) -> tuple[str | None, str | None]:
    home = (event_info.get("teamHome") or {}).get("description")
    away = (event_info.get("teamAway") or {}).get("description")
    if home and away:
        return home, away
    # Fallback: split eventDescription on " - ".
    desc = event_info.get("eventDescription") or ""
    parts = [p.strip() for p in desc.split(" - ", 1)]
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    return None, None


def parse_prematch(
    payload: dict, *, sport_description: str | None = None
) -> Iterator[OddQuote]:
    """Walk a Ladbrokes prematch-homepage payload and yield OddQuote objects.

    Pass `sport_description` (e.g. 'FOOTBALL', 'TENNIS') to filter out events
    from other disciplines — needed when consuming `/prematch-homepage/next`
    or `/prematch-homepage/highlight`, both of which mix sports. The
    detail-service per-meeting payloads are already sport-scoped, so the
    parameter can be left None for them."""
    result = payload.get("result") or {}
    events = result.get("events") or []
    if not events:
        return

    now = datetime.now(timezone.utc)
    expected = sport_description.upper() if sport_description else None
    for ev in events:
        ei = ev.get("eventInfo") or {}
        # Skip live events; the engine compares to Pinnacle prematch fair lines.
        if ei.get("live"):
            continue
        if expected is not None and (ei.get("disciplineDescription") or "").upper() != expected:
            continue

        home, away = _home_away(ei)
        start = _parse_event_time(ei)
        if not (home and away and start):
            continue
        if is_noise_event(home, away, ei.get("meetingDescription", "")):
            continue
        record_pair(home, away)
        ek = event_key(home, away, start)
        source_id = str(ei.get("eventCode") or ei.get("programCode") or "")

        for bg in ev.get("betGroupList") or []:
            for og in bg.get("oddGroupList") or []:
                market = _market_type(og)
                if market is None:
                    continue
                line = _extract_line(og, market)
                for o in og.get("oddList") or []:
                    box = str(o.get("boxTitle") or "")
                    label = _selection_label(box, market)
                    if label is None:
                        continue
                    decimal_odd = _odd_from_int(o.get("oddValue"))
                    if decimal_odd is None or decimal_odd <= 1.0:
                        continue
                    yield OddQuote(
                        event_key=ek,
                        book=Book.LADBROKES_BE,
                        market=market,
                        outcome=Outcome(label=label, line=line),
                        decimal_odd=decimal_odd,
                        fetched_at=now,
                        source_event_id=source_id,
                    )
