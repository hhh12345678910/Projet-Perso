"""CLV, ROI, règlement, couverture, avertissements.

AUCUNE DÉFINITION N'EST CRÉÉE DANS LA COUCHE ANALYTIQUE, et ces tests le
vérifient : les chiffres rendus sont comparés à `src.clv` appelée directement,
pas à des constantes recopiées. Si quelqu'un réécrivait le CLV « en mieux »
dans `metriques.py`, ces tests tomberaient.

⚠️ ET LA RÈGLE LA PLUS IMPORTANTE : `None` reste `None`. Un pari non réglé
n'est ni perdu, ni nul. Le confondre avec un P&L de zéro tirerait le ROI vers
la moyenne et ferait passer un retard de règlement pour une série de matchs
nuls.
"""
from __future__ import annotations

import pytest

from src.analytics import Filtres, analyser, detail
from src.analytics.metriques import (DEVIG_COMPLET_DEPUIS, SEUIL_COUVERTURE_CLV,
                                     SEUIL_EFFECTIF, SEUIL_SETTLEMENT,
                                     avertissements, clv_de, resume, statut_de)
from src.clv import clv_pct
from src.clv import pnl as clv_pnl
from src.clv import settle as clv_settle
from tests.analytics_base import Opp, monter


def _sommaire(chemin, **kw):
    return analyser(str(chemin), Filtres(**kw))["summary"]


# ── CLV : la définition vient de src.clv ─────────────────────────────

def test_la_clv_est_exactement_celle_de_src_clv(tmp_path):
    p = monter(tmp_path, [Opp(1, odd=2.10, cloture=1.90)])
    attendu = clv_pct(2.10, 1.90) * 100.0
    assert _sommaire(p)["clv"] == pytest.approx(attendu, abs=0.01)


def test_la_clv_se_calcule_sur_la_cloture_DEVIGUEE(tmp_path):
    """⚠️ `clv_snapshots.pinnacle_odd` porte la commission. L'utiliser
    gonflerait chaque CLV de toute la marge : sur ce portefeuille à 6,6 %, un
    pari sans valeur scorerait +6,6 %. La fabrique écrit `pinnacle_odd` à
    0,95 × `fair_odd` — si la couche lisait la mauvaise colonne, le chiffre
    serait franchement différent."""
    p = monter(tmp_path, [Opp(1, odd=2.00, cloture=2.00)])
    assert _sommaire(p)["clv"] == pytest.approx(0.0, abs=0.01)


def test_sans_cloture_il_n_y_a_PAS_de_clv(tmp_path):
    """Et surtout pas une CLV de zéro : l'absence de mesure et une mesure
    nulle ne se ressemblent pas."""
    p = monter(tmp_path, [Opp(1, odd=2.10, cloture=None)])
    s = _sommaire(p)
    assert s["clv"] is None and s["clv_n"] == 0 and s["clv_coverage"] == 0.0


def test_une_cloture_nulle_ou_negative_est_ignoree(tmp_path):
    p = monter(tmp_path, [Opp(1, odd=2.10, cloture=1.90),
                          Opp(2, home="C", away="D", odd=2.10, cloture=0.0)])
    assert _sommaire(p)["clv_n"] == 1


def test_la_mediane_est_rendue_a_cote_de_la_moyenne(tmp_path):
    """Une moyenne seule ne dit rien d'une distribution à queue longue."""
    lignes = [Opp(i, home=f"A{i}", away=f"B{i}", odd=2.0,
                  cloture=[1.0, 1.9, 2.0, 2.05, 2.1][i - 1])
              for i in range(1, 6)]
    s = _sommaire(monter(tmp_path, lignes))
    assert s["clv_median"] is not None
    assert s["clv"] > s["clv_median"], "la queue longue doit tirer la moyenne"


# ── ROI : la formule, énoncée et vérifiée ────────────────────────────

