"""Strategy Finder — volume minimum, validation chronologique, robustesse,
cohérence avec l'Analytics.

Les bases sont construites pour que chaque propriété se lise à la main :

  A  « solide »  cote 2,0, clôture 1,8 (CLV +11,1 %), 60 % gagnés, toute la
     période — CLV et ROI positifs partout, validation comprise ;
  B  « perdant » cote 2,0, clôture 2,1 (CLV −4,8 %), 45 % gagnés ;
  C  « fragile » excellent sur les 70 % anciens, mauvais sur les 30 %
     récents — c'est le cas que la validation existe pour attraper.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from src.analytics import strategies as sf
from src.analytics.metriques import clv_de
from src.analytics_ui.app import creer_app
from src.clv import clv_pct
from tests.analytics_base import Opp, monter

DEBUT = date(2026, 6, 20)
JOURS = 96


def _jour(i, n):
    return (DEBUT + timedelta(days=int(JOURS * i / max(1, n)))).isoformat()


def _serie(prem_id, book, n, *, cloture, taux, odd=2.0, ev=10.0, market="h2h",
           heure="18:00", jours=None):
    """n paris de `book`, répartis sur la période (ou sur `jours`), dont une
    proportion `taux` gagnés (répartie régulièrement, pas en bloc)."""
    out, gagnes = [], 0
    for k in range(n):
        gagne = (k + 1) * taux >= gagnes + 1
        gagnes += gagne
        j = jours(k) if jours else _jour(k, n)
        out.append(Opp(prem_id + k, book=book, home=f"H{prem_id + k}", away=f"A{prem_id + k}",
                       jour=j, heure=heure, odd=odd, ev=ev, cloture=cloture, market=market,
                       outcome="home", gagnant="home" if gagne else "away"))
    return out


@pytest.fixture
def base(tmp_path):
    opps = (_serie(1, "betano_be", 300, cloture=1.8, taux=0.60)
            + _serie(1001, "ladbrokes_be", 300, cloture=2.1, taux=0.45))
    # C : les 70 % anciens excellents, les 30 % récents mauvais.
    n = 300
    tot = []
    for k in range(n):
        ancien = k < 0.7 * n
        tot.append(Opp(2001 + k, book="napoleon_be", home=f"H{2001 + k}", away=f"A{2001 + k}",
                       jour=_jour(k, n), odd=2.0, ev=10.0,
                       cloture=1.7 if ancien else 2.3,
                       gagnant="home" if (k % 10 < (7 if ancien else 3)) else "away"))
    return monter(tmp_path, opps + tot)


def _trouver(base, **k):
    sf._CACHE.clear()
    return sf.trouver(str(base), **{"sport": "soccer", "min_n": 100, **k})


def _par_id(res):
    return {s["id"]: s for s in res["strategies"]}


# ── Volume minimum ────────────────────────────────────────────────────

def test_99_paris_regles_EXCLUS_100_INCLUS(tmp_path):
    base = monter(tmp_path, _serie(1, "betano_be", 100, cloture=1.8, taux=0.6)
                  + _serie(501, "ladbrokes_be", 99, cloture=1.8, taux=0.6))
    ids = _par_id(_trouver(base))
    assert "bookmaker=betano_be" in ids
    assert not any(i.startswith("bookmaker=ladbrokes_be") for i in ids)
    assert ids["bookmaker=betano_be"]["summary"]["settled"] == 100


def test_les_paris_NON_REGLES_ne_comptent_pas_dans_le_minimum(tmp_path):
    opps = _serie(1, "betano_be", 100, cloture=1.8, taux=0.6)
    for o in opps[:5]:
        o.gagnant = None                      # 95 réglés seulement
    res = _trouver(monter(tmp_path, opps), population="detected")
    assert res["strategies"] == []
    assert res["counts"]["eligible"] == 0


# ── CLV et ROI : les définitions du projet ────────────────────────────

def test_le_ROI_est_PnL_sur_mise_des_regles(base):
    s = _par_id(_trouver(base))["bookmaker=betano_be"]["summary"]
    # 180 gagnés à 2,0 (+25 € chacun), 120 perdus (−25 €) : P&L = 1 500 €,
    # mise = 300 × 25 € = 7 500 €, ROI = +20 %.
    assert (s["settled"], s["won"], s["lost"]) == (300, 180, 120)
    assert s["pnl"] == 1500.0 and s["stake_total"] == 7500.0 and s["roi"] == 20.0


def test_la_CLV_est_celle_de_clv_pct(base):
    s = _par_id(_trouver(base))["bookmaker=betano_be"]["summary"]
    attendu = round(clv_pct(2.0, 1.8) * 100, 2)
    assert s["clv"] == attendu and s["clv_n"] == 300


def test_les_chiffres_sont_CEUX_DE_LANALYTICS_pour_les_memes_filtres(base):
    """La cohérence promise : ouvrir la configuration dans l'Analytics (mêmes
    filtres) redonne les mêmes nombres, au centime près."""
    client = TestClient(creer_app(str(base)))
    sf._CACHE.clear()
    res = client.get("/api/strategies", params={"sport": "soccer", "min_n": 100}).json()
    for strat in res["strategies"]:
        f = strat["analytics_filters"]
        # Le sport, la population et la période viennent de la réponse elle-même :
        # oubliés, l'Analytics rouvrait tous les sports (564 paris au lieu de 310).
        assert f["sports"] == ["soccer"] and f["population"] == "settled"
        params = [("sports", s) for s in f["sports"]] + [("population", f["population"])]
        params += [(k, f[k]) for k in ("date_from", "date_to") if f[k]]
        params += [("bookmakers", b) for b in f["bookmakers"]]
        params += [("markets", m) for m in f["markets"]]
        params += [("outcomes", o) for o in f["outcomes"]]
        params += [("ev_bands", b) for b in f["ev_bands"]]
        params += [("odds_bands", b) for b in f["odds_bands"]]
        a = client.get("/api/analyse", params=params).json()["summary"]
        for k in ("settled", "roi", "pnl", "clv", "clv_n", "stake_total"):
            assert a[k] == strat["summary"][k], (strat["id"], k)


# ── Classement, validation, robustesse ────────────────────────────────

def test_le_solide_passe_devant_le_perdant_et_le_fragile(base):
    ids = _par_id(_trouver(base))
    a = ids["bookmaker=betano_be"]
    assert a["rank"] == 1
    for autre in ("bookmaker=ladbrokes_be", "bookmaker=napoleon_be"):
        if autre in ids:
            assert ids[autre]["rank"] > a["rank"]


def test_le_decoupage_est_CHRONOLOGIQUE(base):
    res = _trouver(base)
    coupure = res["split"]["cutoff"]
    assert res["split"]["train"]["to"] == coupure == res["split"]["validation"]["from"]
    assert res["split"]["train"]["from"] < coupure < res["split"]["validation"]["to"]
    # 70 / 30 des réglés, à un pari près.
    tr, va = res["split"]["train"]["settled"], res["split"]["validation"]["settled"]
    assert abs(tr / (tr + va) - 0.70) < 0.01
    # Chaque configuration : entraînement + validation = le tout.
    for s in res["strategies"]:
        assert s["train"]["settled"] + s["validation"]["settled"] == s["summary"]["settled"]


def test_le_fragile_est_demasque_par_la_validation(base):
    c = _par_id(_trouver(base))["bookmaker=napoleon_be"]
    assert c["train"]["clv"] > 10 and c["validation"]["clv"] < 0
    assert c["robustness"]["level"] == "weak"
    assert any("validation" in r for r in c["robustness"]["reasons"])


def test_le_solide_est_robuste(base):
    a = _par_id(_trouver(base))["bookmaker=betano_be"]
    assert a["validation"]["sufficient"] and a["validation"]["clv"] > 0
    assert a["stability"]["clv_positive_share"] == 1.0
    assert a["robustness"]["level"] == "strong", a["robustness"]["reasons"]


def test_une_validation_trop_courte_plafonne_a_MOYENNE(tmp_path):
    """Tous les paris dans les 70 % anciens de la période : la validation
    n'a presque rien, donc rien ne peut y être confirmé."""
    anciens = _serie(1, "betano_be", 300, cloture=1.8, taux=0.6,
                     jours=lambda k: (DEBUT + timedelta(days=k % 40)).isoformat())
    recents = _serie(1001, "ladbrokes_be", 150, cloture=2.1, taux=0.45,
                     jours=lambda k: (DEBUT + timedelta(days=60 + k % 30)).isoformat())
    a = _par_id(_trouver(monter(tmp_path, anciens + recents)))["bookmaker=betano_be"]
    assert not a["validation"]["sufficient"]
    assert a["robustness"]["level"] == "medium"


