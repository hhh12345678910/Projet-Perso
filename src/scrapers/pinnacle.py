from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Iterable, Iterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..filter import is_noise_event
from ..matcher import event_key
from ..models import Book, Event, MarketType, OddQuote, Outcome
from ..teams import record_pair


PINNACLE_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"

# Public frontend key (used by their browser app). Override via env if needed.
PINNACLE_API_KEY = os.getenv(
    "PINNACLE_API_KEY",
    "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R",
)

SPORT_IDS = {
    "soccer": 29,
    "tennis": 33,
    "basketball": 4,
    "hockey": 19,
    "esports": 12,
    "volleyball": 34,
}


# Le calendrier coûte deux fois le prix des cotes, et ne change presque jamais
# ----------------------------------------------------------------------------
# Mesuré contre l'API le 01/08, football :
#
#   /sports/29/matchups          3,35 Mo gzip  (34,0 Mo décompressés)
#   /sports/29/markets/straight  1,70 Mo gzip  (23,3 Mo décompressés)
#
# Les deux étaient rechargés à chaque cycle, soit ~5 Mo compressés toutes les
# 35 s — 12 Go par jour pour le seul football. C'est ce volume, et non le
# nombre de requêtes, qui déclenche les 403 : quatre requêtes par cycle ne
# saturent aucun limiteur, cinquante mégaoctets par minute si.
#
# Or `matchups` ne porte AUCUN prix. Il porte les participants, l'heure de
# coup d'envoi, la ligue et le drapeau isLive — un calendrier, qui bouge à
# l'échelle de l'heure, pas de la demi-minute. Le recharger à chaque cycle
# achetait deux tiers du trafic pour une information identique.
#
# Le garder en cache quelques minutes ne périme donc aucune cote : seul
# `markets/straight` est réinterrogé à chaque cycle, et c'est lui qui porte
# l'intégralité des prix.
#
# Deux effets de bord, tous deux bornés :
#   - un match qui vient de commencer garde isLive=False jusqu'à l'expiration.
#     Sans conséquence : find_value_bets rejette déjà tout événement dont
#     l'heure de coup d'envoi est passée (ScanConfig.scan_live_value_bets).
#   - un match ajouté au calendrier n'apparaît qu'au rafraîchissement suivant.
#     Sans conséquence non plus : on parie sur du prématch à plusieurs heures.
_MATCHUPS_TTL = float(os.getenv("PINNACLE_MATCHUPS_TTL_SEC", "300"))
_MATCHUPS_CACHE: dict[int, tuple[float, dict]] = {}
_MATCHUPS_LOCK = threading.Lock()


def matchups_cache_clear() -> None:
    """Vide le cache calendrier. Pour les tests, et pour un rechargement forcé."""
    with _MATCHUPS_LOCK:
        _MATCHUPS_CACHE.clear()


