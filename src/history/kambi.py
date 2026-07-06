from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..matcher import event_key
from ..models import Book
from .base import HistoryImporter, HistoryImportError, SettledBet, normalize_result


# Every Belgian Kambi skin shares the same coupon-history API; only the offering
# code, the Book tag, and the account/cookie differ. One importer class, four
# instances (see build_kambi_importers).
#
# The exact host varies per operator and isn't worth guessing — you already have
# the full URL in your browser (DevTools → Network → coupon/history.json → Copy
# URL). Paste it into <PREFIX>_HISTORY_URL and the session cookie into
# <PREFIX>_COOKIE. The importer only appends query params and the cookie header.
#
#   UNIBET_HISTORY_URL / UNIBET_COOKIE
#   SEVEN_ELEVEN_HISTORY_URL / SEVEN_ELEVEN_COOKIE
#   BINGOAL_HISTORY_URL / BINGOAL_COOKIE
#   SCOOORE_HISTORY_URL / SCOOORE_COOKIE
#
# Optional, only if the numbers come out wrong against a real capture:
#   KAMBI_MONEY_SCALE (default 1000) — stake/payout are integers scaled by this
#   KAMBI_ODDS_SCALE  (default 1000) — decimal odds scaled by this


_MONEY_SCALE = float(os.getenv("KAMBI_MONEY_SCALE", "1000"))
_ODDS_SCALE = float(os.getenv("KAMBI_ODDS_SCALE", "1000"))


def _first(d: dict, *keys: str) -> Any:
    """Return the first present, non-None value among `keys`. Kambi has renamed
    coupon-history fields across API versions (couponId vs id, createdDate vs
    placedDate, …); trying several keys keeps the parser robust to the version
    a given skin serves."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def _money(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        return round(float(raw) / _MONEY_SCALE, 2)
    except (TypeError, ValueError):
        return None


def _odd(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    # History payloads sometimes carry already-decimal odds (e.g. 2.5) and
    # sometimes the ×1000 integer form (2500). Detect: a plausible decimal odd
    # is < 1000; anything >= 1000 is the scaled integer.
    if v >= 1000:
        return round(v / _ODDS_SCALE, 3)
    return round(v, 3)


def _dt(raw: Any) -> Optional[datetime]:
    """Kambi timestamps are epoch milliseconds; tolerate ISO strings too."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Heuristic: > 10^12 is ms, otherwise seconds.
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
    """Pull the array of coupons out of a history payload, tolerating the
    various envelope keys Kambi has used."""
    if not isinstance(data, dict):
        return []
    for key in ("coupons", "historyCoupons", "couponHistory", "items", "history"):
        val = data.get(key)
        if isinstance(val, list):
            return val
    return []


def _outcomes(coupon: dict) -> list[dict]:
    for key in ("outcomes", "selections", "bets", "legs", "betOffers"):
        val = coupon.get(key)
        if isinstance(val, list):
            return val
    return []


def _event_label_and_key(leg: dict) -> tuple[Optional[str], Optional[str]]:
    """Best-effort event label + normalised event_key from a single leg, so a
    single bet can later be joined to our value_bets. Combos skip this."""
    name = _first(leg, "eventName", "englishName", "name", "matchName")
    home = _first(leg, "homeName", "home")
    away = _first(leg, "awayName", "away")
    start = _dt(_first(leg, "eventStartDate", "eventStart", "start", "startDate"))
    # Derive home/away from "Home - Away" if not given explicitly.
    if (not home or not away) and isinstance(name, str):
        for sep in (" - ", " vs ", " v ", " @ "):
            if sep in name:
                home, away = (p.strip() for p in name.split(sep, 1))
                break
    label = name if isinstance(name, str) else (
        f"{home} - {away}" if home and away else None
    )
    ek = event_key(home, away, start) if (home and away and start) else None
    return label, ek


