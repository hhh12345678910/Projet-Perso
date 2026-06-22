from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..filter import is_noise_event
from ..matcher import event_key
from ..models import Book, MarketType, OddQuote, Outcome
from ..teams import record_pair


# Napoleonsports.be runs on the Superbet platform. The prematch offer is served
# from a public Fastly CDN endpoint (no auth) that returns every event of a
# sport with its 1X2 (match result) odds in one call — independent odds (not a
# Kambi/Altenar reseller), so it genuinely widens value/surebet coverage.
BASE = "https://production-superbet-offer-ng-be.freetls.fastly.net/v2/en-BE"

# Superbet sportId codes (from /v2/en-BE/struct).
SPORT_IDS = {
    "soccer": 5,
    "tennis": 2,
    "basketball": 4,
    "hockey": 3,        # Ice Hockey
    "volleyball": 1,
}

# Only the 1X2 / match-result market (marketId 547) is carried in the by-date
# feed. Outcome codes are locale-independent: 1 = home, 0 = draw, 2 = away.
_MATCH_RESULT_MARKET_ID = 547
_OUTCOME_LABELS = {"1": "home", "0": "draw", "2": "away"}


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
        "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Origin": "https://www.napoleonsports.be",
        "Referer": "https://www.napoleonsports.be/",
    }


def _odd(price) -> float | None:
    try:
        v = float(price)
    except (TypeError, ValueError):
        return None
    return v if v > 1.0 else None


class NapoleonScraper:
    book = Book.NAPOLEON_BE

    def __init__(self, timeout: float = 15.0):
        self._client = httpx.Client(timeout=timeout, headers=_headers())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "NapoleonScraper":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    def fetch_by_date(self, sport: str = "soccer", *, days_ahead: int = 4) -> dict:
        """Fetch every active prematch event of a sport (with 1X2 odds) in one
        call, over a now..now+days_ahead window."""
        sport_id = SPORT_IDS.get(sport, sport)
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days_ahead)
        r = self._client.get(
            f"{BASE}/events/by-date",
            params={
                "currentStatus": "active",
                "offerState": "prematch",
                "startDate": now.strftime("%Y-%m-%d %H:%M:%S"),
                "endDate": end.strftime("%Y-%m-%d %H:%M:%S"),
                "sportId": sport_id,
            },
        )
        r.raise_for_status()
        return r.json()


def parse_by_date(payload: dict) -> Iterator[OddQuote]:
    """Walk a Superbet by-date payload and yield 1X2 OddQuote objects tagged as
    Napoleon. Shape: payload["data"] is a list of events; each has matchName
    ('home·away'), matchDate (UTC), and an `odds` list."""
    now = datetime.now(timezone.utc)
    for ev in payload.get("data") or []:
        name = ev.get("matchName") or ""
        parts = [p.strip() for p in name.split("·")]
        if len(parts) != 2 or not (parts[0] and parts[1]):
            continue
        home, away = parts
        raw_date = ev.get("matchDate")
        if not raw_date:
            continue
        try:
            start = datetime.strptime(raw_date, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if is_noise_event(home, away, ev.get("tournamentName", "")):
            continue
        record_pair(home, away)
        ek = event_key(home, away, start)
        source_id = str(ev.get("eventId", ""))

        for o in ev.get("odds") or []:
            if o.get("marketId") != _MATCH_RESULT_MARKET_ID:
                continue
            if o.get("status") not in (None, "active"):
                continue
            label = _OUTCOME_LABELS.get(str(o.get("code")))
            if label is None:
                continue
            decimal_odd = _odd(o.get("price"))
            if decimal_odd is None:
                continue
            yield OddQuote(
                event_key=ek,
                book=Book.NAPOLEON_BE,
                market=MarketType.H2H,
                outcome=Outcome(label=label, line=None),
                decimal_odd=decimal_odd,
                fetched_at=now,
                source_event_id=source_id,
            )
