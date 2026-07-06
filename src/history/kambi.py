from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..matcher import event_key
from ..models import Book
from .base import HistoryImporter, HistoryImportError, SettledBet, normalize_result


# Every Belgian Kambi skin shares the same authenticated coupon-history API; only
# the offering code, the Book tag, and the account differ. One importer class,
# four instances (see build_kambi_importers).
#
# Endpoint (decoded from a real capture — fr.unibetsports.be, offering "ubbe"):
#   https://cf-mt-auth-api.kambicdn.com/player/api/v2019/ubbe/coupon/history.json
#     ?lang=fr_BE&market=BE&channel_id=1&range_size=25&range_start=0
#      [&fromDate=ISO&toDate=ISO]
#   -> {"historyCoupons": [ {couponRef, placedDate, bets[], outcomes[],
#        couponRows[], events[], betOffers[], ...} ], "range": {start,size,more}}
#
# The money and odds fields are integers scaled by 1000 (stake 25000 = 25.00€,
# playedOdds 2550 = 2.55, line 2500 = 2.5). One SettledBet is emitted per *bet*
# inside a coupon (that's where stake/payout/status live), keyed on betRef.
#
# Auth: the session is short-lived (~1h). The endpoint is credentialed; the exact
# header (Cookie vs Authorization token from punter/login.json) is whatever your
# browser sends — grab it via "Copy as cURL" and paste into env:
#   <PREFIX>_HISTORY_URL   full coupon/history.json URL
#   <PREFIX>_COOKIE        the Cookie header value          (if present)
#   <PREFIX>_AUTH          the Authorization header value   (if present)
# where PREFIX is UNIBET / SEVEN_ELEVEN / BINGOAL / SCOOORE. At least one of
# COOKIE / AUTH must be set.
#
# Optional, only if the numbers come out wrong against a real capture:
#   KAMBI_MONEY_SCALE (default 1000)
#   KAMBI_ODDS_SCALE  (default 1000)


_MONEY_SCALE = float(os.getenv("KAMBI_MONEY_SCALE", "1000"))
_ODDS_SCALE = float(os.getenv("KAMBI_ODDS_SCALE", "1000"))


