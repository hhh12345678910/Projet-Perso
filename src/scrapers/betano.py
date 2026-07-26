from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..matcher import event_key, team_similarity
from ..models import Book, MarketType, OddQuote, Outcome
from ..teams import record_pair


BASE = "https://www.betanosports.be/fr/danae-webapi/api"


class BetanoAuthError(RuntimeError):
    """Raised when Cloudflare/DataDome rejects the request (cookie expired)."""


def _is_retryable(exc: BaseException) -> bool:
    """Retry only transient failures. A rejected cookie (BetanoAuthError) or a
    4xx won't recover on retry, so surface it immediately instead of burying it
    in a RetryError after pointless re-attempts."""
    if isinstance(exc, BetanoAuthError):
        return False
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _headers(user_agent: str, x_language: str, x_operator: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "User-Agent": user_agent,
        "Referer": "https://www.betanosports.be/fr/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "x-language": x_language,
        "x-operator": x_operator,
    }


_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def _cookie_file_path() -> str:
    """Where the browser userscript's cookie push lands (written by
    scripts/betano_ingest_server.py)."""
    default = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "betano_cookie.json",
    )
    return os.getenv("BETANO_COOKIE_FILE", default)


def load_pushed_credentials() -> tuple[str, str] | None:
    """Read the cookie + User-Agent most recently pushed by the browser
    userscript. Returns None when no usable push is on disk.

    Read on every scraper construction rather than cached at import: the
    userscript refreshes the file every few minutes, and re-reading is what
    lets a new cookie take effect without restarting the daemon.

    The User-Agent travels with the cookie because Cloudflare/DataDome bind
    the clearance token to the UA that solved the challenge — replaying the
    cookie under a different UA gets it rejected."""
    path = _cookie_file_path()
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    cookie = str(data.get("cookie") or "").strip()
    if not cookie:
        return None
    return cookie, str(data.get("user_agent") or "").strip() or _DEFAULT_UA


