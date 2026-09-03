from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx
from concurrent.futures import ThreadPoolExecutor, as_completed
from tenacity import retry, stop_after_attempt, wait_exponential

from ..filter import is_noise_event
from ..matcher import event_key
from ..models import Book, MarketType, OddQuote, Outcome
from ..teams import record_pair


# Unibet.be runs on the Kambi sportsbook platform. The public "offering" API is
# guest-accessible (no auth cookie needed). The offering code for Unibet
# Belgium is "ubbe".
BASE = "https://eu-offering-api.kambicdn.com/offering/v2018"
OFFERING = os.getenv("UNIBET_OFFERING", "ubbe")

# Our sport name -> Kambi path term key.
SPORT_TERMS = {
    "soccer": "football",
    "tennis": "tennis",
    "basketball": "basketball",
    "hockey": "ice_hockey",
    "esports": "esports",
    "volleyball": "volleyball",
}


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Origin": "https://www.unibet.be",
        "Referer": "https://www.unibet.be/",
    }


class UnibetScraper:
    book = Book.UNIBET_BE

    def __init__(self, timeout: float = 15.0):
        self._client = httpx.Client(timeout=timeout, headers=_headers())

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "UnibetScraper":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self._client.get(f"{BASE}/{OFFERING}{path}", params=params)
        r.raise_for_status()
        return r.json()

    # Kambi top-level termKey -> internal group ID (discovered via /group.json).
    # Each is used to enumerate every leaf termKey under that sport.
    SPORT_GROUP_IDS = {
        "soccer": "1000093190",      # football
        "tennis": "1000093193",
        "basketball": "1000093204",
        "hockey": "1000093191",      # ice_hockey
        "esports": "2000077768",
        "volleyball": "1000093214",
    }

    def fetch_listview(self, sport: str = "soccer", path_suffix: str = "") -> dict:
        """Fetch the list view for a sport. The optional `path_suffix` lets us
        drill into a specific termKey (e.g. 'england/premier_league'), which
        the Kambi public API exposes as `listView/<sport>/<suffix>.json`."""
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
        """Walk a sport's group tree and return every depth-1 termKey
        (countries + cross-region tournaments like 'champions_league'). Drives
        the bulk listView fetch in fetch_all_events."""
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

    # Back-compat alias for callers that already used the football-only name.
    def fetch_football_term_keys(self) -> list[str]:
        return self.fetch_sport_term_keys("soccer")

    def fetch_all_events(self, sport: str = "soccer", *, max_terms: int = 100) -> dict:
        """Iterate over every termKey of a sport (country / competition) and
        merge the events lists into a single payload. Returns the same shape
        parse_listview consumes."""
        try:
            term_keys = self.fetch_sport_term_keys(sport)
        except httpx.HTTPError:
            term_keys = []
        # Always include the bare term as a sentinel so we still ship something
        # if the tree walk fails.
        term_keys = [""] + term_keys[:max_terms]

        # UNE REQUÊTE PAR COMPÉTITION, ET ELLES ÉTAIENT EN FILE.
        #
        # Mesuré le 03/09 par `scripts/book_latency.py` : Unibet tenait le
        # chemin critique du cycle 52 % du temps, médiane 12,1 s mais p90 à
        # 23,4 s et pointe à 25,2 s. Ce n'est pas un scraper lent, c'est une
        # boucle série : le coût vaut N × latence, et N varie avec le nombre de
        # compétitions du jour. Une seule requête qui repart en tenacity
        # (3 tentatives, recul de 1 à 8 s) ajoute son recul à TOUTES les
        # suivantes.
        #
        # ⚠️ PARALLÉLISME VOLONTAIREMENT MODESTE. Kambi limite le débit — c'est
        # la raison pour laquelle les trois jumeaux (711, Bingoal, Scooore)
        # sont désactivés dans `orchestration.fetch_all_parallel`. Lâcher 100
        # requêtes d'un coup ferait courir à Unibet le risque qui a déjà coûté
        # les trois autres, et Unibet est l'un des deux books du canal premium.
        # Le plafond reste donc du même ordre que ce que les jumeaux ajoutaient
        # avant leur coupure, et il est réglable sans déploiement.
        #
        # ⚠️ L'ORDRE DE FUSION EST PRÉSERVÉ. La déduplication garde le PREMIER
        # exemplaire d'un event_id, donc fusionner dans l'ordre d'arrivée des
        # threads changerait quel exemplaire gagne — un changement de données
        # silencieux, déguisé en optimisation. Les résultats sont rangés par
        # index de termKey et fusionnés dans l'ordre d'origine.
        # ⚠️ UNE FAUTE DE FRAPPE DANS .env NE DOIT PAS FAIRE TAIRE UN BOOK.
        # `int()` sur une valeur illisible lève, et l'exception remonterait
        # jusqu'à `fetch_all_parallel` qui la journalise en « Unibet skipped »
        # — un book du canal premium éteint par un réglage mal écrit, sans que
        # personne ne fasse le lien. On retombe sur le défaut en le disant.
        _brut = os.getenv("UNIBET_PARALLEL_TERMS", "6")
        try:
            ouvriers = max(1, int(_brut))
        except ValueError:
            print(f"[unibet] UNIBET_PARALLEL_TERMS={_brut!r} illisible — "
                  f"6 par défaut")
            ouvriers = 6
        par_index: dict[int, dict] = {}
        if ouvriers == 1 or len(term_keys) <= 1:
            for i, tk in enumerate(term_keys):
                try:
                    par_index[i] = self.fetch_listview(sport, tk)
                except httpx.HTTPError:
                    continue
        else:
            with ThreadPoolExecutor(max_workers=ouvriers) as ex:
                futs = {ex.submit(self.fetch_listview, sport, tk): i
                        for i, tk in enumerate(term_keys)}
                for fut in as_completed(futs):
                    try:
                        par_index[futs[fut]] = fut.result()
                    except httpx.HTTPError:
                        continue
                    except Exception:
                        # Un `fetch_listview` peut lever autre chose qu'une
                        # HTTPError (JSON illisible, tenacity à bout). En série
                        # ça faisait tomber toute la collecte Unibet ; ici ça ne
                        # doit pas non plus faire tomber les autres termKeys.
                        continue

        seen_ids: set = set()
        merged: list = []
        for i in range(len(term_keys)):
            data = par_index.get(i)
            if not data:
                continue
            for ev in data.get("events") or []:
                eid = (ev.get("event") or {}).get("id")
                if eid is None or eid in seen_ids:
                    continue
                seen_ids.add(eid)
                merged.append(ev)
        return {"events": merged}