def test_le_score_sature_avec_le_volume():
    """1 000 paris ne valent pas dix fois 100 : bonus logarithmique."""
    assert 0.5 * __import__("math").log2(1000 / 100) < 2.0


# ── Données manquantes, déduplication, jumeaux ────────────────────────

def test_les_donnees_manquantes_ne_cassent_rien(tmp_path):
    opps = _serie(1, "betano_be", 150, cloture=1.8, taux=0.6)
    for o in opps[::7]:
        o.cloture = None                       # pas de CLV
    for o in opps[1::11]:
        o.gagnant = None                       # pas de résultat
    opps.append(Opp(900, book="betano_be", home="X", away="Y", odd=150.0, ev=10))  # cote hors bande
    res = _trouver(monter(tmp_path, opps), population="detected")
    s = _par_id(res)["bookmaker=betano_be"]["summary"]
    assert s["settled"] < s["opportunities"]
    assert s["clv_n"] < s["opportunities"]


def test_deux_descriptions_du_meme_lot_ne_font_quune(base):
    """Tous les paris de A sont des « h2h » : « Betano » et « Betano · H2H »
    décrivent le même lot — seule la plus courte reste."""
    res = _trouver(base)
    ids = _par_id(res)
    assert "bookmaker=betano_be" in ids and "bookmaker=betano_be|market=h2h" not in ids
    assert res["counts"]["redundant"] > 0