def _is_retryable(exc: BaseException) -> bool:
    """Retry transient failures only — network errors, 429, and 5xx. A 403/404
    won't fix itself on retry (and retrying a 403 just deepens a rate-limit ban)."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _american_to_decimal(price: object) -> float | None:
    """Pinnacle's arcadia API returns moneyline prices as American odds
    (e.g. -158, +289). Convert to decimal odds for the rest of the engine."""
    try:
        a = float(price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    return 1.0 + (a / 100.0 if a > 0 else 100.0 / abs(a))


def _headers() -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.pinnacle.com/",
        "Origin": "https://www.pinnacle.com",
        "X-API-Key": PINNACLE_API_KEY,
        "Accept": "application/json",
    }


class PinnacleScraper:
    book = Book.PINNACLE

    def __init__(self, timeout: float = 10.0, request_delay: float | None = None):
        # Route control for Pinnacle traffic only (the rest of the app keeps the
        # default route), used to dodge a Cloudflare/IP block on the host:
        #   PINNACLE_PROXY   -> send Pinnacle via an HTTP/SOCKS proxy with a
        #                       clean (e.g. residential) IP. Takes priority.
        #   PINNACLE_LOCAL_IP-> bind outbound to a specific source IP
        #                       (e.g. an OVH additional IP).
        proxy = os.getenv("PINNACLE_PROXY", "").strip()
        local_ip = os.getenv("PINNACLE_LOCAL_IP", "").strip()
        kwargs: dict = {"timeout": timeout, "headers": _headers()}
        if proxy:
            kwargs["proxy"] = proxy
        elif local_ip:
            kwargs["transport"] = httpx.HTTPTransport(local_address=local_ip)
        self._client = httpx.Client(**kwargs)
        # Light throttle between requests to stay under Pinnacle's rate limit.
        self._delay = request_delay if request_delay is not None else float(
            os.getenv("PINNACLE_REQUEST_DELAY", "0.3")
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PinnacleScraper":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    def _get(self, path: str, params: dict | None = None) -> list | dict:
        if self._delay:
            time.sleep(self._delay)
        r = self._client.get(f"{PINNACLE_BASE}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def list_leagues(self, sport: str) -> list[dict]:
        sport_id = SPORT_IDS[sport]
        data = self._get(f"/sports/{sport_id}/leagues", {"all": "false", "brandId": "0"})
        return data if isinstance(data, list) else []

    def list_matchups(self, league_id: int) -> list[dict]:
        data = self._get(f"/leagues/{league_id}/matchups")
        return [m for m in (data if isinstance(data, list) else []) if not m.get("parent")]

    def list_straight_markets(self, league_id: int) -> list[dict]:
        data = self._get(f"/leagues/{league_id}/markets/straight")
        return data if isinstance(data, list) else []

    def fetch_events(self, sport: str) -> Iterator[Event]:
        for league in self.list_leagues(sport):
            league_id = league.get("id")
            league_name = league.get("name", "?")
            if not league_id:
                continue
            try:
                matchups = self.list_matchups(league_id)
            except httpx.HTTPError:
                continue
            for m in matchups:
                participants = m.get("participants") or []
                if len(participants) < 2:
                    continue
                home = next((p["name"] for p in participants if p.get("alignment") == "home"), None)
                away = next((p["name"] for p in participants if p.get("alignment") == "away"), None)
                if not home or not away:
                    continue
                start_raw = m.get("startTime")
                if not start_raw:
                    continue
                start = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
                yield Event(
                    sport=sport,
                    league=league_name,
                    home=home,
                    away=away,
                    start_time=start,
                    source_id=str(m["id"]),
                    book=Book.PINNACLE,
                )

    def fetch_quotes(self, event: Event) -> Iterable[OddQuote]:
        # Cheaper to fetch all markets per league once; kept per-event for interface symmetry.
        # See orchestrator for the batch path.
        return []

    def _matchups_by_id(self, sport_id: int) -> dict:
        """Calendrier indexé par matchupId, rechargé au plus toutes les
        _MATCHUPS_TTL secondes. Voir le commentaire près de _MATCHUPS_CACHE :
        c'est la plus grosse réponse de l'API et celle qui bouge le moins.

        Le cache retient le dictionnaire déjà indexé, pas le JSON brut : sur le
        football, réindexer 34 Mo à chaque cycle coûtait aussi du CPU sur une
        petite VM."""
        now = time.monotonic()
        with _MATCHUPS_LOCK:
            hit = _MATCHUPS_CACHE.get(sport_id)
            if hit is not None and (now - hit[0]) < _MATCHUPS_TTL:
                return hit[1]

        matchups = self._get(f"/sports/{sport_id}/matchups")
        if not isinstance(matchups, list):
            return {}
        # Index prematch matchups by id (skip derivative "parent" entries).
        indexed = {
            m["id"]: m for m in matchups
            if isinstance(m, dict) and m.get("id") is not None and not m.get("parent")
        }
        with _MATCHUPS_LOCK:
            _MATCHUPS_CACHE[sport_id] = (time.monotonic(), indexed)
        return indexed

    def fetch_market_quotes(self, sport: str) -> Iterator[OddQuote]:
        """Pull an entire sport in bulk instead of fanning out one request per
        league. This cuts Pinnacle traffic ~100x — the old per-league fan-out
        (≈200 requests per sport per cycle) is what got the guest API to
        rate-limit/ban the IP.

        Deux points d'appel, de coûts très différents : le calendrier
        (`matchups`) est mis en cache quelques minutes, seuls les prix
        (`markets/straight`) sont rechargés à chaque cycle."""
        now = datetime.now(timezone.utc)
        sport_id = SPORT_IDS[sport]
        matchups_by_id = self._matchups_by_id(sport_id)
        markets = self._get(f"/sports/{sport_id}/markets/straight")
        if not matchups_by_id or not isinstance(markets, list):
            return

        for market in markets:
            if not isinstance(market, dict):
                continue
            # Only full-match (period 0); per-period prices would contaminate
            # the (event, market, line) devig groups.
            if market.get("period") != 0:
                continue
            if market.get("status") not in (None, "open"):
                continue
            matchup = matchups_by_id.get(market.get("matchupId"))
            if matchup is None or matchup.get("isLive"):
                continue

            participants = matchup.get("participants") or []
            home = next((p["name"] for p in participants if p.get("alignment") == "home"), None)
            away = next((p["name"] for p in participants if p.get("alignment") == "away"), None)
            start_raw = matchup.get("startTime")
            if not (home and away and start_raw):
                continue
            league_name = (matchup.get("league") or {}).get("name", "")
            if is_noise_event(home, away, league_name):
                continue
            start = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
            record_pair(home, away)
            ek = event_key(home, away, start)

            market_type = self._map_market(market.get("type"))
            if market_type is None:
                continue

            for p in market.get("prices") or []:
                if p.get("points") is not None and market_type == MarketType.H2H:
                    continue
                decimal_odd = _american_to_decimal(p.get("price"))
                if decimal_odd is None:
                    continue
                designation = p.get("designation") or "?"
                label = self._designation_label(designation, market_type, home, away)
                yield OddQuote(
                    event_key=ek,
                    book=Book.PINNACLE,
                    market=market_type,
                    outcome=Outcome(label=label, line=p.get("points")),
                    decimal_odd=decimal_odd,
                    fetched_at=now,
                    source_event_id=str(market.get("matchupId")),
                    # La ligue était lue puis jetée : elle servait au filtre de
                    # bruit et n'allait pas plus loin. Pinnacle est la seule
                    # source qui la nomme pour TOUS les événements de la
                    # référence, donc c'est ici, et nulle part ailleurs, qu'on
                    # peut la rattacher à chaque event_key.
                    league=league_name or None,
                )

    @staticmethod
    def _map_market(t: str | None) -> MarketType | None:
        return {
            "moneyline": MarketType.H2H,
            "total": MarketType.TOTALS,
            "spread": MarketType.HANDICAP,
        }.get(t or "")

    @staticmethod
    def _designation_label(designation: str, market: MarketType, home: str, away: str) -> str:
        d = designation.lower()
        if market == MarketType.H2H:
            return {"home": "home", "away": "away", "draw": "draw"}.get(d, d)
        if market == MarketType.TOTALS:
            return {"over": "over", "under": "under"}.get(d, d)
        return d