def _odd(raw: Any) -> float | None:
    """Kambi odds are decimal odds * 1000 (e.g. 1350 -> 1.35)."""
    if raw is None:
        return None
    try:
        return int(raw) / 1000.0
    except (TypeError, ValueError):
        return None


def _line(raw: Any) -> float | None:
    """Kambi lines are also scaled by 1000 (e.g. 2500 -> 2.5)."""
    if raw is None:
        return None
    try:
        return int(raw) / 1000.0
    except (TypeError, ValueError):
        return None


def _parse_datetime(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


# betOfferType id -> our market type.
_MARKET_BY_TYPE_ID = {
    2: MarketType.H2H,        # "Match" (1X2 / full-time result)
    6: MarketType.TOTALS,     # "Over/Under"
    11: MarketType.HANDICAP,  # "Handicap"
    12: MarketType.HANDICAP,
}

# Kambi outcome type -> normalised outcome label.
_OUTCOME_LABELS = {
    "OT_ONE": "home",
    "OT_CROSS": "draw",
    "OT_TWO": "away",
    "OT_OVER": "over",
    "OT_UNDER": "under",
}


def parse_listview(data: dict) -> Iterator[OddQuote]:
    """Walk a Kambi listView payload and yield OddQuote objects.

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
                    book=Book.UNIBET_BE,
                    market=market,
                    outcome=Outcome(label=label, line=_line(o.get("line"))),
                    decimal_odd=decimal_odd,
                    fetched_at=now,
                    source_event_id=source_id,
                )
