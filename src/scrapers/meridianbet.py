from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Iterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..filter import is_noise_event
from ..matcher import event_key
from ..models import Book, MarketType, OddQuote, Outcome
from ..teams import record_pair


# MeridianBet (meridiansports.be) runs on its own REST API — independent odds,
# not a Kambi/Altenar/Entain reseller, so it genuinely widens value/surebet
# coverage. The prematch offer for a sport comes from one guest endpoint:
#   /betshop/api/v1/offer/sport/{sportId}/leagues?page=N&time=ALL&groupIndices=0,0,0
# groupIndices=0,0,0 returns the headline markets (1X2, totals, BTTS).
BASE = "https://online.mbatbkd.com/betshop/api"

# ── Le jeton ──────────────────────────────────────────────────────────────
#
# L'offre exige `Authorization: Bearer`. Sans lui, l'API répond
# `401 {"error":"invalid_token"}` — c'est ce qui a tenu ce book désactivé, et
# non l'anti-bot TrafficGuard auquel on l'attribuait : un 401 est un refus
# d'authentification, pas un filtrage d'ASN, et l'IP de la VM passe très bien.
#
# LE JETON SE PREND PAR LA PORTE D'ENTRÉE. Le site le fabrique via un
# `POST {AUTH_API}/oauth/token` dont l'en-tête `Basic` se construit sur un nom
# de client volontairement dissimulé dans le bundle JavaScript. On ne touche
# pas à ça : chaque page HTML rendue par le serveur embarque déjà un jeton
# NEUF dans son `<script id="ng-state">`, sous la clé `NEW_TOKEN`. Un simple
# GET anonyme suffit donc, et c'est le mécanisme prévu pour tout visiteur.
#
# C'est un jeton INVITÉ : `scope: ["GENERAL"]`, `permissions: []`, aucun
# compte, aucune capacité de pari. Il ne sert qu'à lire l'offre publique.
_PAGE_JETON = os.getenv(
    "MERIDIAN_TOKEN_URL", "https://meridiansports.be/en/betting/football/")
# Deux identifiants selon la variante servie : `ng-state` hors mobile,
# `meridianbet-mobile-v4-state` sur mobile. Chercher les deux évite qu'un
# changement d'agent utilisateur rende le scraper muet sans erreur.
_IDS_ETAT = ("ng-state", "meridianbet-mobile-v4-state")
# Marge avant expiration : le jeton vit une heure, on le renouvelle avant.
_MARGE_SEC = float(os.getenv("MERIDIAN_TOKEN_MARGIN_SEC", "300"))

_JETON: dict = {"valeur": "", "expire": 0.0}
_VERROU = threading.Lock()


def _expiration(jwt: str) -> float:
    """L'`exp` du jeton, en epoch. 0 si illisible.

    Lue DANS le jeton plutôt que fixée en dur : c'est le serveur qui décide de
    la durée de vie, et un « une heure » codé en dur casserait en silence le
    jour où ils la raccourcissent."""
    try:
        import base64
        corps = jwt.split(".")[1]
        d = json.loads(base64.urlsafe_b64decode(corps + "=" * (-len(corps) % 4)))
        return float(d.get("exp") or 0.0)
    except Exception:                                        # noqa: BLE001
        return 0.0
TIME_FILTER = os.getenv("MERIDIAN_TIME_FILTER", "ALL")

# sportId codes from /betshop/api/v1/standard/sport/active.
SPORT_IDS = {
    "soccer": 58,        # Football
    "tennis": 56,
    "basketball": 55,
    "hockey": 59,        # Ice Hockey
}

# 1X2 / moneyline selection names are locale-independent ("1"/"X"/"2").
_H2H_LABELS = {"1": "home", "X": "draw", "2": "away"}

# Over/Under labels across the three Belgian locales MeridianBet may serve.
_OVER_NAMES = {"over", "plus", "boven"}
_UNDER_NAMES = {"under", "moins", "onder"}


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
        # SANS le `www.` : c'est ce que le navigateur envoie, et une origine
        # qui ne correspond pas est exactement ce qu'un anti-bot vérifie.
        "Origin": "https://meridiansports.be",
        "Referer": "https://meridiansports.be/",
    }


def _odd(price) -> float | None:
    try:
        v = float(price)
    except (TypeError, ValueError):
        return None
    return v if v > 1.0 else None


