"""PHASE 4 — les meilleurs segments, et surtout leurs garde-fous.

Un module qui cherche « le meilleur segment » parmi des centaines de
combinaisons FABRIQUE un gagnant même sur du bruit pur. La moitié de ce
fichier ne teste donc pas que les segments sont bons : elle teste que le
module dit assez de vérité pour qu'on ne les croie pas trop.

Le test le plus important est `test_sur_du_BRUIT_PUR_il_sort_quand_meme_un
_podium` : il montre le danger en acte, sur des données où il n'y a rien à
trouver par construction.
"""
from __future__ import annotations

import random

import pytest

from src.analytics.filtres import Filtres, FiltreInvalide
from src.analytics.metriques import resume
from src.analytics.perimetre import MIN_SEGMENT
from src.analytics.segments import _precalculer, resume_rapide
from src.analytics.service import _charger, meilleurs_segments
from tests.analytics_base import Opp, monter

BOOKS = ["unibet_be", "betano_be", "betfirst", "napoleon_be"]


def _lot(tmp_path, n=600, graine=11):
    """Un lot TIRÉ AU HASARD : aucun segment n'a d'edge réel, par
    construction. C'est exactement ce qu'il faut pour éprouver les garde-fous.
    """
    rnd = random.Random(graine)
    opps = []
    for i in range(1, n + 1):
        opps.append(Opp(
            id=i, sport=rnd.choice(["soccer", "tennis"]),
            home=f"H{i}", away=f"A{i}", book=rnd.choice(BOOKS),
            market=rnd.choice(["h2h", "totals"]),
            line=2.5 if rnd.random() < 0.3 else None,
            odd=round(rnd.uniform(1.4, 7.0), 2),
            ev=round(rnd.uniform(3.0, 40.0), 1),
            jour=f"2026-08-{rnd.randint(1, 28):02d}",
            heure=f"{rnd.randint(10, 22)}:00",
            cloture=(round(rnd.uniform(1.4, 7.0), 2)
                     if rnd.random() < 0.85 else None),
            gagnant=rnd.choice(["home", "away", None]),
            score_dom=rnd.randint(0, 3), score_ext=rnd.randint(0, 3)))
    return monter(tmp_path, opps)


# ══ L'équivalence qui autorise l'optimisation ═══════════════════════

def test_resume_rapide_est_D_ACCORD_avec_resume(tmp_path):
    """⚠️ LE VERROU QUI REND `resume_rapide` ACCEPTABLE.

    C'est une seconde implémentation de l'agrégation, écrite pour la vitesse :
    sans mémoïsation, une ligne présente dans quarante et un groupements
    serait réglée quarante et une fois. Elle n'est tolérable que parce que son
    accord exact avec `metriques.resume` est vérifié ici, sur des lots tirés
    au hasard. Si elle divergeait, ce test tomberait — pas un chiffre bizarre
    dans une interface, trois semaines plus tard."""
    p = _lot(tmp_path, 400)
    lignes, _ = _charger(p, Filtres().valider())
    _precalculer(lignes, 25.0)
    rnd = random.Random(4)
    cles = ["opportunities", "settled", "settlement_rate", "clv_n",
            "clv_coverage", "clv", "roi", "pnl", "stake_total", "played"]
    for _ in range(30):
        lot = rnd.sample(lignes, rnd.randint(1, len(lignes)))
        lent, rapide = resume(lot, 25.0), resume_rapide(lot, 25.0)
        for k in cles:
            assert lent[k] == rapide[k], f"divergence sur {k}"


def test_resume_rapide_sur_un_lot_VIDE():
    assert resume_rapide([], 25.0)["opportunities"] == 0
    assert resume_rapide([], 25.0)["roi"] is None


# ══ Le danger, montré en acte ══════════════════════════════════════

