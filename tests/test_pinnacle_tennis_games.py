"""Les totaux de JEUX du tennis, et les deux échelles qui se croisent dessus.

Pourquoi ce fichier existe
--------------------------
L'export de détections ne contenait pas UNE SEULE ligne de totaux au tennis —
5019 h2h, zéro over/under — alors que six books en affichent, et que le tennis
est le meilleur sport du système (CLV premium +15 %, 92 % positive). Sondé le
19/08 contre l'API : Pinnacle price bien 320 marchés de totaux en jeux, mais
tous portent le matchupId d'un matchup ENFANT, et l'index ne retenait que les
racines. Ils mouraient donc sur `matchup is None: continue`, sans un mot dans
les logs. Pinnacle affichait 2,5 (des SETS) en face des 17,5-23,0 des books, et
aucune ligne ne s'appariait jamais.

Le piège que le correctif ne doit pas ouvrir
--------------------------------------------
La racine et son enfant « Games » décrivent le même match : même `event_key`.
Mais `spread ±1,5` existe des deux côtés — handicap d'un SET sur la racine,
handicap d'un JEU sur l'enfant — sur 20 des 72 matchs relevés. Les laisser se
rejoindre ferait deviguer 1,5 jeu contre 1,5 set : le groupe aurait ses deux
côtés, la marge serait plausible, la ligne juste serait fausse, et rien ne
lèverait d'erreur. C'est le §19.2 dans sa version silencieuse — la raison pour
laquelle ces tests parlent d'échelles plutôt que de comptages.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from unittest.mock import patch

import pytest

from src.models import MarketType
from src.scrapers.pinnacle import PinnacleScraper, matchups_cache_clear


ROOT_ID = 1634269141
GAMES_ID = 1634278449
SETS_ID = 1634278999
# ⚠️ Date RELATIVE, et pas une date en dur. Depuis que `fetch_market_quotes`
# écarte les matchs dont le coup d'envoi est passé, une fixture figée dans le
# passé ferait tomber ces tests — qui ne portent pourtant pas sur le coup
# d'envoi. Le calcul les rend insensibles au calendrier.
START = (datetime.now(timezone.utc) + timedelta(hours=6)) \
    .isoformat().replace("+00:00", "Z")

_PARENT_BLOCK = {
    "id": ROOT_ID,
    "hasMarkets": True,
    # ⚠️ Le bloc `parent` embarqué dans un enfant annonce TOUJOURS isLive=False,
    # y compris pour un match commencé depuis huit heures (81/81 au relevé).
    "isLive": False,
    "startTime": START,
    "participants": [
        {"alignment": "home", "name": "Katarina Zavatska", "order": 0},
        {"alignment": "away", "name": "Giorgia Pedone", "order": 1},
    ],
}

_ROOT = {
    "id": ROOT_ID,
    "isLive": False,
    "startTime": START,
    "units": None,
    "participants": [
        {"alignment": "home", "name": "Katarina Zavatska", "order": 0},
        {"alignment": "away", "name": "Giorgia Pedone", "order": 1},
    ],
    "league": {"name": "ITF Women Kursumlijska Banja - R16"},
}

_GAMES_CHILD = {
    "id": GAMES_ID,
    "parentId": ROOT_ID,
    "parent": _PARENT_BLOCK,
    "units": "Games",
    "isLive": False,
    "startTime": START,
    # Les participants de l'ENFANT portent un suffixe : les lire ici
    # fabriquerait une `event_key` sans aucun h2h en face.
    "participants": [
        {"alignment": "home", "name": "Katarina Zavatska (Games)", "order": 0},
        {"alignment": "away", "name": "Giorgia Pedone (Games)", "order": 1},
    ],
    "league": {"name": "ITF Women Kursumlijska Banja - R16"},
}

_SETS_CHILD = {
    "id": SETS_ID,
    "parentId": ROOT_ID,
    "parent": _PARENT_BLOCK,
    "units": "Sets",
    "isLive": False,
    "startTime": START,
    "participants": [
        {"alignment": "home", "name": "Katarina Zavatska (Sets)", "order": 0},
        {"alignment": "away", "name": "Giorgia Pedone (Sets)", "order": 1},
    ],
    "league": {"name": "ITF Women Kursumlijska Banja - R16"},
}


def _market(matchup_id: int, type_: str, prices: list[dict], period: int = 0) -> dict:
    return {"matchupId": matchup_id, "period": period, "status": "open",
            "type": type_, "prices": prices}


_ROOT_MONEYLINE = _market(ROOT_ID, "moneyline", [
    {"designation": "home", "price": -140},
    {"designation": "away", "price": +120},
])
# Total de SETS sur la racine : c'est le 2,5 qu'on voyait seul en base.
_ROOT_SETS_TOTAL = _market(ROOT_ID, "total", [
    {"designation": "over", "points": 2.5, "price": +105},
    {"designation": "under", "points": 2.5, "price": -125},
])
_ROOT_SETS_SPREAD = _market(ROOT_ID, "spread", [
    {"designation": "home", "points": -1.5, "price": +150},
    {"designation": "away", "points": 1.5, "price": -180},
])
# Total de JEUX sur l'enfant : ce que les six books affichent réellement.
_GAMES_TOTAL = _market(GAMES_ID, "total", [
    {"designation": "over", "points": 17.5, "price": -114},
    {"designation": "under", "points": 17.5, "price": -106},
])
_GAMES_SPREAD = _market(GAMES_ID, "spread", [
    {"designation": "home", "points": -1.5, "price": -110},
    {"designation": "away", "points": 1.5, "price": -110},
])
_GAMES_TEAM_TOTAL = _market(GAMES_ID, "team_total", [
    {"designation": "over", "points": 8.5, "price": -120},
])
_SETS_CHILD_TOTAL = _market(SETS_ID, "total", [
    {"designation": "over", "points": 2.5, "price": +101},
    {"designation": "under", "points": 2.5, "price": -121},
])


def _run(matchups: list[dict], markets: list[dict], sport: str = "tennis") -> list:
    def fake_get(self, path, params=None):
        if path.endswith("/matchups"):
            return list(matchups)
        if "markets/straight" in path:
            return list(markets)
        return []

    matchups_cache_clear()
    try:
        with patch.object(PinnacleScraper, "_get", fake_get):
            with PinnacleScraper(request_delay=0) as sc:
                return list(sc.fetch_market_quotes(sport))
    finally:
        matchups_cache_clear()


def _lines(quotes, market: MarketType) -> set:
    return {q.outcome.line for q in quotes if q.market == market}


# ------------------------------------------------------- ce qui manquait ---

def test_game_totals_reach_the_quotes():
    """Le manque exact : zéro détection de totaux au tennis depuis le début."""
    quotes = _run([_ROOT, _GAMES_CHILD], [_ROOT_MONEYLINE, _GAMES_TOTAL])
    totals = [q for q in quotes if q.market == MarketType.TOTALS]
    assert {q.outcome.line for q in totals} == {17.5}
    assert {q.outcome.label for q in totals} == {"over", "under"}


def test_game_totals_land_on_the_same_event_as_the_h2h():
    """LA propriété qui rend le correctif utile.

    Un total de jeux sous une `event_key` à lui ne servirait à rien : il
    n'aurait ni h2h en face, ni cote de book rapprochée, et le réalignement des
    softs sur la référence ne le trouverait jamais. Les noms doivent donc venir
    du bloc `parent` — ceux de l'enfant sont suffixés « (Games) »."""
    quotes = _run([_ROOT, _GAMES_CHILD], [_ROOT_MONEYLINE, _GAMES_TOTAL])
    assert len({q.event_key for q in quotes}) == 1
    assert all("games" not in q.event_key for q in quotes)


