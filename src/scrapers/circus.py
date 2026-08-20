"""Circus (plateforme Gaming1 / Ardent) — lecture d'un dump poussé par le navigateur.

Pourquoi un dump et pas un scraper direct
-----------------------------------------
Gaming1 refuse l'IP de la VM sur TOUT le domaine, pas seulement sur son API :
même la page d'accueil répond 403. Ce n'est pas un géoblocage — une IP Google
Cloud belge est refusée aussi — c'est l'ASN datacenter. Un userscript tourne
donc dans un vrai navigateur, ouvre sa propre WebSocket vers le serveur de
cotes, réclame le prématch et pousse le résultat vers la VM (même principe que
Betano, voir scripts/betano_ingest_server.py).

Le transport est une WebSocket, jamais du HTTP : un export HAR d'une session de
navigation ne contient donc aucune cote. C'est `tools/websocket-spy.user.js`
qui a permis de reconstituer le protocole.

Le même code vaut pour Bet777 et Magic Betting : seuls changent l'adresse du
serveur et le RoomDomainName, côté userscript.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, Iterator

from ..matcher import event_key
from ..models import Book, MarketType, OddQuote, Outcome
from ..teams import record_pair


# Gaming1 nomme ses marchés par un BetType textuel. Seuls ceux qui ont une
# contrepartie chez Pinnacle sont exploitables : comparer un marché à une
# référence qui ne le price pas fabrique des value bets fantômes.
#
# Volontairement exclus :
#   1st-half-*            mi-temps. ⚠️ Le motif écrit ici — « Pinnacle ne price
#                         que le match complet » — est FAUX, mesuré le 20/08 :
#                         Pinnacle publie 18 098 marchés de période 1 au
#                         football, dont 6 475 moneyline et 4 880 totals. La
#                         croyance venait de notre propre filtre
#                         (pinnacle.py, `period != 0`), pas de l'API. Le vrai
#                         blocage est ailleurs : OddQuote n'a pas de champ
#                         `period` et le devig groupe sur
#                         (event_key, market, line), donc une mi-temps et un
#                         match plein tomberaient dans le même groupe. Voir
#                         §21.13.
#   both-teams-to-score   aucune référence sharp
#   draw-no-bet           idem
#   1X12X2                double chance, idem
#   to-qualify-*          qualification, se joue sur deux matchs
#   handicap-*            les conventions de signe diffèrent d'un book à
#                         l'autre ; find_value_bets écarte déjà les handicaps
_MARKETS: dict[str, MarketType] = {
    # Football — codes confirmés sur capture réelle.
    "P1XP2": MarketType.H2H,
    "total-OverUnder": MarketType.TOTALS,
    "total-goals-OverUnder": MarketType.TOTALS,
    # Tennis — codes confirmés sur capture réelle.
    #
    # Circus écrit le total de jeux de DEUX façons, et les deux coexistent dans
    # le même dump : 43 marchés en `over-under`, 24 en `OverUnder`. Ne
    # reconnaître que la seconde faisait perdre 64 % des totaux tennis. Les
    # lignes (18,5 à 24,5) confirment qu'il s'agit bien de jeux du match
    # complet, pas de sets — comparer un total de sets au total de jeux de
    # Pinnacle n'aurait rien donné de bon.
    #
    # Ne PAS ajouter de variante par ressemblance : le comparatif se fait par
    # égalité exacte, et first-set-total-games-over-under-OverUnder contient
    # `total-games-over-under` en sous-chaîne tout en ne mesurant qu'un set.
    "P1P2": MarketType.H2H,
    "total-games-OverUnder": MarketType.TOTALS,
    "total-games-over-under": MarketType.TOTALS,
}

_OVER = {"plus de", "over", "meer dan"}
_UNDER = {"moins de", "under", "minder dan"}
_DRAW = {"x", "nul", "draw"}


def _parse_start(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _h2h_label(name: str, home: str, away: str) -> str | None:
    n = (name or "").strip()
    if n.lower() in _DRAW:
        return "draw"
    if n == home:
        return "home"
    if n == away:
        return "away"
    return None


def _totals_label(name: str) -> str | None:
    n = (name or "").strip().lower()
    if n in _OVER:
        return "over"
    if n in _UNDER:
        return "under"
    return None


def parse_prematch(
    data: dict, *, fetched_at: datetime | None = None,
    unknown_types: set[str] | None = None,
    expect_sport_id: int | None = None,
    foreign: list[int] | None = None,
) -> Iterator[OddQuote]:
    """Transforme une réponse GetPrematchSport en OddQuote.

    `unknown_types` recueille les BetType non reconnus. Les signaler plutôt que
    les jeter en silence est ce qui avait permis de rattraper un marché oublié
    sur Betano ; un book qui renomme un code passerait autrement inaperçu.

    `expect_sport_id` refuse les ligues d'un autre sport. Le pont pousse un
    fichier par sport, mais rien dans le fichier ne garantit qu'il est arrivé
    au bon endroit — c'est déjà arrivé, deux réponses de tailles très
    différentes s'étant croisées. Chaque ligue porte son SportId : on s'en sert
    plutôt que de faire confiance au nom du fichier. Les intrus sont collectés
    dans `foreign` pour être signalés, jamais jetés en silence."""
    now = fetched_at or datetime.now(timezone.utc)

    for league in data.get("Leagues") or []:
        league_name = (league or {}).get("LeagueName") or ""
        if expect_sport_id is not None:
            sid = (league or {}).get("SportId")
            if sid is not None and sid != expect_sport_id:
                if foreign is not None:
                    foreign.append(sid)
                continue
        for event in (league or {}).get("Events") or []:
            home = (event.get("HomeName") or "").strip()
            away = (event.get("AwayName") or "").strip()
            start = _parse_start(event.get("StartDate"))
            if not (home and away and start):
                continue
            # Un événement bloqué n'accepte plus de mise : ses cotes existent
            # encore mais ne sont pas jouables.
            if event.get("IsBlocked"):
                continue
            record_pair(home, away)
            ek = event_key(home, away, start)

            for market in event.get("Markets") or []:
                bet_type = market.get("BetType")
                market_type = _MARKETS.get(bet_type)
                if market_type is None:
                    if unknown_types is not None and bet_type:
                        unknown_types.add(bet_type)
                    continue

                for out in market.get("Outcomes") or []:
                    odd = out.get("Odd")
                    # Une cote nulle ou négative signale un marché fermé —
                    # Gaming1 utilise -1 pour ça.
                    if not isinstance(odd, (int, float)) or odd <= 1.0:
                        continue

                    if market_type is MarketType.H2H:
                        label = _h2h_label(out.get("Name", ""), home, away)
                        line = None
                    else:
                        label = _totals_label(out.get("Name", ""))
                        # La ligne vit dans Base, jamais dans le libellé : les
                        # issues s'appellent seulement « Plus de » / « Moins de ».
                        base = out.get("Base", market.get("Base"))
                        if base is None:
                            continue
                        line = float(base)
                    if label is None:
                        continue

                    yield OddQuote(
                        event_key=ek,
                        book=Book.CIRCUS_BE,
                        market=market_type,
                        outcome=Outcome(label=label, line=line),
                        decimal_odd=float(odd),
                        fetched_at=now,
                        source_event_id=str(event.get("EventId") or ""),
                        league=league_name,
                    )


def parse_push(payload: dict | list, **kwargs) -> list[OddQuote]:
    """Point d'entrée pour le fichier poussé par le navigateur.

    Le userscript interroge plusieurs jours d'affilée, donc le fichier contient
    une liste de réponses. Un dict isolé est accepté aussi, pour rester tolérant
    au format d'un push manuel."""
    blocks: Iterable[dict]
    if isinstance(payload, dict):
        blocks = payload.get("blocks") or [payload]
    else:
        blocks = payload

    seen: set[tuple] = set()
    out: list[OddQuote] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for q in parse_prematch(block, **kwargs):
            # Les jours consécutifs se recouvrent : un même match peut revenir
            # dans deux réponses.
            key = (q.event_key, q.market, q.outcome.label, q.outcome.line)
            if key in seen:
                continue
            seen.add(key)
            out.append(q)
    return out