class BetanoScraper:
    book = Book.BETANO_BE

    def __init__(
        self,
        cookie: str | None = None,
        user_agent: str | None = None,
        x_language: str | None = None,
        x_operator: str | None = None,
        timeout: float = 15.0,
    ):
        # Precedence: explicit arg > userscript push > .env. The pushed cookie
        # outranks .env because it's the one that auto-refreshes; a stale
        # hand-pasted BETANO_COOKIE should never shadow a fresh push.
        pushed_ua: str | None = None
        if not cookie:
            pushed = load_pushed_credentials()
            if pushed is not None:
                cookie, pushed_ua = pushed
        cookie = cookie or os.getenv("BETANO_COOKIE", "")
        if not cookie:
            raise BetanoAuthError(
                "No Betano cookie. Either run the browser userscript "
                "(tools/betano-ingest.user.js) so it pushes one, or set "
                "BETANO_COOKIE in .env."
            )
        ua = user_agent or pushed_ua or os.getenv("BETANO_USER_AGENT", _DEFAULT_UA)
        xl = x_language or os.getenv("BETANO_X_LANGUAGE", "9")
        xo = x_operator or os.getenv("BETANO_X_OPERATOR", "22")

        self._client = httpx.Client(
            base_url=BASE,
            timeout=timeout,
            headers=_headers(ua, xl, xo),
            cookies=_parse_cookie_header(cookie),
            http2=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BetanoScraper":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self._client.get(path, params=params)
        if r.status_code in (401, 403):
            raise BetanoAuthError(
                f"{r.status_code} from Betano — your cookie likely expired. "
                "Re-capture cf_clearance + datadome from your browser."
            )
        r.raise_for_status()
        return r.json()

    def fetch_live_overview(self, content_version: int | str = "latest", is_init: bool = True) -> dict:
        """Fetch the live overview. Use 'latest' for an initial bulk fetch,
        or pass a previous contentVersion (with is_init=False) to receive a delta."""
        params: dict[str, str] = {"includeVirtuals": "true"}
        if is_init:
            params["queryLanguageId"] = os.getenv("BETANO_X_LANGUAGE", "9")
            params["queryOperatorId"] = os.getenv("BETANO_X_OPERATOR", "22")
        else:
            params["isInit"] = "false"
        return self._get(f"/live/overview/{content_version}", params=params)

    def fetch_prematch_overview(self, content_version: int = 0, is_init: bool = True) -> dict:
        """Best-effort: sibling path of /live/overview. Adjust path here once
        confirmed by capturing a prematch XHR (e.g. on a competition page)."""
        params = {
            "isInit": "true" if is_init else "false",
            "includeVirtuals": "true",
        }
        for path in (
            f"/prematch/overview/{content_version}",
            f"/overview/{content_version}",
            f"/sport/overview/{content_version}",
        ):
            try:
                return self._get(path, params=params)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    continue
                raise
        raise RuntimeError(
            "Could not discover the prematch overview path. Capture a prematch "
            "XHR in DevTools and add its path to fetch_prematch_overview()."
        )


def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out


# Field aliases — the danae-webapi has a few naming conventions across endpoints.
_FIELDS_EVENT_START = ("startTime", "startDate", "kickoff", "eventDate", "date")
_FIELDS_EVENT_PARTICIPANTS = ("participants", "competitors", "teams")
_FIELDS_PARTICIPANT_NAME = ("name", "shortName", "displayName")
_FIELDS_PARTICIPANT_ROLE = ("type", "role", "alignment", "side")
_FIELDS_SELECTION_LABEL = ("name", "label", "outcome", "shortName")
_FIELDS_SELECTION_ODD = ("odds", "price", "decimalOdds", "value")


def _first(d: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _parse_datetime(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        # Milliseconds or seconds since epoch
        ts = float(v)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


# Betano's danae-webapi tags each market with a stable, language-independent
# `type` code. Map only the markets that line up with Pinnacle's references
# (1X2 / moneyline, total goals, handicap). Period/prop markets are skipped.
_MARKET_BY_TYPE = {
    "MRES": MarketType.H2H,        # Résultat de match — 1X2 (3-way)
    "H2HT": MarketType.H2H,        # Vainqueur — 2-way winner / moneyline
    "HCTG": MarketType.TOTALS,     # Total des buts Plus de/Moins de
    "HCAP": MarketType.HANDICAP,   # Handicap
    "FAHC": MarketType.HANDICAP,
    "FHOT": MarketType.HANDICAP,
}


def _market_type(market: dict) -> MarketType | None:
    return _MARKET_BY_TYPE.get(str(market.get("type") or "").upper())


_H2H_DIRECT = {
    "1": "home", "home": "home", "domicile": "home", "local": "home",
    "x": "draw", "draw": "draw", "nul": "draw", "match nul": "draw",
    "2": "away", "away": "away", "exterieur": "away", "visiteur": "away",
}


def _side_from_team(name: str, home: str | None, away: str | None) -> str | None:
    """Map a selection labelled with a team/player name (2-way winner or
    handicap) onto 'home'/'away' by matching against the event participants."""
    if not (home and away):
        return None
    sh = team_similarity(name, home)
    sa = team_similarity(name, away)
    if max(sh, sa) < 60:
        return None
    return "home" if sh >= sa else "away"


def _h2h_label(label: str, home: str | None, away: str | None) -> str | None:
    s = label.strip().lower()
    if s in _H2H_DIRECT:
        return _H2H_DIRECT[s]
    return _side_from_team(label, home, away)


def _normalise_outcome_label(label: str, market: MarketType) -> str:
    s = label.strip().lower()
    if market == MarketType.H2H:
        return _H2H_DIRECT.get(s, s)
    if market == MarketType.TOTALS:
        if s.startswith(("o", "+", "plus")):
            return "over"
        if s.startswith(("u", "-", "moins")):
            return "under"
    return s


def parse_overview(data: dict) -> Iterator[OddQuote]:
    """Walk a danae-webapi overview JSON and yield OddQuote objects.

    Structure (Redux-store style, fully normalised):
        data["events"][event_id]    -> event details
        data["leagues"][league_id]  -> league with eventIdList
        data["markets"][market_id]  -> market details + selectionIdList
        data["selections"][sel_id]  -> selection with odds
    """
    events = data.get("events") or {}
    markets = data.get("markets") or {}
    selections = data.get("selections") or {}

    if not (events and markets and selections):
        return

    now = datetime.now(timezone.utc)

    # Index: market_id -> event_id (preferred via market.eventId; fall back to event.marketIdList)
    market_to_event: dict[str, str] = {}
    for mid, m in markets.items():
        eid = m.get("eventId") or m.get("event_id")
        if eid is not None:
            market_to_event[str(mid)] = str(eid)
    for eid, ev in events.items():
        for mid in (ev.get("marketIdList") or ev.get("markets") or []):
            market_to_event.setdefault(str(mid), str(eid))

    # Index: selection_id -> market_id
    selection_to_market: dict[str, str] = {}
    for sid, s in selections.items():
        mid = s.get("marketId") or s.get("market_id")
        if mid is not None:
            selection_to_market[str(sid)] = str(mid)
    for mid, m in markets.items():
        for sid in (m.get("selectionIdList") or m.get("selections") or []):
            selection_to_market.setdefault(str(sid), str(mid))

    for sid, sel in selections.items():
        mid = selection_to_market.get(str(sid))
        if mid is None:
            continue
        market = markets.get(mid)
        if market is None and mid.isdigit():
            market = markets.get(int(mid))
        if market is None:
            continue

        eid = market_to_event.get(str(mid))
        if eid is None:
            continue
        ev = events.get(eid) or (events.get(int(eid)) if eid.isdigit() else None)
        if ev is None:
            continue

        market_type = _market_type(market)
        if market_type is None:
            continue

        start = _parse_datetime(_first(ev, _FIELDS_EVENT_START))
        if start is None:
            continue

        participants = _first(ev, _FIELDS_EVENT_PARTICIPANTS, default=[])
        home, away = _extract_home_away(participants)
        if not (home and away):
            continue

        odd_raw = _first(sel, _FIELDS_SELECTION_ODD)
        if odd_raw is None:
            continue
        try:
            decimal_odd = float(odd_raw)
        except (TypeError, ValueError):
            continue
        if decimal_odd <= 1.0:
            continue

        raw_label = str(_first(sel, _FIELDS_SELECTION_LABEL, default=""))
        if market_type == MarketType.H2H:
            label = _h2h_label(raw_label, home, away)
        elif market_type == MarketType.HANDICAP:
            label = _side_from_team(raw_label, home, away)
        else:
            label = _normalise_outcome_label(raw_label, market_type)
        if label is None:
            continue

        line = sel.get("line") or sel.get("handicap") or market.get("line")
        try:
            line_val = float(line) if line is not None else None
        except (TypeError, ValueError):
            line_val = None

        record_pair(home, away)
        yield OddQuote(
            event_key=event_key(home, away, start),
            book=Book.BETANO_BE,
            market=market_type,
            outcome=Outcome(label=label, line=line_val),
            decimal_odd=decimal_odd,
            fetched_at=now,
            source_event_id=str(eid),
        )


def _extract_home_away(participants: Any) -> tuple[str | None, str | None]:
    if not isinstance(participants, list):
        return None, None
    named = [(p, _first(p, _FIELDS_PARTICIPANT_NAME)) for p in participants if isinstance(p, dict)]
    named = [(p, n) for p, n in named if n]
    if len(named) < 2:
        return None, None
    names = [n for _, n in named]

    home = away = None
    # 1) `isHome` boolean flag (current danae-webapi shape).
    for p, n in named:
        if p.get("isHome") is True:
            home = n
    # 2) explicit role/alignment field.
    for p, n in named:
        role = _first(p, _FIELDS_PARTICIPANT_ROLE)
        r = str(role).lower() if role is not None else ""
        if r in ("home", "1", "domicile", "h"):
            home = home or n
        elif r in ("away", "2", "exterieur", "visiteur", "a"):
            away = away or n
    # 3) positional fallback.
    if home is None:
        home = names[0]
    if away is None:
        away = next((n for n in names if n != home), None)
    return home, away
