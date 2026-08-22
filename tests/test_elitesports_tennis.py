"""EliteSports au TENNIS — les identifiants ne sont pas ceux du football.

⚠️ CE FICHIER EXISTE À CAUSE D'UNE PANNE RÉELLE. La première version du
scraper avait relevé `marketExternalId` et `betTypeName` sur des échantillons
de FOOTBALL uniquement, puis les avait appliqués au tennis. Résultat en
production : le football rendait 31 600 cotes par cycle et **le tennis ZÉRO**,
sans la moindre erreur — le log n'imprime que les books qui produisent, donc
l'absence ne se voyait nulle part.

C'est très exactement ce que le §10 interdit : *un Id de marché se confirme par
égalité exacte, jamais par ressemblance ; une supposition qui tombe à côté ne
lève aucune erreur, elle rend simplement un book muet.*

Deux écarts, un seul mot chacun :

  football  marketExternalId=1  « Résultat du match »  betTypeName « Home wins »
  tennis    marketExternalId=5  « Vainqueur »          betTypeName « Home team wins »
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from src.models import Book, MarketType
from src.scrapers.elitesports import BET_TYPES, MARKET_IDS, parse_prematch

ECHANTILLON = (Path(__file__).parent / "fixtures" /
               "elitesports_tennis_prematch_sample.json")


@pytest.fixture()
def tennis():
    return json.loads(ECHANTILLON.read_text(encoding="utf-8"))


def test_le_tennis_produit_des_cotes(tennis):
    """LA non-régression. Zéro ici veut dire book muet en production."""
    qs = list(parse_prematch(tennis))
    assert qs, "le tennis ne produit RIEN — les identifiants ont divergé"
    assert len(qs) == 20                       # 10 matchs × 2 issues
    assert {q.book for q in qs} == {Book.ELITESPORTS}


def test_le_vainqueur_est_un_h2h_a_deux_issues(tennis):
    """Pas de nul au tennis : `home` et `away`, jamais `draw`."""
    qs = list(parse_prematch(tennis))
    assert Counter(q.outcome.label for q in qs) == {"home": 10, "away": 10}
    assert all(q.market is MarketType.H2H for q in qs)
    assert all(q.outcome.line is None for q in qs)


def test_les_deux_formes_de_libelle_sont_connues():
    """« Home wins » (football) ET « Home team wins » (tennis). Un mot d'écart,
    et c'est la moitié d'un marché qui disparaît."""
    assert BET_TYPES["Home wins"] == "home"
    assert BET_TYPES["Home team wins"] == "home"
    assert BET_TYPES["Away team wins"] == "away"


def test_les_deux_marches_de_vainqueur_sont_connus():
    """1 au football, 5 au tennis — relevés, jamais déduits l'un de l'autre."""
    assert MARKET_IDS[1] is MarketType.H2H
    assert MARKET_IDS[5] is MarketType.H2H
    assert MARKET_IDS[7] is MarketType.TOTALS


def test_le_total_du_tennis_est_un_placeholder_vide(tennis):
    """Le marché 7 existe au tennis mais il est VIDE : `marketId` à zéro,
    `locked: true`, `lines: []`. Rien à extraire — et donc aucun risque de
    confondre jeux et sets, la confusion du §19.2 qui rendrait « under »
    gagnant partout."""
    marches = [m for lg in tennis["content"] for ev in lg["events"]
               for m in ev.get("markets") or [] if m.get("marketExternalId") == 7]
    assert marches, "l'échantillon ne contient plus de marché 7 au tennis"
    for m in marches:
        assert m.get("locked") is True
        assert all(not (p.get("lines") or []) for p in m.get("periods") or [])
    # Et rien n'en sort.
    assert not [q for q in parse_prematch(tennis) if q.market is MarketType.TOTALS]


def test_les_noms_nom_prenom_sont_pris_tels_quels(tennis):
    """EliteSports écrit « Granollers, Marcel/Zeballos, Horacio ». Le matcher a
    `swap_surname_first` pour ça (§15.4) : le scraper ne doit RIEN découper,
    sinon il casserait ce que la normalisation sait déjà faire."""
    from src.scrapers.elitesports import _team_names
    ev = [e for lg in tennis["content"] for e in lg["events"]][0]
    noms = _team_names(ev)
    assert noms is not None
    assert "," in noms[0] or "/" in noms[0], "nom de joueur inattendu"
    assert " - " not in noms[0], "le scraper a découpé sur le tiret"


def test_la_ligue_de_tennis_est_portee(tennis):
    qs = list(parse_prematch(tennis))
    assert all(q.league for q in qs)
    assert any("ATP" in (q.league or "") for q in qs)