def test_le_roi_est_le_pl_sur_la_mise_des_paris_REGLES(tmp_path):
    """ROI % = 100 × Σ(P&L) / (mise × nombre de RÉGLÉS).

    Ici : un gagnant à 2,00 (+25 €) et un perdant (−25 €) sur 2 × 25 € misés
    → ROI 0 %."""
    p = monter(tmp_path, [
        Opp(1, home="A", away="B", odd=2.00, gagnant="home", outcome="home"),
        Opp(2, home="C", away="D", odd=2.00, gagnant="away", outcome="home")])
    s = _sommaire(p, stake=25)
    assert s["settled"] == 2 and s["stake_total"] == 50.0
    assert s["pnl"] == pytest.approx(0.0) and s["roi"] == pytest.approx(0.0)


def test_le_denominateur_ne_compte_QUE_les_regles(tmp_path):
    """⚠️ Mettre les paris sans résultat au dénominateur diluerait le ROI vers
    zéro à mesure que le règlement prend du retard — et ferait baisser le
    chiffre sans qu'aucun pari n'ait perdu."""
    p = monter(tmp_path, [
        Opp(1, home="A", away="B", odd=3.00, gagnant="home", outcome="home"),
        Opp(2, home="C", away="D", odd=3.00, gagnant=None)])
    s = _sommaire(p, stake=25)
    assert s["settled"] == 1 and s["stake_total"] == 25.0
    assert s["roi"] == pytest.approx(200.0), "un gagnant à 3,00 rend +200 %"


def test_le_pl_vient_de_clv_pnl(tmp_path):
    p = monter(tmp_path, [Opp(1, odd=2.40, gagnant="home", outcome="home")])
    assert _sommaire(p, stake=25)["pnl"] == pytest.approx(
        clv_pnl("won", 2.40, 25))


def test_la_mise_change_le_pl_mais_PAS_le_roi(tmp_path):
    """Le ROI est un ratio : doubler la mise doit doubler le P&L et laisser le
    ROI identique. Si ce n'était pas le cas, la formule serait fausse."""
    p = monter(tmp_path, [Opp(1, odd=2.50, gagnant="home", outcome="home")])
    a, b = _sommaire(p, stake=25), _sommaire(p, stake=50)
    assert b["pnl"] == pytest.approx(2 * a["pnl"])
    assert b["roi"] == pytest.approx(a["roi"])


# ── Règlement : None reste None ──────────────────────────────────────

def test_un_pari_non_regle_n_est_ni_perdu_ni_nul(tmp_path):
    p = monter(tmp_path, [Opp(1, odd=2.00, gagnant=None)])
    s = _sommaire(p)
    assert s["settled"] == 0 and s["unsettled"] == 1
    assert s["roi"] is None and s["pnl"] is None
    assert s["won"] == 0 and s["lost"] == 0 and s["void"] == 0


def test_le_detail_distingue_QUATRE_statuts(tmp_path):
    """gagné / perdu / void / non réglé — quatre états, pas trois."""
    p = monter(tmp_path, [
        Opp(1, home="A", away="B", outcome="home", gagnant="home"),
        Opp(2, home="C", away="D", outcome="home", gagnant="away"),
        Opp(3, home="E", away="F", market="totals", outcome="over 2.5",
            line=2.5, gagnant="home", score_dom=1.5, score_ext=1.0),
        Opp(4, home="G", away="H", gagnant=None)])
    statuts = {i["result"] for i in detail(str(p), Filtres())["items"]}
    assert statuts == {"won", "lost", "void", "unsettled"}


def test_un_push_est_un_VOID_et_rapporte_zero(tmp_path):
    """Total exact sur la ligne : la mise est rendue. Le traiter comme une
    perte biaiserait le P&L vers le bas en silence."""
    p = monter(tmp_path, [Opp(1, market="totals", outcome="over 2.5", line=2.5,
                              gagnant="home", score_dom=1.5, score_ext=1.0)])
    s = _sommaire(p, stake=25)
    assert s["void"] == 1 and s["settled"] == 1
    assert s["pnl"] == pytest.approx(0.0) and s["stake_total"] == 25.0


