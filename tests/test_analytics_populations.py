"""Les six populations — et l'honnêteté sur ce qui existe vraiment.

    DETECTED → ELIGIBLE_FOR_ALERT → SENT → CLICKED → BET → SETTLED

Trois de ces étages ne sont PAS stockés, et une analyse qui l'ignore compare
deux populations en croyant mesurer un choix. Ces tests fixent ce que chaque
étage contient, ce qu'il ne contient pas, et ce qu'il annonce de ses limites.
"""
from __future__ import annotations

import pytest

from src.analytics import Filtres, Population, analyser
from src.analytics.populations import (EXPLICATION, HORS_SQL, LIMITES,
                                       alias_de)
from tests.analytics_base import Opp, ajouter_canal, monter


def _res(chemin, pop, **kw):
    return analyser(str(chemin), Filtres(population=pop, **kw))


def _n(chemin, pop, **kw) -> int:
    return _res(chemin, pop, **kw)["summary"]["opportunities"]


#: Un jeu couvrant tous les étages d'un coup.
def _jeu(tmp_path):
    return monter(tmp_path, [
        # détectée seulement : EV trop faible pour la porte
        Opp(1, home="A", away="B", ev=3.0, odd=2.00),
        # éligible mais jamais envoyée
        Opp(2, home="C", away="D", ev=12.0, odd=2.00),
        # envoyée, non cliquée, non réglée
        Opp(3, home="E", away="F", ev=12.0, odd=2.20,
            alerte="2026-08-15T10:05:00+00:00"),
        # envoyée, cliquée, réglée
        Opp(4, home="G", away="H", ev=12.0, odd=2.40, joue=True,
            alerte="2026-08-15T10:06:00+00:00", gagnant="home"),
        # cliquée et réglée, mais hors porte (cote trop longue)
        Opp(5, home="I", away="J", ev=12.0, odd=9.00, joue=True,
            gagnant="away"),
    ])


# ── Les effectifs de chaque étage ────────────────────────────────────

def test_detected_prend_tout(tmp_path):
    p = _jeu(tmp_path)
    assert _n(p, Population.DETECTED) == 5


def test_eligible_rejoue_la_porte_de_production(tmp_path):
    p = _jeu(tmp_path)
    ajouter_canal(p, ev_min=8.0, odd_min=1.5, odd_max=4.0)
    # 1 tombe (EV 3 < 8), 5 tombe (cote 9 > 4). Restent 2, 3, 4.
    assert _n(p, Population.ELIGIBLE_FOR_ALERT) == 3


def test_sent_ne_garde_que_les_alertes_reellement_parties(tmp_path):
    p = _jeu(tmp_path)
    assert _n(p, Population.SENT) == 2


def test_clicked_ne_garde_que_les_clics(tmp_path):
    p = _jeu(tmp_path)
    assert _n(p, Population.CLICKED) == 2


def test_settled_ne_garde_que_ce_que_le_moteur_sait_regler(tmp_path):
    p = _jeu(tmp_path)
    assert _n(p, Population.SETTLED) == 2


# ── BET = CLICKED, et il faut le DIRE ────────────────────────────────

def test_bet_est_un_autre_nom_pour_clicked(tmp_path):
    """Le système ne distingue pas un clic d'un pari réellement placé : il
    n'existe aucune confirmation de mise."""
    p = _jeu(tmp_path)
    assert _n(p, Population.BET) == _n(p, Population.CLICKED)
    assert alias_de(Population.BET) is Population.CLICKED


def test_bet_annonce_qu_il_confond_clic_et_pari(tmp_path):
    """⚠️ Une limite tue est pire qu'une limite affichée : sans ce message,
    « BET » se lirait comme une population plus étroite que CLICKED."""
    p = _jeu(tmp_path)
    limites = " ".join(_res(p, Population.BET)["population"]["limites"])
    assert "CLICKED" in limites
    assert "confirmation de mise" in limites


def test_la_population_rendue_dit_ce_qui_a_ete_DEMANDE(tmp_path):
    """Demander BET et lire « clicked » sans plus d'explication ferait croire
    à un filtre ignoré."""
    p = _jeu(tmp_path)
    bloc = _res(p, Population.BET)["population"]
    assert bloc["demandee"] == "bet" and bloc["value"] == "clicked"


# ── Chaque population porte son explication et ses limites ──────────

@pytest.mark.parametrize("pop", list(Population))
def test_chaque_population_a_une_explication(pop):
    assert EXPLICATION[pop] and EXPLICATION[pop].endswith(".")


@pytest.mark.parametrize("pop", [Population.ELIGIBLE_FOR_ALERT,
                                 Population.SENT, Population.BET,
                                 Population.SETTLED])
def test_les_populations_imparfaites_declarent_leurs_limites(pop):
    """DETECTED et CLICKED sont stockées telles quelles ; les quatre autres
    ont une limite connue et doivent la porter."""
    assert LIMITES[pop], f"{pop} n'annonce aucune limite"


def test_sent_annonce_le_probleme_de_cle_d_evenement(tmp_path):
    p = _jeu(tmp_path)
    limites = " ".join(_res(p, Population.SENT)["population"]["limites"])
    assert "value_bet_id" in limites and "tennis" in limites