def _money(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        return round(float(raw) / _MONEY_SCALE, 2)
    except (TypeError, ValueError):
        return None


def _odd(raw: Any) -> Optional[float]:
    """Kambi odds are decimal * 1000 (2550 -> 2.55, 7056 -> 7.056)."""
    if raw is None:
        return None
    try:
        return round(float(raw) / _ODDS_SCALE, 3)
    except (TypeError, ValueError):
        return None


def _line(raw: Any) -> Optional[float]:
    """Kambi lines are also * 1000 (2500 -> 2.5, -1500 -> -1.5)."""
    if raw is None:
        return None
    try:
        return round(float(raw) / _ODDS_SCALE, 3)
    except (TypeError, ValueError):
        return None


def _dt(raw: Any) -> Optional[datetime]:
    """coupon-history timestamps are ISO strings; tolerate epoch ms too."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        secs = raw / 1000.0 if raw > 1_000_000_000_000 else float(raw)
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _coupon_list(data: dict) -> list[dict]:
    """Pull the coupon array out of a history payload, tolerating the envelope
    key variants Kambi has used across versions (historyCoupons is the current
    one)."""
    if not isinstance(data, dict):
        return []
    for key in ("historyCoupons", "coupons", "couponHistory", "items", "history"):
        val = data.get(key)
        if isinstance(val, list):
            return val
    return []


def _outcomes(coupon: dict) -> list[dict]:
    """The outcomes/selections array of a coupon (used by inspect + enrichment)."""
    for key in ("outcomes", "selections", "couponRows"):
        val = coupon.get(key)
        if isinstance(val, list):
            return val
    return []


def parse_history(data: dict, book: Book) -> list[SettledBet]:
    """Parse a Kambi coupon-history payload into SettledBet rows.

    One SettledBet per bet (a coupon holds one or more bets; stake/payout/status
    are per bet). Singles get event/market/selection/line filled by walking
    couponRowIndexes -> couponRows -> outcomes -> events/betOffers. Combos
    (a bet spanning several rows) stay coupon-level with legs = number of rows.
    """
    out: list[SettledBet] = []
    for coupon in _coupon_list(data):
        if not isinstance(coupon, dict):
            continue
        rows_by_index = {
            r.get("index"): r for r in coupon.get("couponRows") or [] if isinstance(r, dict)
        }
        outcomes_by_id = {
            o.get("outcomeId"): o for o in coupon.get("outcomes") or [] if isinstance(o, dict)
        }
        events_by_id = {
            e.get("eventId"): e for e in coupon.get("events") or [] if isinstance(e, dict)
        }
        offers_by_id = {
            b.get("betOfferId"): b for b in coupon.get("betOffers") or [] if isinstance(b, dict)
        }
        placed = _dt(coupon.get("placedDate"))
        currency = coupon.get("currency")
        coupon_ref = coupon.get("couponRef")

        for bet in coupon.get("bets") or []:
            if not isinstance(bet, dict):
                continue
            bet_ref = bet.get("betRef")
            if bet_ref is None:
                continue
            result = normalize_result(bet.get("betStatus"))
            stake = _money(bet.get("stake"))
            # betOdds is 0 on settled losers; playedOdds always carries the real
            # price, so prefer it.
            odd = _odd(bet.get("playedOdds")) or _odd(bet.get("betOdds"))
            payout = None if result == "pending" else _money(bet.get("payout"))
            row_idxs = bet.get("couponRowIndexes") or []
            legs = len(row_idxs) or 1

            sb = SettledBet(
                book=book,
                bet_id=str(bet_ref),
                odd=odd if odd is not None else 0.0,
                stake=stake if stake is not None else 0.0,
                result=result,
                placed_at=placed,
                payout=payout,
                legs=legs,
                currency=str(currency) if currency else None,
                bet_type="combo" if legs > 1 else "single",
                raw=json.dumps(
                    {"couponRef": coupon_ref, "bet": bet},
                    separators=(",", ":"), ensure_ascii=False,
                ),
            )

            if legs == 1 and row_idxs:
                row = rows_by_index.get(row_idxs[0], {})
                outcome = outcomes_by_id.get(row.get("outcomeId"), {})
                event = events_by_id.get(outcome.get("eventId"), {})
                offer = offers_by_id.get(outcome.get("betOfferId"), {})
                sb.selection = outcome.get("label")
                sb.line = _line(outcome.get("line"))
                sb.market = offer.get("criterion") or offer.get("boType")
                sport = event.get("sport")
                sb.sport = str(sport).lower() if sport else None
                sb.event_start = _dt(event.get("eventStartDate"))
                home = event.get("homeName")
                away = event.get("awayName")
                sb.event_label = event.get("eventName") or (
                    f"{home} - {away}" if home and away else None
                )
                if home and away and sb.event_start:
                    sb.event_key = event_key(home, away, sb.event_start)
            else:
                sb.event_label = f"Combiné {legs} sélections"

            out.append(sb)
    return out


class KambiHistoryImporter(HistoryImporter):
    """Bet-history importer for any Kambi skin (Unibet, 711, Bingoal, Scooore).

    Config (env), PREFIX in UNIBET / SEVEN_ELEVEN / BINGOAL / SCOOORE:
      <PREFIX>_HISTORY_URL  full coupon/history.json URL (copied from DevTools)
      <PREFIX>_COOKIE       the logged-in Cookie header value       (optional)
      <PREFIX>_AUTH         the Authorization header value          (optional)
    At least one of COOKIE / AUTH is required.
    """

    def __init__(self, book: Book, env_prefix: str, offering: str, timeout: float = 20.0):
        self.book = book
        self._prefix = env_prefix
        self._offering = offering
        self._timeout = timeout

    def _url(self) -> str:
        # Strip any query string the pasted URL carried — we set params ourselves.
        return os.getenv(f"{self._prefix}_HISTORY_URL", "").strip().split("?", 1)[0]

    def _cookie(self) -> str:
        return os.getenv(f"{self._prefix}_COOKIE", "").strip()

    def _auth(self) -> str:
        return os.getenv(f"{self._prefix}_AUTH", "").strip()

    def available(self) -> bool:
        return bool(self._url() and (self._cookie() or self._auth()))

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/json",
            "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "Origin": "https://fr.unibetsports.be",
            "Referer": "https://fr.unibetsports.be/",
        }
        if self._cookie():
            h["Cookie"] = self._cookie()
        if self._auth():
            h["Authorization"] = self._auth()
        return h

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def _get(self, url: str, params: dict) -> dict:
        with httpx.Client(timeout=self._timeout, headers=self._headers()) as client:
            r = client.get(url, params=params)
            if r.status_code in (401, 403):
                raise HistoryImportError(
                    f"{self.book.value}: authentification refusée (HTTP {r.status_code}) — "
                    f"session Kambi expirée (~1h). Recapture {self._prefix}_COOKIE / "
                    f"{self._prefix}_AUTH."
                )
            r.raise_for_status()
            return r.json()

    def _params(self, range_start: int, page_size: int, since: Optional[datetime]) -> dict:
        now = datetime.now(timezone.utc)
        from_date = since if since is not None else (now - timedelta(days=365))
        return {
            "lang": "fr_BE",
            "market": "BE",
            "channel_id": "1",
            "range_size": page_size,
            "range_start": range_start,
            "fromDate": from_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "toDate": now.strftime("%Y-%m-%dT%H:%M:%S.999Z"),
        }

    def raw_first_page(self, page_size: int = 25) -> dict:
        """First page of history as raw JSON, for `inspect-kambi-history`."""
        url = self._url()
        if not url:
            raise HistoryImportError(f"{self.book.value}: {self._prefix}_HISTORY_URL manquant.")
        return self._get(url, self._params(0, page_size, None))

    def fetch(
        self, *, since: Optional[datetime] = None, page_size: int = 25, max_pages: int = 40,
    ) -> Iterable[SettledBet]:
        url = self._url()
        if not url:
            raise HistoryImportError(f"{self.book.value}: {self._prefix}_HISTORY_URL manquant.")

        seen_ids: set[str] = set()
        for page in range(max_pages):
            try:
                data = self._get(url, self._params(page * page_size, page_size, since))
            except httpx.HTTPError as e:
                raise HistoryImportError(f"{self.book.value}: requête échouée — {e}") from e

            bets = parse_history(data, self.book)
            for sb in bets:
                if sb.bet_id in seen_ids:
                    continue
                seen_ids.add(sb.bet_id)
                # Incremental stop: skip bets placed at/before the newest we
                # already stored (bets come newest-first).
                if since is not None and sb.placed_at is not None and sb.placed_at <= since:
                    continue
                yield sb

            rng = data.get("range") if isinstance(data, dict) else None
            has_more = bool(rng.get("more")) if isinstance(rng, dict) else False
            if not bets or not has_more:
                break


# Offering code per Belgian Kambi skin (mirrors the scrapers). The importer only
# needs it for labelling; the actual endpoint comes from <PREFIX>_HISTORY_URL.
_KAMBI_BOOKS = [
    (Book.UNIBET_BE, "UNIBET", "ubbe"),
    (Book.SEVEN_ELEVEN_BE, "SEVEN_ELEVEN", "sevelevbe"),
    (Book.BINGOAL_BE, "BINGOAL", "bingoalbe"),
    (Book.SCOOORE_BE, "SCOOORE", "bnlbe"),
]


def build_kambi_importers() -> list[KambiHistoryImporter]:
    """One importer per Belgian Kambi skin. Callers filter to the ones that are
    `available()` (i.e. have a URL + cookie/auth configured)."""
    return [
        KambiHistoryImporter(book, prefix, offering)
        for book, prefix, offering in _KAMBI_BOOKS
    ]