def test_le_statut_vient_de_clv_settle(tmp_path):
    p = monter(tmp_path, [Opp(1, outcome="home", gagnant="away")])
    item = detail(str(p), Filtres())["items"][0]
    assert item["result"] == clv_settle("h2h", "home", None, "away", None, None)


def test_la_mise_n_est_portee_que_par_un_pari_REGLE(tmp_path):
    """Afficher une mise sur un pari non réglé suggérerait qu'il compte déjà
    dans le ROI."""
    p = monter(tmp_path, [Opp(1, gagnant=None)])
    assert detail(str(p), Filtres())["items"][0]["stake"] is None


# ── Couverture : jamais un chiffre sans son effectif ────────────────

def test_la_clv_est_TOUJOURS_accompagnee_de_sa_couverture(tmp_path):
    """⚠️ +10,4 % sur 95 % du lot et +10,4 % sur 30 % ne sont pas la même
    phrase. Les deux sortent toujours ensemble."""
    p = monter(tmp_path, [Opp(1, cloture=1.9), Opp(2, home="C", away="D")])
    s = _sommaire(p)
    assert s["clv"] is not None and s["clv_coverage"] == 50.0
    assert s["clv_n"] == 1


def test_le_taux_de_settlement_est_rendu(tmp_path):
    p = monter(tmp_path, [Opp(1, gagnant="home"), Opp(2, home="C", away="D")])
    assert _sommaire(p)["settlement_rate"] == 50.0


def test_ev_moyen_et_cote_moyenne_sont_rendus(tmp_path):
    p = monter(tmp_path, [Opp(1, odd=2.0, ev=10), Opp(2, home="C", away="D",
                                                      odd=4.0, ev=20)])
    s = _sommaire(p)
    assert s["odds_mean"] == pytest.approx(3.0)
    assert s["ev_mean"] == pytest.approx(15.0)


# ── Avertissements ───────────────────────────────────────────────────

def _messages(chemin, **kw):
    return analyser(str(chemin), Filtres(**kw))["warnings"]


def test_une_couverture_clv_faible_declenche_un_avertissement(tmp_path):
    lignes = [Opp(i, home=f"A{i}", away=f"B{i}", cloture=1.9 if i < 3 else None,
                  gagnant="home", outcome="home") for i in range(1, 11)]
    p = monter(tmp_path, lignes)
    msgs = " ".join(_messages(p, date_from="2026-08-01"))
    assert "CLV disponible sur" in msgs
    assert _sommaire(p, date_from="2026-08-01")["clv_coverage"] < SEUIL_COUVERTURE_CLV


def test_une_couverture_clv_complete_ne_declenche_RIEN(tmp_path):
    lignes = [Opp(i, home=f"A{i}", away=f"B{i}", cloture=1.9, gagnant="home",
                  outcome="home") for i in range(1, 41)]
    p = monter(tmp_path, lignes)
    assert _messages(p, date_from="2026-08-01") == []


def test_un_settlement_faible_dit_RESULTATS_PROVISOIRES(tmp_path):
    lignes = [Opp(i, home=f"A{i}", away=f"B{i}", cloture=1.9,
                  gagnant="home" if i < 3 else None) for i in range(1, 11)]
    p = monter(tmp_path, lignes)
    msgs = " ".join(_messages(p, date_from="2026-08-01"))
    assert "Résultats provisoires" in msgs
    assert "settlement incomplet" in msgs


def test_un_effectif_sous_le_seuil_est_signale(tmp_path):
    lignes = [Opp(i, home=f"A{i}", away=f"B{i}", cloture=1.9, gagnant="home",
                  outcome="home") for i in range(1, 5)]
    p = monter(tmp_path, lignes)
    msgs = " ".join(_messages(p, date_from="2026-08-01"))
    assert "Indice, pas résultat" in msgs
    assert SEUIL_EFFECTIF == 30


