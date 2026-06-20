from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterator

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..filter import is_noise_event
from ..matcher import event_key
from ..models import Book, OddQuote, Outcome
from ..teams import record_pair
from .unibet import (
    SPORT_TERMS,
    _MARKET_BY_TYPE_ID,
    _OUTCOME_LABELS,
    _odd,
    _line,
    _parse_datetime,
)


# Bingoal.be runs on the Kambi sportsbook platform — the exact same offering API
# as Unibet.be and 711.be. Only the operator ("offering") code differs: Bingoal
# Belgium is "bingoalbe". The public offering API is guest-accessible, so we
# reuse the Unibet/Kambi fetch + parse logic verbatim, swapping only the
# offering code and the Book tag on emitted quotes.
BASE = "https://eu-offering-api.kambicdn.com/offering/v2018"
OFFERING = os.getenv("BINGOAL_OFFERING", "bingoalbe")


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Origin": "https://www.bingoalsport.be",
        "Referer": "https://www.bingoalsport.be/",
    }


class BingoalScraper:
    book = Book.BINGOAL_BE

    # Kambi sport-level group IDs are global across operators (same IDs as the
    # Unibet/711 scrapers) — they identify the sport tree, not the operator.
    SPORT_GROUP_IDS = {
        "soccer": "1000093190",      # football
        "tennis": "1000093193",
        "basketball": "1000093204",
        "hockey": "1000093191",      # ice_hockey
        "esports": "2000077768",
        "volleyball": "1000093214",
    }

    def __init__(self, timeout: float = 15.0):
        self._client = httpx.Client(timeout=timeout, headers=_headers())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BingoalScraper":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self._client.get(f"{BASE}/{OFFERING}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def fetch_listview(self, sport: str = "soccer", path_suffix: str = "") -> dict:
        term = SPORT_TERMS.get(sport, sport)
        full = f"{term}/{path_suffix}" if path_suffix else term
        return self._get(
            f"/listView/{full}.json",
            params={
                "lang": "fr_BE",
                "market": "BE",
                "client_id": "2",
                "channel_id": "1",
                "useCombined": "true",
                "includeParticipants": "true",
                "useWalk": "true",
            },
        )

    def fetch_sport_term_keys(self, sport: str = "soccer") -> list[str]:
        group_id = self.SPORT_GROUP_IDS.get(sport)
        if group_id is None:
            return []
        payload = self._get(
            f"/group/{group_id}.json",
            params={"lang": "fr_BE", "market": "BE"},
        )
        root = (payload.get("group") or payload) if isinstance(payload, dict) else {}
        return [
            child.get("termKey")
            for child in (root.get("groups") or [])
            if child.get("termKey")
        ]

    def fetch_all_events(self, sport: str = "soccer", *, max_terms: int = 100) -> dict:
        """Iterate every termKey of a sport and merge the events lists into one
        payload (same shape parse_listview consumes)."""
        try:
            term_keys = self.fetch_sport_term_keys(sport)
        except httpx.HTTPError:
            term_keys = []
        term_keys = [""] + term_keys[:max_terms]

        seen_ids: set = set()
        merged: list = []
        for tk in term_keys:
            try:
                data = self.fetch_listview(sport, tk)
            except httpx.HTTPError:
                continue
            for ev in data.get("events") or []:
                eid = (ev.get("event") or {}).get("id")
                if eid is None or eid in seen_ids:
                    continue
                seen_ids.add(eid)
                merged.append(ev)
        return {"events": merged}


def parse_listview(data: dict) -> Iterator[OddQuote]:
    """Walk a Kambi listView payload and yield OddQuote objects tagged as Bingoal.

    Identical structure to the Unibet/711 parser — only the Book tag differs.
    Shape: data["events"] is a list of {"event": {...}, "betOffers": [...]}.
    """
    now = datetime.now(timezone.utc)
    for entry in data.get("events") or []:
        ev = entry.get("event") or {}
        home = ev.get("homeName")
        away = ev.get("awayName")
        start = _parse_datetime(ev.get("start"))
        if not (home and away and start):
            continue
        if is_noise_event(home, away, ev.get("group", "")):
            continue
        record_pair(home, away)
        ek = event_key(home, away, start)
        source_id = str(ev.get("id", ""))

        for bo in entry.get("betOffers") or []:
            type_id = (bo.get("betOfferType") or {}).get("id")
            market = _MARKET_BY_TYPE_ID.get(type_id)
            if market is None:
                continue
            for o in bo.get("outcomes") or []:
                decimal_odd = _odd(o.get("odds"))
                if decimal_odd is None or decimal_odd <= 1.0:
                    continue
                label = _OUTCOME_LABELS.get(o.get("type"))
                if label is None:
                    continue
                yield OddQuote(
                    event_key=ek,
                    book=Book.BINGOAL_BE,
                    market=market,
                    outcome=Outcome(label=label, line=_line(o.get("line"))),
                    decimal_odd=decimal_odd,
                    fetched_at=now,
                    source_event_id=source_id,
                )