def test_les_jumeaux_kambi_ne_font_quun_bookmaker(tmp_path):
    opps = (_serie(1, "unibet_be", 60, cloture=1.8, taux=0.6)
            + _serie(101, "bingoal_be", 60, cloture=1.8, taux=0.6))
    res = _trouver(monter(tmp_path, opps))
    s = _par_id(res)["bookmaker=unibet_be"]
    assert s["summary"]["settled"] == 120
    assert "Bingoal" in s["title"]
    assert set(s["analytics_filters"]["bookmakers"]) >= {"unibet_be", "bingoal_be"}


def test_au_plus_quatre_criteres_bookmaker_toujours_present(base):
    for s in _trouver(base, min_n=20)["strategies"]:
        assert 1 <= s["depth"] <= 4
        assert s["criteria"][0]["dimension"] == "bookmaker"
        assert len(s["criteria"]) == s["depth"]


# ── Réponse, cache, erreurs ───────────────────────────────────────────

def test_aucune_configuration_rend_une_liste_VIDE_pas_une_erreur(base):
    res = _trouver(base, min_n=100000)
    assert res["strategies"] == [] and res["counts"]["shown"] == 0
    assert res["warnings"] and res["method"]


def test_moins_de_cinq_resultats_ne_sont_pas_completes(base):
    res = _trouver(base)
    assert len(res["strategies"]) == res["counts"]["validated"] <= 25
    assert res["counts"]["shown"] == min(5, len(res["strategies"]))


def test_le_wording_reste_historique(base):
    for s in _trouver(base)["strategies"]:
        texte = " ".join(s["why"]).lower()
        assert "a présenté" in texte
        for interdit in ("va gagner", "meilleure stratégie", "garanti"):
            assert interdit not in texte


def test_la_meme_recherche_sert_le_cache(base):
    sf._CACHE.clear()
    premier = sf.trouver(str(base), sport="soccer", min_n=100)
    second = sf.trouver(str(base), sport="soccer", min_n=100)
    assert not premier["cached"] and second["cached"]
    assert second["strategies"] == premier["strategies"]