class MeridianScraper:
    book = Book.MERIDIAN_BE

    def __init__(self, timeout: float = 15.0):
        self._client = httpx.Client(timeout=timeout, headers=_headers())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MeridianScraper":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _prendre_jeton(self, *, force: bool = False) -> str:
        """Le jeton invité, pris dans le `ng-state` d'une page du site.

        Mis en cache et partagé par tous les sports : `fetch_all_parallel`
        lance un fil par sport, et sans verrou chacun irait chercher le sien —
        trois chargements de page au lieu d'un, pour rien."""
        with _VERROU:
            maintenant = time.time()
            if (not force and _JETON["valeur"]
                    and maintenant < _JETON["expire"] - _MARGE_SEC):
                return _JETON["valeur"]
            r = self._client.get(_PAGE_JETON, headers={
                "Accept": "text/html,application/xhtml+xml",
            })
            r.raise_for_status()
            etat = None
            for ident in _IDS_ETAT:
                m = re.search(rf'<script id="{ident}"[^>]*>(.*?)</script>',
                              r.text, re.S)
                if m:
                    etat = json.loads(m.group(1))
                    break
            if etat is None:
                raise RuntimeError(
                    f"aucun <script id> parmi {_IDS_ETAT} dans {_PAGE_JETON} — "
                    f"la page a changé de forme")
            brut = etat.get("NEW_TOKEN")
            # La valeur est du JSON ENCODÉ DANS UNE CHAÎNE. Un `.get()` direct
            # rendrait la chaîne entière et l'en-tête partirait invalide.
            if isinstance(brut, str):
                brut = json.loads(brut)
            jeton = (brut or {}).get("access_token") or ""
            if not jeton:
                raise RuntimeError(
                    f"NEW_TOKEN sans access_token dans {_PAGE_JETON}")
            _JETON["valeur"] = jeton
            _JETON["expire"] = _expiration(jeton) or (maintenant + 3600)
            return jeton

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
    )
    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self._client.get(
            f"{BASE}{path}", params=params,
            headers={"Authorization": f"Bearer {self._prendre_jeton()}"})
        if r.status_code == 401:
            # Un jeton périmé est le mode d'échec ATTENDU, pas une anomalie :
            # on en reprend un et on rejoue, UNE fois. Boucler indéfiniment sur
            # un vrai changement d'authentification martèlerait le site sans
            # jamais aboutir.
            r = self._client.get(
                f"{BASE}{path}", params=params,
                headers={"Authorization":
                         f"Bearer {self._prendre_jeton(force=True)}"})
        r.raise_for_status()
        return r.json()

    def fetch_offer_page(self, sport: str, page: int) -> dict:
        sport_id = SPORT_IDS.get(sport, sport)
        return self._get(
            f"/v1/offer/sport/{sport_id}/leagues",
            params={"page": page, "time": TIME_FILTER, "groupIndices": "0,0,0"},
        )

    def fetch_all_events(self, sport: str = "soccer", *, max_pages: int = 25) -> dict:
        """Page through the offer until a page returns no leagues, merging every
        league's events into one payload (the shape parse_offer consumes).

        The offer endpoint is 0-indexed: page 0 carries the first (and often the
        headline) leagues. Starting at page 1 silently dropped that whole first
        page — a chunk of the offer every scan — so we start at 0."""
        leagues: list = []
        for page in range(0, max_pages):
            try:
                payload = (self.fetch_offer_page(sport, page) or {}).get("payload") or {}
            except httpx.HTTPError:
                break
            page_leagues = payload.get("leagues") or []
            if not page_leagues:
                break
            leagues.extend(page_leagues)
        return {"payload": {"leagues": leagues}}


def _market_and_label(group: dict, name: str):
    """Map a MeridianBet market group + selection name to our (MarketType, label,
    line). Returns None for markets we don't track (handicap, BTTS, ...)."""
    over_under = group.get("overUnder")
    handicap = group.get("handicap")
    # Totals: an over/under line is set and the market isn't a handicap.
    if over_under is not None and handicap is None:
        low = name.strip().lower()
        if low in _OVER_NAMES:
            return MarketType.TOTALS, "over", float(over_under)
        if low in _UNDER_NAMES:
            return MarketType.TOTALS, "under", float(over_under)
        return None
    # 1X2 / moneyline: no line, selection named 1 / X / 2.
    if over_under is None and handicap is None and name in _H2H_LABELS:
        return MarketType.H2H, _H2H_LABELS[name], None
    return None


def parse_offer(data: dict) -> Iterator[OddQuote]:
    """Walk a MeridianBet offer payload and yield OddQuote objects.

    Shape: payload.leagues[].events[] = {"header": {...}, "positions": [...]}.
    Each position has groups (markets); each group has selections (outcomes with
    a decimal `price`)."""
    now = datetime.now(timezone.utc)
    payload = data.get("payload") or {}
    for league in payload.get("leagues") or []:
        league_name = league.get("leagueName", "")
        for ev in league.get("events") or []:
            header = ev.get("header") or {}
            rivals = header.get("rivals") or []
            if len(rivals) != 2 or not (rivals[0] and rivals[1]):
                continue
            home, away = rivals[0], rivals[1]
            start_ms = header.get("startTime")
            if not start_ms:
                continue
            start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
            if is_noise_event(home, away, league_name):
                continue
            record_pair(home, away)
            ek = event_key(home, away, start)
            source_id = str(header.get("eventId", ""))

            for pos in ev.get("positions") or []:
                for group in pos.get("groups") or []:
                    for sel in group.get("selections") or []:
                        mapped = _market_and_label(group, sel.get("name", ""))
                        if mapped is None:
                            continue
                        market, label, line = mapped
                        decimal_odd = _odd(sel.get("price"))
                        if decimal_odd is None:
                            continue
                        yield OddQuote(
                            event_key=ek,
                            book=Book.MERIDIAN_BE,
                            market=market,
                            outcome=Outcome(label=label, line=line),
                            decimal_odd=decimal_odd,
                            fetched_at=now,
                            source_event_id=source_id,
                        )