def test_the_two_scales_stay_on_separate_lines():
    """2,5 sets et 17,5 jeux vivent sous la même clé et le même marché. C'est
    la LIGNE qui les sépare, et c'est elle qui doit rester distincte."""
    quotes = _run([_ROOT, _GAMES_CHILD],
                  [_ROOT_MONEYLINE, _ROOT_SETS_TOTAL, _GAMES_TOTAL])
    assert _lines(quotes, MarketType.TOTALS) == {2.5, 17.5}


def test_the_games_handicap_is_refused():
    """Le piège de l'en-tête : `spread ±1,5` en JEUX contre `spread ±1,5` en
    SETS, même événement, même marché, même ligne. Les deux se retrouveraient
    dans le même groupe de devig — deux côtés, marge plausible, ligne juste
    fausse, aucune erreur levée. Tant qu'une ligne ne porte pas son unité, le
    handicap en jeux reste dehors."""
    quotes = _run([_ROOT, _GAMES_CHILD],
                  [_ROOT_SETS_SPREAD, _GAMES_SPREAD, _GAMES_TOTAL])
    handicaps = [q for q in quotes if q.market == MarketType.HANDICAP]
    assert len(handicaps) == 2, "seul le handicap de SETS de la racine survit"
    assert {q.source_event_id for q in handicaps} == {str(ROOT_ID)}
    # …et le total de jeux, lui, est bien passé : le refus vise le handicap,
    # pas la famille entière.
    assert _lines(quotes, MarketType.TOTALS) == {17.5}