def parse_history(data: dict, book: Book) -> list[SettledBet]:
    """Parse a Kambi coupon-history payload into SettledBet rows.

    One SettledBet per coupon. Singles get event/market/selection filled from
    the single leg; combos/systems keep those None with legs = number of
    outcomes. Defensive by design — the exact field names are confirmed against
    a real capture (see `inspect-kambi-history`), and every field is looked up
    through `_first` so a version mismatch degrades gracefully instead of
    throwing.
    """
    out: list[SettledBet] = []
    for entry in _coupon_list(data):
        # Some payloads wrap each row as {"coupon": {...}}, others are flat.
        coupon = entry.get("coupon") if isinstance(entry.get("coupon"), dict) else entry
        if not isinstance(coupon, dict):
            continue

        bet_id = _first(coupon, "couponId", "id", "couponRef", "ref", "betId")
        if bet_id is None:
            continue

        stake = _money(_first(coupon, "stake", "totalStake", "amount"))
        payout = _money(_first(coupon, "payout", "payoutAmount", "totalPayout", "returns"))
        total_odds = _odd(_first(coupon, "totalOdds", "odds", "combinedOdds", "totalOdd"))
        result = normalize_result(_first(coupon, "result", "couponResult", "status", "outcome"))
        placed = _dt(_first(coupon, "createdDate", "placedDate", "placed", "createdAt"))
        settled = _dt(_first(coupon, "settledDate", "settled", "settledAt", "resolvedDate"))
        currency = _first(coupon, "currency", "currencyCode")
        ctype = _first(coupon, "couponType", "type", "betType")
        bet_type = str(ctype).lower() if ctype else None

        legs = _outcomes(coupon)
        n_legs = len(legs) or 1

        sb = SettledBet(
            book=book,
            bet_id=str(bet_id),
            odd=total_odds if total_odds is not None else 0.0,
            stake=stake if stake is not None else 0.0,
            result=result,
            placed_at=placed,
            settled_at=settled,
            payout=payout,
            bet_type=bet_type,
            legs=n_legs,
            currency=str(currency) if currency else None,
            raw=json.dumps(coupon, separators=(",", ":"), ensure_ascii=False),
        )

        # Enrich singles from their one leg so match/market/selection are usable.
        if n_legs == 1 and legs:
            leg = legs[0] if isinstance(legs[0], dict) else {}
            sb.event_start = _dt(_first(leg, "eventStartDate", "eventStart", "start"))
            sb.sport = _first(leg, "sport", "sportName", "sportId")
            label, ek = _event_label_and_key(leg)
            sb.event_label = label
            sb.event_key = ek
            sb.market = _first(leg, "criterionName", "criterion", "market", "betOfferType")
            sb.selection = _first(leg, "label", "outcomeLabel", "selection", "participant", "name")
            leg_line = _first(leg, "line", "handicap")
            try:
                sb.line = float(leg_line) / _ODDS_SCALE if leg_line and abs(float(leg_line)) >= 1000 else (
                    float(leg_line) if leg_line is not None else None
                )
            except (TypeError, ValueError):
                sb.line = None
            # If the coupon-level odd was missing, fall back to the single leg's odd.
            if sb.odd == 0.0:
                leg_odd = _odd(_first(leg, "odds", "odd"))
                if leg_odd is not None:
                    sb.odd = leg_odd
        else:
            sb.event_label = f"Combiné {n_legs} sélections"

        out.append(sb)
    return out


class KambiHistoryImporter(HistoryImporter):
    """Bet-history importer for any Kambi skin (Unibet, 711, Bingoal, Scooore).

    Config (env), where PREFIX is e.g. UNIBET / SEVEN_ELEVEN / BINGOAL / SCOOORE:
      <PREFIX>_HISTORY_URL  full coupon/history.json URL, copied from DevTools
      <PREFIX>_COOKIE       the logged-in session Cookie header value
    """

    def __init__(self, book: Book, env_prefix: str, offering: str, timeout: float = 20.0):
        self.book = book
        self._prefix = env_prefix
        self._offering = offering
        self._timeout = timeout

    def _url(self) -> str:
        return os.getenv(f"{self._prefix}_HISTORY_URL", "").strip()

    def _cookie(self) -> str:
        return os.getenv(f"{self._prefix}_COOKIE", "").strip()

    def available(self) -> bool:
        return bool(self._url() and self._cookie())

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "Cookie": self._cookie(),
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def _get(self, url: str, params: dict) -> dict:
        with httpx.Client(timeout=self._timeout, headers=self._headers()) as client:
            r = client.get(url, params=params)
            if r.status_code in (401, 403):
                raise HistoryImportError(
                    f"{self.book.value}: authentification refusée (HTTP {r.status_code}) — "
                    f"cookie {self._prefix}_COOKIE expiré, recapture-le."
                )
            r.raise_for_status()
            return r.json()

    def raw_first_page(self, page_size: int = 20) -> dict:
        """Fetch the first page of history as raw JSON, for `inspect-kambi-history`
        / `--dump-raw` so the exact field names can be confirmed."""
        url = self._url()
        if not url:
            raise HistoryImportError(f"{self.book.value}: {self._prefix}_HISTORY_URL manquant.")
        return self._get(url, {
            "lang": "fr_BE", "market": "BE", "settled": "true",
            "range_size": page_size, "range_start": 0,
        })

    def fetch(
        self, *, since: Optional[datetime] = None, page_size: int = 100, max_pages: int = 20,
    ) -> Iterable[SettledBet]:
        url = self._url()
        if not url:
            raise HistoryImportError(f"{self.book.value}: {self._prefix}_HISTORY_URL manquant.")

        seen_ids: set[str] = set()
        for page in range(max_pages):
            params = {
                "lang": "fr_BE",
                "market": "BE",
                "settled": "true",
                "range_size": page_size,
                "range_start": page * page_size,
            }
            try:
                data = self._get(url, params)
            except httpx.HTTPError as e:
                raise HistoryImportError(f"{self.book.value}: requête échouée — {e}") from e

            bets = parse_history(data, self.book)
            if not bets:
                break

            stop = False
            for sb in bets:
                if sb.bet_id in seen_ids:
                    continue
                seen_ids.add(sb.bet_id)
                # Incremental stop: once we reach bets older than the newest we
                # already stored, the rest of the pages are known history.
                ref = sb.settled_at or sb.placed_at
                if since is not None and ref is not None and ref <= since:
                    stop = True
                    continue
                yield sb
            if stop or len(bets) < page_size:
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
    `available()` (i.e. have a URL + cookie configured)."""
    return [
        KambiHistoryImporter(book, prefix, offering)
        for book, prefix, offering in _KAMBI_BOOKS
    ]
