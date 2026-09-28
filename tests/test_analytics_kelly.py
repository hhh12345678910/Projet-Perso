"""La mise Kelly d'Analytics — tirée de l'EV et de la cote de chaque pari.

Trois paris réglés, calculables à la main (bankroll 1 000 €, quart de Kelly) :

    A  cote 2,0  EV 10 %  gagné   Kelly entier 10 % → mise 25 €  → +25
    B  cote 3,0  EV 20 %  perdu   Kelly entier 10 % → mise 25 €  → −25
    C  cote 1,5  EV  4 %  gagné   Kelly entier  8 % → mise 20 €  → +10

    ROI Kelly = (25 − 25 + 10) / (25 + 25 + 20) = 10 / 70 = 14,29 %
    ROI fixe  = (25 − 25 + 12,5) / 75          = 16,67 %
"""
from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from src.analytics.filtres import FiltreInvalide, Filtres
from src.analytics.metriques import resume
from src.analytics.mise import MiseKelly, mise_de
from src.analytics.segments import _precalculer, resume_rapide
from src.analytics.service import _charger
from src.analytics_ui.app import creer_app
from src.ev import kelly_stake
from tests.analytics_base import Opp, monter

KELLY = {"stake_mode": "kelly", "kelly_fraction": 0.25, "bankroll": 1000,
         "kelly_cap_pct": 100}


@pytest.fixture
def client(tmp_path):
    base = monter(tmp_path, [
        Opp(1, home="A1", away="B1", odd=2.0, ev=10, gagnant="home", cloture=1.9),
        Opp(2, home="A2", away="B2", odd=3.0, ev=20, gagnant="away", cloture=2.8),
        Opp(3, home="A3", away="B3", odd=1.5, ev=4, gagnant="home", cloture=1.45),
    ])
    return TestClient(creer_app(str(base)))


def _resume(client, **params):
    r = client.get("/api/analyse", params=params)
    assert r.status_code == 200, r.text
    return r.json()["summary"]


# ── La mise d'un pari ─────────────────────────────────────────────────

def test_la_mise_kelly_est_CELLE_DU_PROJET():
    """Aucune formule nouvelle : `src.ev.kelly_stake`, la même que la « mise
    conseillée » des alertes, sur la cote juste de la détection."""
    ligne = {"odd_taken": 2.4, "fair_odd": 2.1}
    m = MiseKelly(bankroll=1000, fraction=0.5, plafond_pct=100)
    assert m.pour(ligne) == round(kelly_stake(2.4, 1 / 2.1, 1000, 0.5), 2)


def test_l_EV_pese_lineairement_et_la_cote_tempere():
    """Mise = bankroll × fraction × EV / (cote − 1) : deux fois plus d'EV,
    deux fois plus de mise ; à EV égale, une cote plus haute mise moins."""
    m = MiseKelly(bankroll=1000, fraction=1.0, plafond_pct=100)

    def ligne(cote, ev):
        return {"odd_taken": cote, "fair_odd": cote / (1 + ev)}
    assert m.pour(ligne(2.0, 0.10)) == pytest.approx(100.0, abs=0.01)
    assert m.pour(ligne(2.0, 0.20)) == pytest.approx(2 * m.pour(ligne(2.0, 0.10)), abs=0.02)
    assert m.pour(ligne(5.0, 0.10)) < m.pour(ligne(2.0, 0.10))
    assert m.pour(ligne(5.0, 0.10)) == pytest.approx(1000 * 0.10 / 4, abs=0.01)


def test_le_plafond_borne_une_EV_aberrante():
    """Le faux appariement du 28/09 : Féroé à 6,44 contre la cote juste de
    l'Angleterre, 1,23 (+424 % d'EV). Kelly entier y demanderait 78 % de la
    bankroll ; le plafond la ramène à 3 %."""
    m = MiseKelly(bankroll=1000, fraction=1.0, plafond_pct=3.0)
    assert MiseKelly(bankroll=1000, fraction=1.0, plafond_pct=100).pour(
        {"odd_taken": 6.44, "fair_odd": 1.23}) > 700
    assert m.pour({"odd_taken": 6.44, "fair_odd": 1.23}) == 30.0


def test_une_ligne_sans_avantage_ne_mise_rien():
    m = MiseKelly()
    assert m.pour({"odd_taken": 2.0, "fair_odd": 2.2}) == 0.0
    assert m.pour({"odd_taken": 2.0, "fair_odd": None}) == 0.0


def test_la_mise_fixe_reste_un_nombre():
    assert mise_de(25.0, {"odd_taken": 3.0, "fair_odd": 2.0}) == 25.0


# ── L'analyse ─────────────────────────────────────────────────────────

def test_le_ROI_kelly_est_pondere_par_la_mise(client):
    s = _resume(client, **KELLY)
    assert s["stake_total"] == 70.0
    assert s["pnl"] == 10.0
    assert s["roi"] == 14.29