def test_une_periode_avant_aout_annonce_le_trou_de_devig(tmp_path):
    """⚠️ Mesuré sur la base de production : 100 % de juin et 70,1 % de
    juillet 2026 sans ligne de clôture déviguée. Les cotes Pinnacle sont
    purgées depuis, donc c'est IRRÉCUPÉRABLE — et ça doit se dire."""
    p = monter(tmp_path, [Opp(1, jour="2026-07-15", cloture=1.9,
                              gagnant="home", outcome="home")])
    msgs = " ".join(_messages(p, date_from="2026-07-01"))
    assert DEVIG_COMPLET_DEPUIS in msgs
    assert "irrécupérable" in msgs.lower()
    assert "NON ALÉATOIRE" in msgs


def test_une_periode_entierement_posterieure_ne_le_dit_PAS(tmp_path):
    lignes = [Opp(i, home=f"A{i}", away=f"B{i}", cloture=1.9, gagnant="home",
                  outcome="home") for i in range(1, 41)]
    p = monter(tmp_path, lignes)
    msgs = " ".join(_messages(p, date_from="2026-08-01"))
    assert DEVIG_COMPLET_DEPUIS not in msgs


def test_sans_date_de_debut_le_trou_de_devig_est_annonce(tmp_path):
    """Pas de borne basse = la période inclut juin. Se taire laisserait croire
    que la CLV couvre tout l'historique."""
    lignes = [Opp(i, home=f"A{i}", away=f"B{i}", cloture=1.9, gagnant="home",
                  outcome="home") for i in range(1, 41)]
    p = monter(tmp_path, lignes)
    assert DEVIG_COMPLET_DEPUIS in " ".join(_messages(p))


def test_un_lot_vide_le_DIT_au_lieu_de_rendre_des_zeros(tmp_path):
    """⚠️ « Aucune opportunité » ne doit jamais pouvoir se lire « aucun pari
    n'est rentable »."""
    p = monter(tmp_path, [Opp(1, sport="soccer")])
    msgs = _messages(p, sports=("tennis",))
    assert len(msgs) == 1
    assert "lot vide" in msgs[0]
    assert _sommaire(p, sports=("tennis",))["opportunities"] == 0


def test_les_seuils_sont_nommes_et_lisibles():
    """Un seuil enfoui dans un `if` finit par exister en deux exemplaires qui
    ne disent pas la même chose."""
    assert SEUIL_COUVERTURE_CLV == 80.0
    assert SEUIL_SETTLEMENT == 60.0
    assert SEUIL_EFFECTIF == 30
    assert DEVIG_COMPLET_DEPUIS == "2026-08-01"


def test_avertissements_est_appelable_seul():
    """La couche doit être testable sans base ni FastAPI."""
    bloc = {"opportunities": 100, "clv_coverage": 50.0, "clv_n": 50,
            "settlement_rate": 90.0, "settled": 90}
    msgs = avertissements(bloc, date_from="2026-08-01")
    assert len(msgs) == 1 and "CLV disponible" in msgs[0]


# ── Les fonctions unitaires ──────────────────────────────────────────

def test_clv_de_rend_None_sans_cloture():
    assert clv_de({"closing_fair_odd": None, "odd_taken": 2.0}) is None
    assert clv_de({"closing_fair_odd": 0, "odd_taken": 2.0}) is None


def test_statut_de_rend_None_sur_un_marche_non_reglable():
    assert statut_de({"market": "handicap", "outcome_label": "home",
                      "line": -1.0, "winner": "home", "home_score": 2,
                      "away_score": 0}) is None


def test_resume_sur_un_lot_vide_ne_leve_pas():
    s = resume([], 25.0)
    assert s["opportunities"] == 0 and s["roi"] is None
    assert s["clv_coverage"] is None and s["settlement_rate"] is None
