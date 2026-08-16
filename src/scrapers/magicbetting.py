"""MagicBetting (plateforme Digitain) — parseur des réponses déchiffrées.

Le §15.6 avait classé ce book inexploitable, ses réponses étant chiffrées.
`digitain_crypto` lève ce blocage : le clair est un tableau d'événements JSON.

⚠️ Digitain n'est PAS Gaming1 (le §6 se trompait) : ce n'est donc pas un jumeau
de Circus, et c'est tout son intérêt — une source de prix réellement
indépendante des quatre Kambi et des deux Altenar du portefeuille.

Structure d'un événement
------------------------
    {"Id":…, "HT":"Orlando City SC", "AT":"FC Cincinnati",
     "D":"2026-08-15T23:30:00Z", "SId":1, "CN":"USA. MLS",
     "StakeTypes":[ {"Id":1, "N":"…", "Stakes":[
         {"N":"Orlando City SC", "F":2.12, "A":null} ]} ]}

`F` porte la cote décimale, `A` la ligne d'un total. Les marchés se
reconnaissent à leur `Id` — JAMAIS à leur nom, qui dépend de la langue
demandée dans l'URL (`langId=62` rend du néerlandais). C'est la règle du §10 :
égalité exacte, jamais de correspondance par ressemblance.

Marchés retenus
---------------
      1  résultat du match          -> H2H
      3  total de buts, toutes lignes -> TOTALS

Les identifiants NÉGATIFS (-2, -3, -2532, -2533) sont les lignes « vedettes »
du site : le même marché réduit à une seule ligne, en doublon de la version
complète. Les prendre écrirait deux fois la même cote.

Exclus volontairement, faute de contrepartie chez Pinnacle : handicaps (2,
2532), double chance (37), totaux asiatiques (2533) — même politique que
Circus au §10.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterator

from ..matcher import event_key
from ..models import Book, MarketType, OddQuote, Outcome
from ..teams import record_pair

log = logging.getLogger(__name__)

# Sport Digitain -> le nôtre. Confirmés sur capture : 1 = football,
# 3 = tennis (SId 3, « ATP Cincinnati », Nakashima — Kovacevic).
SPORT_IDS: dict[int, str] = {1: "soccer", 3: "tennis"}

# Id de marché -> notre type, PAR SPORT. Égalité exacte uniquement.
#
# ⚠️ Le mapping ne peut pas être global : le football écrit son 1X2 sous l'Id 1
# et le tennis son vainqueur sous l'Id 702. C'est la règle déjà posée sur
# Circus — « un code déjà vu en football rendrait muet le même code en
# tennis ». Un dictionnaire commun ferait exactement ça, sans rien signaler.
#
# Tennis, relevé sur la capture du 16/08 :
#   702  49 événements, 98 cotes -> exactement 2 issues, toutes des noms de
#        joueur, aucune ligne. C'est le vainqueur du match.
#     3  43 événements, toutes les cotes portent une ligne, étendue 16,5…28.
#        Ce sont des JEUX : un total de sets vaudrait 2,5, et l'Id 545 « Total
#        des sets » existe séparément. Les deux s'affichent « Au-dessus /
#        Moins » — seule la valeur les sépare, et se tromper ne lèverait aucune
#        erreur, juste des cotes comparées à la mauvaise référence Pinnacle.
#
# NE PAS ajouter les « vainqueur » à 38 issues (401499, 401522, 401523) : ce
# sont des vainqueurs de TOURNOI, pas de match. Rien chez Pinnacle en face.
MARKET_BY_STAKE_TYPE: dict[str, dict[int, MarketType]] = {
    "soccer": {
        1: MarketType.H2H,
        3: MarketType.TOTALS,
    },
    "tennis": {
        702: MarketType.H2H,
        3: MarketType.TOTALS,
    },
}

# Libellés over/under selon la langue demandée. Le userscript fixe `langId`,
# donc l'ensemble est déterministe — mais on couvre les langues plausibles
# plutôt que de dépendre de l'ordre des issues, qui n'est garanti nulle part.
_OVER = {"over", "boven", "plus de", "au-dessus", "más de", "mehr als"}
_UNDER = {"onder", "under", "moins de", "moins", "menos de", "weniger als"}

_warned: set[tuple] = set()


def _warn_once(key: tuple, msg: str) -> None:
    """Signaler un code inconnu UNE fois. Un marché jeté en silence est la
    panne dominante de ce projet (§11) ; le répéter à chaque cycle rendrait le
    journal illisible, ce qui revient au même."""
    if key not in _warned:
        _warned.add(key)
        log.warning(msg)


def _parse_start(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _label(stake: dict, market: MarketType, home: str, away: str,
           n_outcomes: int = 3) -> str | None:
    """Étiquette normalisée d'une issue, ou None si elle n'est pas reconnue."""
    name = str(stake.get("N") or "").strip()
    if not name:
        return None
    if market is MarketType.H2H:
        # Comparer aux noms d'équipe de l'événement lui-même : c'est la seule
        # méthode indépendante de la langue.
        if name == home:
            return "home"
        if name == away:
            return "away"
        # ⚠️ « Ni l'un ni l'autre » ne veut dire « nul » que sur un marché à
        # TROIS issues. Sur un marché à deux — le tennis, le basket — il n'y a
        # pas de nul : un nom non reconnu est une orthographe qui diffère, et
        # l'étiqueter « draw » fabriquerait une issue qui n'existe pas, puis
        # une ligne juste à trois termes dont la somme serait fausse. Le devig
        # s'en trouverait faussé sans qu'aucune erreur n'apparaisse.
        if n_outcomes != 3:
            _warn_once(("h2h2", name), f"MagicBetting : issue {name!r} non "
                                       f"reconnue sur un marché à {n_outcomes} issues")
            return None
        return "draw"
    if market is MarketType.TOTALS:
        low = name.casefold()
        if low in _OVER:
            return "over"
        if low in _UNDER:
            return "under"
        _warn_once(("totals", low), f"MagicBetting : issue de total inconnue {name!r}")
        return None
    return None


def parse_events(payload: Any, *, expect_sport: str | None = None,
                 foreign: list | None = None) -> Iterator[OddQuote]:
    """Rendre les OddQuote d'une réponse déchiffrée.

    `expect_sport` écarte les événements d'un autre sport. Le pont pousse un
    fichier par sport, mais rien DANS le fichier ne garantit qu'il est arrivé
    au bon endroit — c'est déjà arrivé sur Circus, où le tennis a été écrit
    dans `soccer.json`. Chaque événement porte son `SId` : on s'en sert plutôt
    que de faire confiance au nom du fichier. Les intrus vont dans `foreign`
    pour être signalés, jamais jetés en silence (§11)."""
    if not isinstance(payload, list):
        return
    now = datetime.now(timezone.utc)

    for ev in payload:
        if not isinstance(ev, dict):
            continue
        sport = SPORT_IDS.get(ev.get("SId"))
        if sport is None:
            _warn_once(("sport", ev.get("SId")),
                       f"MagicBetting : sport {ev.get('SId')} non pris en charge")
            continue
        if expect_sport is not None and sport != expect_sport:
            if foreign is not None:
                foreign.append(ev.get("SId"))
            continue
        markets = MARKET_BY_STAKE_TYPE.get(sport) or {}
        home = str(ev.get("HT") or "").strip()
        away = str(ev.get("AT") or "").strip()
        start = _parse_start(ev.get("D"))
        if not (home and away and start):
            continue
        # Un match commencé n'a plus de ligne de référence prématch : le
        # scraper Pinnacle ignore les matchs en cours, donc le comparer
        # fabriquerait un value bet contre une référence morte (§9).
        if ev.get("HS") is not None or ev.get("AS") is not None:
            continue

        ek = event_key(home, away, start)
        source_id = str(ev.get("Id") or "")
        produced: list[OddQuote] = []

        for st in ev.get("StakeTypes") or []:
            if not isinstance(st, dict):
                continue
            st_id = st.get("Id")
            market = markets.get(st_id)
            if market is None:
                # Les Id négatifs sont des doublons connus ; ne pas les
                # signaler, sinon le journal se remplit de bruit attendu.
                if isinstance(st_id, int) and st_id > 0:
                    _warn_once(("mkt", sport, st_id),
                               f"MagicBetting {sport} : marché {st_id} "
                               f"({st.get('N')!r}) non mappé")
                continue

            stakes = st.get("Stakes") or []
            # Nombre d'issues DISTINCTES du marché : sur un total, la même
            # paire over/under se répète pour chaque ligne, donc compter les
            # entrées surestimerait.
            n_out = len({str(x.get("N") or "") for x in stakes
                         if isinstance(x, dict)}) if market is MarketType.H2H else 3
            for stake in stakes:
                if not isinstance(stake, dict):
                    continue
                try:
                    odd = float(stake.get("F"))
                except (TypeError, ValueError):
                    continue
                if odd <= 1.0:
                    continue
                label = _label(stake, market, home, away, n_out)
                if label is None:
                    continue
                line = None
                if market is MarketType.TOTALS:
                    try:
                        line = float(stake.get("A"))
                    except (TypeError, ValueError):
                        # Un total sans ligne est inexploitable : on ne saurait
                        # pas contre quel seuil de Pinnacle le comparer.
                        continue
                produced.append(OddQuote(
                    event_key=ek,
                    book=Book.MAGICBETTING,
                    market=market,
                    outcome=Outcome(label=label, line=line),
                    decimal_odd=odd,
                    fetched_at=now,
                    source_event_id=source_id,
                ))

        # ⚠️ N'enregistrer la paire d'équipes QUE si l'événement a produit
        # quelque chose. Les réponses contiennent aussi des pseudo-événements
        # de vainqueur de tournoi (Id 401499, 38 issues) : ils portent des noms
        # dans `HT`/`AT` mais aucun marché exploitable, et les inscrire
        # polluerait la table des équipes avec des libellés qui ne
        # correspondent à aucun match.
        if produced:
            record_pair(home, away)
            yield from produced


def load_pushed_quotes(path: str, max_age_minutes: float = 10.0,
                       *, print_fn=print,
                       expect_sport: str | None = None) -> list[OddQuote]:
    """Lit le dump DÉCHIFFRÉ déposé par le serveur d'ingestion.

    ⚠️ Garde de fraîcheur, la même que Betano et Circus : un onglet fermé
    laisse un fichier intact, dont les cotes deviennent silencieusement
    mortes. Sans cette borne d'âge, le daemon les traiterait comme fraîches et
    fabriquerait des value bets contre des prix qui n'existent plus (§5).

    Dix minutes seulement, contre trente pour Circus : le userscript pousse
    toutes les minutes, donc dix cycles manqués signalent déjà un problème."""
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return []
    age_min = (datetime.now(timezone.utc).timestamp() - p.stat().st_mtime) / 60.0
    if age_min > max_age_minutes:
        print_fn(
            f"MagicBetting ignoré : dump vieux de {age_min:.0f} min "
            f"(limite {max_age_minutes:.0f}) — l'onglet est-il encore ouvert ?"
        )
        return []
    try:
        import json as _json
        data = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print_fn(f"MagicBetting illisible : {e}")
        return []
    foreign: list = []
    quotes = list(parse_events(data, expect_sport=expect_sport, foreign=foreign))
    if foreign:
        print_fn(
            f"MagicBetting {expect_sport} : {len(foreign)} événements d'un "
            f"autre sport écartés (SId {sorted(set(foreign))}) — le pont "
            f"pousse-t-il au bon endroit ?"
        )
    return quotes
