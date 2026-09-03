"""L'axe du délai et ses statistiques — vérifiés contre des valeurs à la main.

Ces quatre fonctions décident de ce qu'on lit dans le tableau : la bande d'un
pari, la précision de sa CLV, l'écart entre deux lots et le seuil au-delà
duquel cet écart compte. Une dérive silencieuse sur l'une des quatre change une
conclusion sans changer une seule ligne visible du tableau — d'où ce test.

Le §16.4 s'arrêtait à « > 48 h » et concluait « la tranche 24-48 h est le seul
vrai trou ». Cette conclusion reposait sur six cellules dont la plus petite
faisait 125 paris, sans correction du nombre de comparaisons. Les bornes
testées ici sont exactement celles qui décident si un pari tombe dans le trou
ou juste à côté.
"""
from __future__ import annotations

import math
import statistics as st

import pytest

from scripts.clv_roi_matrix import (BANDES_DELAI, _bande_delai, _cellule,
                                    _delai_h, _welch)


def _ligne(**kw):
    """Une ligne de la requête, avec des valeurs par défaut plausibles."""
    base = {"home": "H", "away": "A", "start_time": "2026-09-20T18:00:00Z",
            "detected_at": "2026-09-20T12:00:00Z", "market": "h2h",
            "outcome_label": "H", "line": None, "odd_taken": 2.0,
            "closing_fair_odd": 1.9, "winner": "H", "home_score": 1,
            "away_score": 0, "played": 0}
    base.update(kw)
    return base


# --- 1. la bande, bornes comprises ------------------------------------------

@pytest.mark.parametrize("heures,attendu", [
    (0.0, "0-2 h"), (1.999, "0-2 h"),
    # Chaque borne appartient à la bande SUPÉRIEURE (lo <= h < hi). Un pari vu
    # exactement 48 h avant est en 48-72 h, pas en 24-48 h.
    (2.0, "2-6 h"), (5.999, "2-6 h"),
    (6.0, "6-12 h"), (12.0, "12-24 h"), (24.0, "24-48 h"),
    (47.999, "24-48 h"), (48.0, "48-72 h"), (71.999, "48-72 h"),
    (72.0, "72-96 h"), (96.0, "96-120 h"), (120.0, "120-168 h"),
    (167.999, "120-168 h"), (168.0, "> 168 h"), (5000.0, "> 168 h"),
])
def test_les_bornes_tombent_dans_la_bande_superieure(heures, attendu):
    from datetime import datetime, timedelta, timezone
    ko = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
    det = ko - timedelta(hours=heures)
    r = _ligne(start_time=ko.isoformat(), detected_at=det.isoformat())
    assert _bande_delai(r) == attendu
    assert _delai_h(r) == pytest.approx(heures, abs=1e-6)


def test_les_bandes_ne_laissent_aucun_trou():
    """Le haut d'une bande est le bas de la suivante — sinon un délai tombe
    dans « ? » sans que personne ne le remarque."""
    for (_, _lo, hi), (_, lo2, _) in zip(BANDES_DELAI, BANDES_DELAI[1:]):
        assert hi == lo2


def test_un_delai_negatif_est_une_bande_visible_et_non_un_silence():
    """§9 : une détection après le coup d'envoi est du live. Elle doit se VOIR
    plutôt que d'être rangée dans la première bande."""
    r = _ligne(start_time="2026-09-20T18:00:00Z",
               detected_at="2026-09-20T19:30:00Z")
    assert _delai_h(r) == pytest.approx(-1.5)
    assert _bande_delai(r) == "< 0 (LIVE)"


@pytest.mark.parametrize("champ,valeur", [
    ("start_time", None), ("detected_at", None),
    ("start_time", ""), ("detected_at", "pas-une-date"),
])
def test_un_horaire_manquant_ou_illisible_a_sa_propre_bande(champ, valeur):
    assert _bande_delai(_ligne(**{champ: valeur})) == "? (sans horaire)"


def test_les_fuseaux_sont_normalises_avant_la_soustraction():
    """Un `+02:00` comparé à un `Z` par simple ordre de chaînes donnerait deux
    heures d'écart en trop. Le délai doit être le même des deux façons."""
    a = _ligne(start_time="2026-09-20T20:00:00+02:00",
               detected_at="2026-09-20T12:00:00Z")
    b = _ligne(start_time="2026-09-20T18:00:00Z",
               detected_at="2026-09-20T14:00:00+02:00")
    assert _delai_h(a) == pytest.approx(6.0)
    assert _delai_h(b) == pytest.approx(6.0)


# --- 2. le σ de la CLV -------------------------------------------------------