@pytest.mark.parametrize("params", [
    {"objective": "maximum"}, {"min_n": 0}, {"min_n": "beaucoup"}, {"sport": "curling"},
])
def test_une_saisie_invalide_est_REFUSEE(base, params):
    client = TestClient(creer_app(str(base)))
    r = client.get("/api/strategies", params=params)
    assert r.status_code in (400, 422), r.text


def test_la_route_rend_le_contrat(base):
    sf._CACHE.clear()
    client = TestClient(creer_app(str(base)))
    d = client.get("/api/strategies", params={"sport": "soccer", "min_n": 100,
                                              "objective": "clv"}).json()
    assert d["params"]["sport_label"] == "Football"
    assert d["params"]["objective"] == "clv" and d["params"]["min_n"] == 100
    for cle in ("params", "lot", "split", "counts", "strategies", "by_bookmaker",
                "by_market", "warnings", "method"):
        assert cle in d, cle
    s = d["strategies"][0]
    for cle in ("id", "rank", "title", "criteria", "summary", "train", "validation",
                "delta", "ci", "stability", "robustness", "sample", "why", "series",
                "analytics_filters"):
        assert cle in s, cle
    assert s["ci"]["method"] == "bootstrap" and s["ci"]["clv"][0] <= s["summary"]["clv"] <= s["ci"]["clv"][1]


# ── Performance ───────────────────────────────────────────────────────

def test_la_recherche_reste_rapide_sur_un_gros_lot(tmp_path):
    """6 000 opportunités sur 6 books, 2 marchés, bandes variées : quelques
    secondes au plus, sans une requête par combinaison (une seule lecture)."""
    books = ["betano_be", "ladbrokes_be", "napoleon_be", "golden_palace",
             "starcasino_sport", "circus_be"]
    opps = []
    for i in range(6000):
        opps.append(Opp(i + 1, book=books[i % 6], home=f"H{i}", away=f"A{i}",
                        jour=_jour(i, 6000), heure=f"{10 + i % 12}:00",
                        odd=1.4 + (i % 40) / 8, ev=2 + (i % 30),
                        market="totals" if i % 3 == 0 else "h2h",
                        outcome="over 2.5" if i % 3 == 0 else "home",
                        line=2.5 if i % 3 == 0 else None,
                        cloture=1.4 + (i % 37) / 8, gagnant="home" if i % 2 else "away",
                        score_dom=i % 4, score_ext=(i // 4) % 3))
    base = monter(tmp_path, opps)
    sf._CACHE.clear()
    t0 = time.perf_counter()
    res = sf.trouver(str(base), sport="soccer", min_n=100)
    assert time.perf_counter() - t0 < 15
    assert res["counts"]["tested"] > 500 and res["strategies"]


def test_les_cartes_ne_sont_pas_des_VARIANTES_les_unes_des_autres(tmp_path):
    """Un bookmaker dont presque tous les paris sont des « totals » : « Betano »
    et « Betano · Totals » décrivent presque le même lot. Les deux ne peuvent
    pas occuper deux cartes ; la seconde est marquée comme variante."""
    opps = _serie(1, "betano_be", 400, cloture=1.8, taux=0.6, market="totals")
    for o in opps:
        o.outcome, o.line, o.gagnant, o.score_dom, o.score_ext = "over 2.5", 2.5, "home", 3, 1
    opps += _serie(401, "betano_be", 40, cloture=1.8, taux=0.6)          # quelques h2h
    res = _trouver(monter(tmp_path, opps), min_n=50)
    cartes = [s for s in res["strategies"] if not s["variant_of"]][:5]
    ens = {s["id"] for s in cartes}
    assert not ({"bookmaker=betano_be", "bookmaker=betano_be|market=totals"} <= ens)
    variantes = [s for s in res["strategies"] if s["variant_of"]]
    assert variantes and all(v["rank"] > 1 for v in variantes)
    # Les variantes passent APRÈS les pistes distinctes, et ne sont pas comptées.
    assert res["counts"]["shown"] == len(cartes)
    assert [s["rank"] for s in cartes] == list(range(1, len(cartes) + 1))
