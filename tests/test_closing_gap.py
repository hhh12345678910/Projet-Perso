"""Le sens du test de proportions — l'erreur que cette sonde a déjà eue.

`_prop_diff` porte le sens de l'hypothèse dans l'ORDRE de ses arguments, et
rien dans sa signature ne le rappelle. La première version de `closing_gap`
passait le lot « avec clôture » en premier tout en gardant les verdicts écrits
pour l'autre sens : les deux branches de conclusion étaient inversées, et la
sonde aurait annoncé « hypothèse écartée » exactement quand elle est
confirmée. Un verdict inversé est pire qu'une absence de verdict — il ferme la
question dans le mauvais sens.

Ces tests fixent le sens. Si quelqu'un réordonne les appels, ils tombent.
"""
from __future__ import annotations

import math

import pytest

from scripts.closing_gap import _groupe_tolerant, _prop_diff


# --- le groupe tolérant : deux minutes de coup d'envoi, un seul match --------

def test_deux_horaires_du_meme_match_tombent_dans_le_meme_groupe():
    """C'est toute la mécanique : `event_key` porte la MINUTE du coup d'envoi
    (matcher.py:190), donc un horaire révisé produit une deuxième clé pour le
    même match. Elles doivent se regrouper."""
    a = "202607011800::anderlecht__vs__genk"
    b = "202607011815::anderlecht__vs__genk"
    assert _groupe_tolerant(a) == _groupe_tolerant(b) == ("20260701", "anderlecht__vs__genk")


def test_deux_matchs_differents_ne_se_regroupent_pas():
    base = "202607011800::anderlecht__vs__genk"
    assert _groupe_tolerant(base) != _groupe_tolerant("202607011800::brugge__vs__genk")
    # Un jour différent est un autre match, même équipes : le groupe inclut la
    # date précisément pour ça.
    assert _groupe_tolerant(base) != _groupe_tolerant("202607021800::anderlecht__vs__genk")


def test_une_cle_malformee_ne_fait_pas_tomber_le_groupement():
    """Une clé sans `::` existe en base (historique, imports). Elle doit
    former son propre groupe, pas lever une exception ni tout agréger."""
    assert _groupe_tolerant("nimportequoi") == ("nimportequoi", "")
    assert _groupe_tolerant("a") != _groupe_tolerant("b")


# --- le sens du test de proportions -----------------------------------------

def test_le_premier_lot_est_celui_dont_l_ecart_positif_soutient_l_hypothese():
    """SANS clôture passe en PREMIER. L'hypothèse « ceux qui n'ont pas de
    clôture sont plus souvent déplacés » doit donner un écart POSITIF."""
    # 60 % parmi les 200 sans clôture, 20 % parmi les 400 avec.
    p_sans, p_avec, d, z = _prop_diff(120, 200, 80, 400)
    assert p_sans == pytest.approx(0.60)
    assert p_avec == pytest.approx(0.20)
    assert d == pytest.approx(0.40), "écart POSITIF quand l'hypothèse est vraie"
    assert z > 2, "et un z positif, pas négatif"


def test_le_cas_inverse_donne_bien_un_z_negatif():
    _p1, _p2, d, z = _prop_diff(20, 200, 200, 400)
    assert d == pytest.approx(-0.40)
    assert z < -2


def test_deux_proportions_egales_donnent_un_z_nul():
    _p1, _p2, d, z = _prop_diff(60, 200, 120, 400)
    assert d == pytest.approx(0.0)
    assert z == pytest.approx(0.0)


def test_le_z_reproduit_la_formule_a_deux_proportions():
    a_ok, a_n, b_ok, b_n = 120, 200, 80, 400
    _p1, _p2, _d, z = _prop_diff(a_ok, a_n, b_ok, b_n)
    pa, pb = a_ok / a_n, b_ok / b_n
    p = (a_ok + b_ok) / (a_n + b_n)
    attendu = (pa - pb) / math.sqrt(p * (1 - p) * (1 / a_n + 1 / b_n))
    assert z == pytest.approx(attendu)


@pytest.mark.parametrize("args", [(0, 0, 5, 10), (5, 10, 0, 0), (0, 0, 0, 0)])
def test_un_lot_vide_ne_pretend_rien(args):
    """Un lot vide doit rendre None partout. Rendre 0 ferait lire « aucune
    différence » là où il n'y a aucune mesure."""
    assert _prop_diff(*args) == (None, None, None, None)


def test_deux_proportions_a_zero_ne_divisent_pas_par_zero():
    """Aucun horaire déplacé nulle part : variance nulle, donc pas de z."""
    p_sans, p_avec, d, z = _prop_diff(0, 200, 0, 400)
    assert p_sans == 0.0 and p_avec == 0.0 and d == 0.0
    assert z is None