def test_a_sets_child_does_not_duplicate_the_root():
    """Les enfants « Sets » redisent la racine. Les garder écrirait deux prix
    pour la même (clé, marché, ligne) : le dernier écrasant le premier, sans
    qu'on sache lequel a gagné."""
    quotes = _run([_ROOT, _SETS_CHILD], [_ROOT_SETS_TOTAL, _SETS_CHILD_TOTAL])
    totals = [q for q in quotes if q.market == MarketType.TOTALS]
    assert len(totals) == 2                      # over + under, une seule fois
    assert {q.source_event_id for q in totals} == {str(ROOT_ID)}


def test_team_totals_stay_out():
    """`team_total` n'est pas cartographié, et doit le rester : c'est le total
    d'un seul joueur, que `settle()` ne sait pas noter."""
    quotes = _run([_ROOT, _GAMES_CHILD], [_GAMES_TEAM_TOTAL, _GAMES_TOTAL])
    assert _lines(quotes, MarketType.TOTALS) == {17.5}


# ------------------------------------------------------------ le direct ---

def test_a_live_games_child_is_skipped():
    """C'est `isLive` de l'ENFANT qui dit la vérité : le bloc `parent` embarqué
    annonce False même huit heures après le coup d'envoi (81/81 au relevé). Se
    fier au parent ferait entrer des prix en direct dans la référence
    prématch."""
    live = dict(_GAMES_CHILD, isLive=True)
    quotes = _run([_ROOT, live], [_ROOT_MONEYLINE, _GAMES_TOTAL])
    assert _lines(quotes, MarketType.TOTALS) == set()
    assert [q.market for q in quotes] == [MarketType.H2H] * 2


def test_a_games_child_whose_root_left_the_calendar_still_works():
    """20 enfants sur 81 n'avaient plus leur racine dans le calendrier. Le bloc
    `parent` embarqué porte tout ce qu'il faut — le résoudre via l'index aurait
    perdu ces matchs."""
    quotes = _run([_GAMES_CHILD], [_GAMES_TOTAL])
    assert _lines(quotes, MarketType.TOTALS) == {17.5}
    assert {q.source_event_id for q in quotes} == {str(ROOT_ID)}


# --------------------------------------------------------- les échelles ---

def test_a_sets_line_on_a_games_matchup_is_refused():
    """Le garde-fou. Rien ne l'exige aujourd'hui — la famille classe déjà — mais
    si Pinnacle glissait un 2,5 sur un matchup « Games », il rejoindrait le
    total de sets de la racine et le devig comparerait des jeux à des sets. On
    préfère perdre la ligne que la mal ranger."""
    egare = _market(GAMES_ID, "total", [
        {"designation": "over", "points": 2.5, "price": -110},
        {"designation": "under", "points": 2.5, "price": -110},
    ])
    quotes = _run([_ROOT, _GAMES_CHILD], [egare])
    assert quotes == []


@pytest.mark.parametrize("sport, line", [("basketball", 220.5), ("soccer", 2.5)])
def test_other_sports_keep_their_own_scale(sport, line):
    """Le seuil des jeux ne vaut QUE pour un matchup « Games ». Rendu symétrique
    — « au-dessus de 10, c'est des jeux » — il jetterait la totalité des totaux
    de basket, qui tournent autour de 220,5."""
    racine = dict(_ROOT, league={"name": "Test"})
    total = _market(ROOT_ID, "total", [
        {"designation": "over", "points": line, "price": -110},
        {"designation": "under", "points": line, "price": -110},
    ])
    quotes = _run([racine], [total], sport=sport)
    assert _lines(quotes, MarketType.TOTALS) == {line}
