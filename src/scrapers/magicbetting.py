from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..matcher import event_key
from ..models import Book, MarketType, OddQuote, Outcome
from ..teams import record_pair


# Magic Betting embeds an iframe served by BetConstruct (sport-ak.bldiframe.com).
# The events endpoint is gated by Cloudflare from datacenter IPs, so live
# fetching only works from the user's machine; the response is XOR'd, so the
# decoder is also exposed via decode_response().
BASE = "https://sport-ak.bldiframe.com"
PARTNER_ID = "3000270"        # Magic Betting's BetConstruct partner id
PARTNER_GUID = "4e9954e6-76b5-4bd4-9801-2cdf644246b0"  # path segment


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
        "Referer": f"{BASE}/",
    }


def decode_response(raw: bytes) -> dict:
    """Magic Betting/BetConstruct obfuscates responses with a tiny cipher: each
    byte of the original JSON is XOR'd with 0x02, and `\\x00` bytes are
    sprinkled in as padding/noise. Drop the nulls, XOR everything else, and
    parse what's left."""
    import json as _json
    cleaned = raw.replace(b"\x00", b"")
    text = bytes((b ^ 0x02) for b in cleaned).decode("utf-8", "replace")
    # The first decoded byte is the original XOR-2 padding; strip it + any
    # surrounding whitespace, then trim to the {...} JSON span.
    text = text.lstrip("\x02").lstrip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("decoded response contains no JSON object")
    return _json.loads(text[start:end + 1])


class MagicBettingScraper:
    book = Book.MAGIC_BETTING

    def __init__(self, timeout: float = 20.0):
        self._client = httpx.Client(timeout=timeout, headers=_headers())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MagicBettingScraper":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    def fetch_tournament_events(self, tournament_id: int, *, sport_id: int = 1) -> dict:
        """Fetch + decode every prematch event of one tournament. From a
        residential IP only — Cloudflare 403s datacenter sources."""
        params = {
            "startDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "endDate": "2036-05-25T21:59:59.999Z",
            "period": 0,
            "tournamentId": tournament_id,
            # Stake types we care about: 1 = 1X2, 3 = Totals, 2 = Handicap.
            "stakeTypes": [1, 2, 3],
            "isTournament": "false",
            "eventFilterType": 0,
            "includeLiveEvents": "false",
            "langId": 16,
            "partnerId": PARTNER_ID,
            "countryCode": "BE",
        }
        r = self._client.get(
            f"/{PARTNER_GUID}/common/getmixedsportandeventslistwithoutright",
            params=params,
        )
        r.raise_for_status()
        return decode_response(r.content)


# BetConstruct's stable per-market group IDs (StakeType.Id).
_MARKET_BY_GROUP_ID = {
    1: MarketType.H2H,        # 1X2 / "Paris sur les matchs"
    2: MarketType.HANDICAP,   # "Handicap des buts" — alternative lines
    3: MarketType.TOTALS,     # "Total des buts" — alternative over/under lines
}

# Per-stake `SC` (selection code) is the position role within a market.
# For 1X2: 1 = home, 2 = draw, 3 = away.
# For Totals: 1 = over, 2 = under.
# For Handicap: 1 = home, 2 = away.
_LABEL_BY_SC = {
    MarketType.H2H: {1: "home", 2: "draw", 3: "away"},
    MarketType.TOTALS: {1: "over", 2: "under"},
    MarketType.HANDICAP: {1: "home", 2: "away"},
}


def _parse_event_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_events(payload: dict, *, book: Book = Book.MAGIC_BETTING) -> Iterator[OddQuote]:
    """Walk a decoded BetConstruct getmixedsportandeventslistwithoutright
    payload and yield OddQuote objects."""
    events = payload.get("events") or []
    if not events:
        return
    now = datetime.now(timezone.utc)

    for ev in events:
        home = ev.get("HT")
        away = ev.get("AT")
        start = _parse_event_time(ev.get("D"))
        if not (home and away and start):
            continue
        record_pair(home, away)
        ek = event_key(home, away, start)
        source_id = str(ev.get("Id") or "")

        for st in ev.get("StakeTypes") or []:
            market_type = _MARKET_BY_GROUP_ID.get(st.get("Id"))
            if market_type is None:
                continue
            label_map = _LABEL_BY_SC[market_type]
            for s in st.get("Stakes") or []:
                label = label_map.get(s.get("SC"))
                if label is None:
                    continue
                try:
                    decimal_odd = float(s.get("F"))
                except (TypeError, ValueError):
                    continue
                if decimal_odd <= 1.0:
                    continue
                line_raw = s.get("A")
                try:
                    line = float(line_raw) if line_raw is not None else None
                except (TypeError, ValueError):
                    line = None
                yield OddQuote(
                    event_key=ek,
                    book=book,
                    market=market_type,
                    outcome=Outcome(label=label, line=line),
                    decimal_odd=decimal_odd,
                    fetched_at=now,
                    source_event_id=source_id,
                )


def load_file(path: str) -> dict:
    """Read a Magic Betting capture from disk. Accepts either the raw
    XOR-encoded response body OR an already-decoded JSON dump."""
    import json as _json
    with open(path, "rb") as f:
        raw = f.read()
    # Already-decoded JSON dumps start with '{' (or whitespace then '{').
    if raw.lstrip().startswith(b"{"):
        return _json.loads(raw)
    return decode_response(raw)