def test_sur_du_BRUIT_PUR_il_sort_quand_meme_un_podium(tmp_path):
    """⚠️ CE TEST EXISTE POUR DOCUMENTER LE PIÈGE, PAS POUR LE CORRIGER.

    Les données sont tirées au hasard : aucun bookmaker, aucune bande de cote,
    aucun sport n'a le moindre avantage réel. Le module rend pourtant un
    classement, et son premier segment affiche une CLV flatteuse. C'est le
    comportement ATTENDU d'un chercheur de segments — et c'est précisément
    pourquoi la mise en garde doit accompagner le résultat partout où il
    s'affiche."""
    p = _lot(tmp_path)
    r = meilleurs_segments(p, Filtres(), min_n=40, limite=10)
    assert r["segments"], "aucun segment sur 600 paris : le seuil est trop haut"
    assert r["segments"][0]["clv"] > r["overall"]["clv"], (
        "le premier segment doit dépasser le lot entier — c'est la sélection "
        "qui le produit, pas un edge")
    assert r["combinaisons_testees"] > 100


def test_le_nombre_de_combinaisons_testees_est_TOUJOURS_rendu(tmp_path):
    p = _lot(tmp_path)
    r = meilleurs_segments(p, Filtres(), min_n=40)
    assert r["combinaisons_testees"] > 0
    assert r["retenus"] <= r["combinaisons_testees"]
    assert r["ecartes_effectif"] >= 0
    assert (r["retenus"] + r["ecartes_effectif"]
            + r["redondants_fusionnes"]) <= r["combinaisons_testees"]


def test_la_mise_en_garde_DATA_MINING_est_toujours_presente(tmp_path):
    p = _lot(tmp_path)
    r = meilleurs_segments(p, Filtres(), min_n=40)
    joint = " ".join(r["warnings"])
    assert "DATA MINING" in joint
    assert str(r["combinaisons_testees"]) in joint
    assert "hasard" in joint


def test_le_lot_ENTIER_est_rendu_comme_repere(tmp_path):
    """Un segment à +12 % ne dit pas la même chose selon que le lot entier est
    à +2 % ou à +11 %. Sans le repère, le podium se lit comme un exploit."""
    p = _lot(tmp_path)
    r = meilleurs_segments(p, Filtres(), min_n=40)
    assert r["overall"]["opportunities"] > 0
    assert "clv" in r["overall"] and "roi" in r["overall"]


# ══ Le classement ══════════════════════════════════════════════════

def test_le_classement_par_defaut_est_la_CLV_pas_le_ROI(tmp_path):
    """La CLV est ~8 fois moins bruitée par pari : trier par ROI reviendrait à
    trier par chance."""
    p = _lot(tmp_path)
    r = meilleurs_segments(p, Filtres(), min_n=40)
    assert r["trier_par"] == "clv"
    clvs = [s["clv"] for s in r["segments"]]
    assert clvs == sorted(clvs, reverse=True)


def test_trier_par_ROI_est_possible_mais_AVERTI(tmp_path):
    p = _lot(tmp_path)
    r = meilleurs_segments(p, Filtres(), min_n=40, trier_par="roi")
    rois = [s["roi"] for s in r["segments"]]
    assert rois == sorted(rois, reverse=True)
    joint = " ".join(r["warnings"])
    assert "bruité" in joint and "1 050" in joint


def test_un_tri_inconnu_est_refuse(tmp_path):
    p = _lot(tmp_path)
    with pytest.raises(FiltreInvalide):
        meilleurs_segments(p, Filtres(), trier_par="pnl")


# ══ Le plancher d'effectif ═════════════════════════════════════════

def test_le_plancher_ecarte_et_le_COMPTE(tmp_path):
    p = _lot(tmp_path)
    bas = meilleurs_segments(p, Filtres(), min_n=20)
    haut = meilleurs_segments(p, Filtres(), min_n=200)
    assert haut["retenus"] < bas["retenus"]
    assert haut["ecartes_effectif"] > bas["ecartes_effectif"]
    for s in haut["segments"]:
        assert s["opportunities"] >= 200


def test_le_plancher_par_defaut_vient_du_module(tmp_path):
    p = _lot(tmp_path)
    r = meilleurs_segments(p, Filtres())
    assert r["min_n"] == MIN_SEGMENT