def test_eligible_annonce_qu_elle_est_RECONSTITUEE(tmp_path):
    """La porte est rejouée sur la configuration ACTUELLE : une opportunité de
    juillet est jugée par les règles d'aujourd'hui."""
    p = _jeu(tmp_path)
    ajouter_canal(p)
    limites = " ".join(_res(p, Population.ELIGIBLE_FOR_ALERT)
                       ["population"]["limites"])
    assert "RECONSTITUÉE" in limites
    assert "borne BASSE" in limites


def test_settled_annonce_les_marches_qu_il_ne_sait_pas_regler(tmp_path):
    p = _jeu(tmp_path)
    limites = " ".join(_res(p, Population.SETTLED)["population"]["limites"])
    assert "h2h" in limites and "totals" in limites


# ── Ce qui reste en Python, et pourquoi ──────────────────────────────

def test_les_deux_filtres_hors_sql_sont_nommes():
    """Une exception à « tout dans la base » doit être déclarée et justifiée,
    pas découverte en lisant le code."""
    assert set(HORS_SQL) == {Population.ELIGIBLE_FOR_ALERT, Population.SETTLED}
    for pop, raison in HORS_SQL.items():
        assert len(raison) > 40, f"{pop} : la raison est trop vague"


def test_eligible_rend_compte_de_ce_qu_elle_a_ecarte(tmp_path):
    """Le filtre Python s'applique sur l'ensemble DÉJÀ réduit par la base, et
    dit combien il a retiré."""
    p = _jeu(tmp_path)
    ajouter_canal(p, ev_min=8.0, odd_min=1.5, odd_max=4.0)
    info = _res(p, Population.ELIGIBLE_FOR_ALERT)["population"]["eligibilite"]
    assert info["avant"] == 5 and info["apres"] == 3
    assert "PREMIUM" in info["porte"]


def test_eligible_dit_D_OU_VIENT_le_seuil_de_fenetre_morte(tmp_path):
    """⚠️ Un seuil pris par défaut faute d'environnement doit SE VOIR : sinon
    le rejeu prétend reproduire un réglage qu'il n'a jamais lu (§11)."""
    p = _jeu(tmp_path)
    ajouter_canal(p)
    info = _res(p, Population.ELIGIBLE_FOR_ALERT)["population"]["eligibilite"]
    assert info["source_seuil"]
    assert info["fenetre_morte_min"] is not None


def test_la_fenetre_morte_peut_etre_imposee_et_elle_MORD(tmp_path):
    """Une détection quatre minutes avant le coup d'envoi n'est pas alertable
    si la fenêtre morte vaut cinq minutes."""
    p = monter(tmp_path, [
        Opp(1, home="A", away="B", ev=12, odd=2.0,
            detecte="2026-08-15T17:56:00+00:00"),      # 4 min avant 18:00
        Opp(2, home="C", away="D", ev=12, odd=2.0,
            detecte="2026-08-15T10:00:00+00:00")])     # 8 h avant
    ajouter_canal(p)
    assert _n(p, Population.ELIGIBLE_FOR_ALERT, fenetre_morte_min=5) == 1
    assert _n(p, Population.ELIGIBLE_FOR_ALERT, fenetre_morte_min=0) == 2


def test_la_mi_temps_n_est_eligible_pour_AUCUN_canal(tmp_path):
    """Aucun canal, ni principal, ni premium, ni critique — quel que soit le
    délai ou l'EV."""
    p = monter(tmp_path, [
        Opp(1, home="A", away="B", market="h2h_h1", ev=20, odd=2.0),
        Opp(2, home="C", away="D", market="h2h", ev=20, odd=2.0)])
    ajouter_canal(p)
    assert _n(p, Population.ELIGIBLE_FOR_ALERT) == 1


def test_settled_ecarte_un_marche_que_le_moteur_ne_sait_pas_regler(tmp_path):
    """Un résultat connu ne suffit pas : `clv.settle` ne traite que `h2h` et
    `totals`. Un pari de mi-temps avec un vainqueur reste NON réglé."""
    p = monter(tmp_path, [
        Opp(1, home="A", away="B", market="h2h", gagnant="home"),
        Opp(2, home="C", away="D", market="h2h_h1", gagnant="home")])
    assert _n(p, Population.SETTLED) == 1


def test_settled_ecarte_un_total_sans_scores(tmp_path):
    """Un total ne se règle pas avec un vainqueur : il lui faut les scores."""
    p = monter(tmp_path, [
        Opp(1, market="totals", outcome="over 2.5", line=2.5, gagnant="home"),
        Opp(2, home="C", away="D", market="totals", outcome="over 2.5",
            line=2.5, gagnant="home", score_dom=2, score_ext=1)])
    assert _n(p, Population.SETTLED) == 1


# ── Population × joué se combinent ───────────────────────────────────

def test_population_et_joue_se_combinent(tmp_path):
    p = _jeu(tmp_path)
    assert _n(p, Population.SENT, joue="oui") == 1
    assert _n(p, Population.SENT, joue="non") == 1
    assert _n(p, Population.DETECTED, joue="oui") == 2