def test_la_mise_fixe_ne_bouge_pas(client):
    s = _resume(client, stake=25)
    assert s["stake_total"] == 75.0 and s["pnl"] == 12.5 and s["roi"] == 16.67


def test_le_plafond_sapplique_dans_lanalyse(client):
    """Kelly entier, plafond 3 % : 100 €, 100 € et 80 € deviennent 30 € chacun."""
    s = _resume(client, **{**KELLY, "kelly_fraction": 1, "kelly_cap_pct": 3})
    assert s["stake_total"] == 90.0 and s["pnl"] == 15.0


def test_le_detail_porte_la_mise_de_CHAQUE_pari(client):
    r = client.get("/api/detail", params={**KELLY, "sort": "odd_taken", "order": "asc"})
    mises = [(i["odds"], i["stake"], i["pnl"]) for i in r.json()["items"]]
    assert mises == [(1.5, 20.0, 10.0), (2.0, 25.0, 25.0), (3.0, 25.0, -25.0)]


def test_les_decoupes_et_la_matrice_suivent_la_mise(client):
    d = client.get("/api/analyse", params=KELLY).json()
    assert sum(t["stake_total"] for t in d["by_odds"]) == 70.0
    assert sum(c["stake_total"] for c in d["matrix"]["cells"]) == 70.0
    assert d["by_time"][-1]["pnl_cumul"] == 10.0


def test_les_filtres_rendus_disent_la_mise(client):
    f = client.get("/api/analyse", params=KELLY).json()["filters"]
    assert (f["stake_mode"], f["kelly_fraction"], f["bankroll"], f["kelly_cap_pct"]) \
        == ("kelly", 0.25, 1000.0, 100.0)
    assert client.get("/api/analyse").json()["filters"]["stake_mode"] == "flat"


@pytest.mark.parametrize("params", [
    {"stake_mode": "martingale"},
    {"stake_mode": "kelly", "kelly_fraction": 0},
    {"stake_mode": "kelly", "kelly_fraction": 1.5},
    {"stake_mode": "kelly", "bankroll": -10},
    {"stake_mode": "kelly", "kelly_cap_pct": 0},
    {"stake_mode": "kelly", "kelly_cap_pct": 150},
])
def test_une_mise_kelly_invalide_est_REFUSEE(client, params):
    r = client.get("/api/analyse", params=params)
    assert r.status_code == 400, r.text


def test_la_validation_du_filtre_directement():
    with pytest.raises(FiltreInvalide):
        Filtres(stake_mode="kelly", kelly_fraction=2).valider()
    assert Filtres(stake_mode="KELLY").valider().stake_mode == "kelly"


def test_resume_rapide_reste_DACCORD_avec_resume_en_kelly(tmp_path):
    """Le verrou de `segments.resume_rapide`, rejoué en mise Kelly."""
    rnd = random.Random(7)
    opps = [Opp(i, home=f"H{i}", away=f"A{i}", odd=round(rnd.uniform(1.4, 6.0), 2),
                ev=round(rnd.uniform(2.0, 40.0), 1),
                gagnant=rnd.choice(["home", "away", None]),
                cloture=round(rnd.uniform(1.4, 6.0), 2) if rnd.random() < 0.8 else None)
            for i in range(1, 301)]
    lignes, _ = _charger(monter(tmp_path, opps), Filtres().valider())
    mise = Filtres(stake_mode="kelly", kelly_fraction=0.5, kelly_cap_pct=4).valider().mise()
    _precalculer(lignes, mise)
    for _ in range(20):
        lot = rnd.sample(lignes, rnd.randint(1, len(lignes)))
        lent, rapide = resume(lot, mise), resume_rapide(lot, mise)
        for k in ("settled", "roi", "pnl", "stake_total"):
            assert lent[k] == rapide[k], k


# ── L'interface ───────────────────────────────────────────────────────

def test_linterface_envoie_la_mise_kelly_seulement_si_elle_est_choisie():
    from src.analytics_ui.app import STATIQUES
    js = (STATIQUES / "app.js").read_text(encoding="utf-8")
    html = (STATIQUES / "index.html").read_text(encoding="utf-8")
    assert "if ($('f-stake-mode').value === 'kelly')" in js
    for nom in ("stake_mode", "kelly_fraction", "bankroll", "kelly_cap_pct"):
        assert f"'{nom}'" in js, nom
    for ident in ("mise-mode", "f-stake-mode", "f-kelly-fraction", "f-bankroll", "f-kelly-cap"):
        assert f'id="{ident}"' in html, ident
    # La mise d'un pari s'AFFICHE depuis la réponse, elle n'est pas calculée.
    assert "i.stake" in js and "kelly_stake" not in js