def test_aucun_segment_nest_une_erreur_explicable(tmp_path):
    """Un plancher hors de portée doit rendre une liste vide, pas planter."""
    p = _lot(tmp_path, 60)
    r = meilleurs_segments(p, Filtres(), min_n=10_000)
    assert r["segments"] == []
    assert r["retenus"] == 0
    assert r["ecartes_effectif"] > 0


# ══ Les descriptions redondantes ═══════════════════════════════════

def test_deux_descriptions_du_MEME_lot_ne_font_quune_ligne(tmp_path):
    """Si tous les paris d'un book sont sur un seul marché, « book=X » et
    « book=X × marché=Y » désignent le même lot. Deux lignes identiques dans
    le podium donneraient l'impression que deux pistes convergent."""
    opps = [Opp(id=i, home=f"H{i}", away=f"A{i}", book="betfirst",
                market="h2h", sport="soccer", odd=2.0, ev=10.0,
                cloture=1.9, gagnant="home", outcome="home")
            for i in range(1, 121)]
    p = monter(tmp_path, opps)
    r = meilleurs_segments(p, Filtres(), min_n=100, limite=50)
    tailles = [s["opportunities"] for s in r["segments"]]
    assert tailles.count(120) == 1, (
        "un même lot de 120 paris est décrit plusieurs fois")
    assert r["redondants_fusionnes"] > 0
    # La description conservée est la plus COURTE.
    assert r["segments"][0]["profondeur"] == 1


# ══ Périmètre et filtres ═══════════════════════════════════════════

def test_les_segments_respectent_les_filtres_courants(tmp_path):
    p = _lot(tmp_path)
    r = meilleurs_segments(p, Filtres(sports=("tennis",)), min_n=30)
    for s in r["segments"]:
        for c in s["criteres"]:
            if c["dimension"] == "sport":
                assert c["value"] == "tennis"
    assert r["overall"]["opportunities"] < 600


def test_les_segments_respectent_lEV_par_sport(tmp_path):
    p = _lot(tmp_path)
    f = Filtres(ev_par_sport=(("soccer", ("15-35%",)), ("tennis", ("5-8%",))))
    r = meilleurs_segments(p, f, min_n=10)
    for s in r["segments"]:
        crit = {c["dimension"]: c["value"] for c in s["criteres"]}
        if crit.get("sport") == "soccer":
            assert crit.get("ev") in (None, "15-35%")
        if crit.get("sport") == "tennis":
            assert crit.get("ev") in (None, "5-8%")


def test_les_criteres_portent_un_LIBELLE_lisible(tmp_path):
    p = _lot(tmp_path)
    r = meilleurs_segments(p, Filtres(), min_n=40)
    for s in r["segments"]:
        for c in s["criteres"]:
            assert c["display"]
            if c["dimension"] == "bookmaker" and c["value"] == "unibet_be":
                assert c["display"] == "Unibet BE"
            if c["dimension"] == "sport" and c["value"] == "soccer":
                assert c["display"] == "Soccer"


def test_chaque_segment_porte_ses_effectifs_et_sa_couverture(tmp_path):
    p = _lot(tmp_path)
    r = meilleurs_segments(p, Filtres(), min_n=40)
    for s in r["segments"]:
        for cle in ("opportunities", "settled", "clv_n", "clv_coverage",
                    "settlement_rate", "pnl", "sample", "sample_settled"):
            assert cle in s, f"{cle} manque à un segment"


def test_la_profondeur_est_bornee(tmp_path):
    p = _lot(tmp_path)
    r = meilleurs_segments(p, Filtres(), min_n=30, profondeur=1)
    assert all(s["profondeur"] == 1 for s in r["segments"])
    r3 = meilleurs_segments(p, Filtres(), min_n=30, profondeur=3)
    assert max(s["profondeur"] for s in r3["segments"]) <= 3
    assert r3["combinaisons_testees"] > r["combinaisons_testees"]


def test_la_troncature_est_ANNONCEE(tmp_path):
    """Une liste coupée en silence se lit comme une liste complète."""
    p = _lot(tmp_path)
    r = meilleurs_segments(p, Filtres(), min_n=30, limite=3)
    assert len(r["segments"]) == 3
    assert r["tronque"] == r["retenus"] - 3