def load_pushed_quotes(path: str, max_age_minutes: float = 30.0,
                       *, print_fn=print,
                       unknown_types: set[str] | None = None,
                       expect_sport_id: int | None = None) -> list[OddQuote]:
    """Lit le dump sur disque, en refusant ce qui est périmé.

    Sans cette garde, un onglet fermé donne des cotes mortes traitées comme
    fraîches — en silence. C'est la leçon de Betano, transposée ici.

    Un fichier par sport : le daemon boucle sport par sport, et mélanger les
    deux ferait porter les cotes tennis au scan football, où elles ne
    correspondent à rien."""
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return []
    age_min = (datetime.now(timezone.utc).timestamp() - p.stat().st_mtime) / 60.0
    if age_min > max_age_minutes:
        print_fn(
            f"Circus ignoré : dump vieux de {age_min:.0f} min "
            f"(limite {max_age_minutes:.0f}) — l'onglet est-il encore ouvert ?"
        )
        return []
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        print_fn(f"Circus : dump illisible ({e})")
        return []
    foreign: list[int] = []
    quotes = parse_push(payload, unknown_types=unknown_types,
                        expect_sport_id=expect_sport_id, foreign=foreign)
    if foreign:
        print_fn(
            f"Circus : {p.name} contient des ligues d'un autre sport "
            f"(SportId {sorted(set(foreign))}) — écartées. Le pont a mal "
            f"routé une réponse, mets à jour tools/circus-ingest.user.js."
        )
    return quotes