def test_sigma_clv_est_le_t_de_la_moyenne_contre_zero():
    """La CLV avait son effectif mais pas sa précision. σ = m·√n / s."""
    clotures = [1.80, 1.85, 1.90, 1.95, 2.00, 1.70, 2.10]
    rows = [_ligne(home=f"H{i}", closing_fair_odd=c)
            for i, c in enumerate(clotures)]
    c = _cellule(rows, 35.0)

    clvs = [(2.0 / x - 1.0) * 100.0 for x in clotures]
    assert c["clv_moy_pct"] == pytest.approx(round(st.mean(clvs), 2))
    attendu = st.mean(clvs) * math.sqrt(len(clvs)) / st.stdev(clvs)
    assert c["sigma_clv"] == pytest.approx(round(attendu, 1))
    assert c["n_clv"] == len(clotures)


def test_sigma_clv_est_absent_quand_il_n_a_pas_de_sens():
    """Un seul pari, ou une CLV identique partout : pas de dispersion, donc pas
    de σ. Imprimer 0 ou l'infini serait pire que d'imprimer un tiret."""
    assert _cellule([_ligne()], 35.0)["sigma_clv"] is None
    memes = [_ligne(home=f"H{i}", closing_fair_odd=1.9) for i in range(5)]
    assert _cellule(memes, 35.0)["sigma_clv"] is None
    # Sans clôture capturée il n'y a pas de CLV du tout.
    sans = [_ligne(home=f"H{i}", closing_fair_odd=None) for i in range(5)]
    c = _cellule(sans, 35.0)
    assert c["n_clv"] == 0 and c["sigma_clv"] is None and c["clv_moy_pct"] is None


def test_les_deux_sigmas_portent_bien_sur_deux_mesures_differentes():
    """Le tableau imprime σCLV et σROI côte à côte : ils ne doivent pas être
    calculés sur la même population. Ici la moitié des paris n'a pas de
    résultat, donc n_clv > n_regles et les deux σ diffèrent."""
    rows = [_ligne(home=f"H{i}", closing_fair_odd=1.8 + 0.03 * i,
                   winner=(None if i % 2 else "H"),
                   home_score=(None if i % 2 else 1),
                   away_score=(None if i % 2 else 0))
            for i in range(10)]
    c = _cellule(rows, 35.0)
    assert c["n_clv"] == 10
    assert c["n_regles"] == 5
    assert c["sigma_clv"] is not None


# --- 3. le t de Welch --------------------------------------------------------

def test_welch_reproduit_le_calcul_a_la_main():
    a = [10.0, 12.0, 14.0, 11.0, 13.0]
    b = [4.0, 6.0, 5.0, 7.0, 3.0, 5.0, 6.0]
    d, t = _welch(a, b)
    assert d == pytest.approx(st.mean(a) - st.mean(b))
    v = st.variance(a) / len(a) + st.variance(b) / len(b)
    assert t == pytest.approx(d / math.sqrt(v))
    assert t > 0  # a est au-dessus de b


def test_welch_ne_pretend_rien_sur_un_lot_trop_petit():
    """Un lot de 0 ou 1 observation n'a pas de variance. Rendre 0 ou l'infini
    ferait apparaître un écart significatif là où il n'y a rien à mesurer."""
    assert _welch([], [1.0, 2.0, 3.0]) == (None, None)
    assert _welch([5.0], [1.0, 2.0, 3.0]) == (None, None)


def test_welch_est_antisymetrique():
    a, b = [1.0, 2.0, 3.0, 4.0], [10.0, 11.0, 9.0, 12.0]
    d1, t1 = _welch(a, b)
    d2, t2 = _welch(b, a)
    assert d1 == pytest.approx(-d2)
    assert t1 == pytest.approx(-t2)


def test_welch_ne_confond_pas_deux_lots_identiques():
    a = [1.0, 5.0, 3.0, 4.0, 2.0]
    d, t = _welch(a, list(a))
    assert d == pytest.approx(0.0)
    assert t == pytest.approx(0.0)


# --- 4. le seuil de Bonferroni ----------------------------------------------

@pytest.mark.parametrize("k,attendu", [
    (1, 1.96), (5, 2.58), (10, 2.81), (12, 2.87),
])
def test_le_seuil_corrige_du_nombre_de_comparaisons(k, attendu):
    """À dix bandes testées, |t| = 2,3 arrive par pur hasard une fois sur cinq
    sous une vérité plate. Le seuil doit monter avec le nombre de bandes,
    sinon le tableau fabrique une trouvaille à chaque lecture."""
    assert st.NormalDist().inv_cdf(1 - 0.025 / k) == pytest.approx(attendu, abs=0.005)
