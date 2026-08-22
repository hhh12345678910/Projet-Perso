"""EliteSports.be — parseur testé contre un échantillon RÉEL.

L'échantillon vient d'un HAR capturé le 22/08 sur le site, réduit à trois
ligues sans reformatage. C'est la règle du projet : le parsing est ce qui peut
se tromper, donc c'est lui qu'on teste contre ce que la source répond
vraiment, jamais contre une forme inventée.

Trois pièges sont gardés ici, et chacun serait SILENCIEUX :

1. les totaux asiatiques (lignes en quart) réglés comme des totaux simples ;
2. une mi-temps prise pour un marché de match entier ;
3. une cote verrouillée publiée comme jouable.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from src.models import Book, MarketType
from src.scrapers.elitesports import (
    SPORT_IDS, _is_quarter_line, _team_names, parse_prematch,
)

ECHANTILLON = Path(__file__).parent / "fixtures" / "elitesports_prematch_sample.json"


@pytest.fixture()
def payload():
    return json.loads(ECHANTILLON.read_text(encoding="utf-8"))


def test_l_echantillon_reel_produit_des_cotes(payload):
    qs = list(parse_prematch(payload))
    assert len(qs) == 226
    par_marche = Counter(q.market for q in qs)
    # 10 événements × 3 issues : le 1X2 est complet partout.
    assert par_marche[MarketType.H2H] == 30
    assert par_marche[MarketType.TOTALS] == 196
    assert {q.book for q in qs} == {Book.ELITESPORTS}


def test_les_trois_issues_du_1x2_sont_traduites(payload):
    """Traduction par égalité exacte : « Home wins » / « Draw » / « Away team
    wins » arrivent en ANGLAIS alors que `marketName` est en français, donc
    ils ne suivent pas `x-locale` et pourraient changer sans prévenir."""
    h2h = [q for q in parse_prematch(payload) if q.market is MarketType.H2H]
    assert Counter(q.outcome.label for q in h2h) == {"home": 10, "draw": 10, "away": 10}
    assert all(q.outcome.line is None for q in h2h), "un 1X2 n'a pas de ligne"


def test_les_lignes_en_quart_sont_ecartees(payload):
    """⚠️ LE piège. EliteSports sert l'échelle complète par pas de 0,25, et
    « over 2.25 » est un pari FRACTIONNÉ — moitié sur 2,0, moitié sur 2,5.
    `settle()` le réglerait comme un total simple et noterait « lost » là où
    la réalité est un demi-remboursement, sans lever la moindre erreur."""
    lignes = {q.outcome.line for q in parse_prematch(payload)
              if q.market is MarketType.TOTALS}
    assert lignes, "aucun total extrait — l'échantillon a changé de forme ?"
    for ligne in lignes:
        assert not _is_quarter_line(ligne), f"ligne en quart passée : {ligne}"
    # Et la source EN CONTIENT bien : le test ne passe pas faute de matière.
    brutes = {
        ln.get("coefficientValue")
        for lg in payload["content"] for ev in lg["events"]
        for m in ev.get("markets") or [] if m.get("marketExternalId") == 7
        for p in m.get("periods") or [] for ln in p.get("lines") or []
    }
    assert any(_is_quarter_line(v) for v in brutes if v is not None), \
        "l'échantillon ne contient aucune ligne en quart — le filtre n'est pas prouvé"


def test_le_filtre_de_quart_est_juste():
    for entiere_ou_demie in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 16.5, 28.0):
        assert not _is_quarter_line(entiere_ou_demie)
    for quart in (0.25, 0.75, 1.25, 2.25, 5.75):
        assert _is_quarter_line(quart)
    assert not _is_quarter_line(None)


def test_seul_le_temps_reglementaire_est_pris(payload):
    """`periodIdentifier == 0`. Une mi-temps entrerait dans la MÊME clé
    (event, marché, ligne) qu'un marché de match entier et serait comparée à
    la mauvaise ligne juste — la famille de bug du §21.14."""
    payload["content"][0]["events"][0]["markets"][0]["periods"].append({
        "periodName": "1st half", "periodIdentifier": 1, "locked": False,
        "lines": [{"odds": [{"betTypeName": "Home wins", "oddsValue": 9.99,
                             "locked": False}]}],
    })
    assert not any(q.decimal_odd == 9.99 for q in parse_prematch(payload))


def test_une_cote_verrouillee_n_est_pas_publiee(payload):
    """Trois niveaux de `locked` existent — marché, période, cote — et une
    cote verrouillée n'est pas jouable. La publier fabriquerait des détections
    sur lesquelles on ne peut pas cliquer."""
    avant = len(list(parse_prematch(payload)))

    m = payload["content"][0]["events"][0]["markets"][0]
    m["periods"][0]["lines"][0]["odds"][0]["locked"] = True
    assert len(list(parse_prematch(payload))) == avant - 1

    m["locked"] = True                      # tout le marché
    apres = len(list(parse_prematch(payload)))
    assert apres < avant - 1


def test_un_libelle_inconnu_est_ignore_pas_devine(payload):
    """Le §10 : un identifiant se confirme par égalité exacte. Deviner rendrait
    un book muet ou, pire, une issue rangée du mauvais côté."""
    payload["content"][0]["events"][0]["markets"][0]["periods"][0]["lines"][0][
        "odds"].append({"betTypeName": "Both teams score", "oddsValue": 1.77,
                        "locked": False})
    assert not any(q.decimal_odd == 1.77 for q in parse_prematch(payload))


def test_les_noms_viennent_de_teams_pas_du_tiret():
    """`eventName` vaut « Fluminense RJ - Remo PA » : le séparateur est un
    tiret, et des noms d'équipes en contiennent. `teams[]` porte les noms
    séparés."""
    ev = {"teams": [{"teamName": "Saint-Étienne"}, {"teamName": "Paris-SG"}],
          "eventName": "Saint-Étienne - Paris-SG"}
    assert _team_names(ev) == ("Saint-Étienne", "Paris-SG")
    # Repli sur teamNames si `teams` manque.
    assert _team_names({"teamNames": ["A", "B"]}) == ("A", "B")
    assert _team_names({"teamNames": ["A"]}) is None
    assert _team_names({}) is None


def test_un_evenement_live_n_est_pas_pris(payload):
    """Le prématch et le live ont des routes séparées ; mélanger les deux
    ferait valoriser une cote en cours de match contre une ligne juste
    prématch — le motif du §config, `scan_live_value_bets`."""
    payload["content"][0]["events"][0]["status"] = "LIVE"
    qs = list(parse_prematch(payload))
    assert len(qs) < 226


def test_la_ligue_est_portee(payload):
    """Sans elle, aucune analyse par championnat n'est possible — et le §21.16
    a montré que la ligue porte aussi la CLASSE (féminin, jeunes)."""
    qs = list(parse_prematch(payload))
    assert all(q.league for q in qs)
    assert "Brésil. Serie A" in {q.league for q in qs}


def test_les_sports_sont_ceux_releves():
    """UUID relevés dans `/public/sports`, jamais devinés."""
    assert set(SPORT_IDS) == {"soccer", "tennis"}
    assert all(len(v) == 36 for v in SPORT_IDS.values())


def test_un_payload_vide_ne_leve_rien():
    assert list(parse_prematch({})) == []
    assert list(parse_prematch({"content": []})) == []
    assert list(parse_prematch({"content": [{"events": []}]})) == []
