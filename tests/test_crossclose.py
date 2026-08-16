"""Rejuger un pari de repli contre la clôture Pinnacle.

Un pari valorisé sur la référence secondaire est aussi clôturé sur elle : on
mesure Smarkets contre lui-même, et battre une ligne bruitée ne prouve rien.
Ces tests fixent la seule règle qui compte ici — comment on tire une « juste »
d'un groupe de clôture — parce qu'elle décidera de garder ou non une source de
référence.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "scripts" / "crossclose.py"
_spec = importlib.util.spec_from_file_location("crossclose", _SRC)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

fair_from_group = mod.fair_from_group


def row(label, line, odd):
    return {"outcome_label": label, "line": line, "decimal_odd": odd}


def test_fair_is_devigged_on_the_whole_market():
    """La marge ne s'enlève qu'en normalisant les issues l'une contre l'autre.

    1,90 / 1,90 porte 5,3 % de marge : la juste doit ressortir à 2,00, pas
    rester à 1,90."""
    g = [row("over", 2.5, 1.90), row("under", 2.5, 1.90)]
    got = fair_from_group(g, "over", 2.5, "multiplicative")
    assert got is not None and abs(got - 2.0) < 0.01


def test_a_lone_outcome_yields_nothing():
    """Une issue seule normaliserait à 100 % et rendrait une « juste » égale à
    la cote affichée — donc une CLV de zéro présentée comme une mesure."""
    assert fair_from_group([row("over", 2.5, 1.90)], "over", 2.5, "shin") is None


def test_unknown_label_yields_nothing():
    g = [row("over", 2.5, 1.90), row("under", 2.5, 1.90)]
    assert fair_from_group(g, "draw", 2.5, "shin") is None


def test_empty_group_yields_nothing():
    assert fair_from_group([], "over", 2.5, "shin") is None


# --- Consensus des softbooks, la seule règle indépendante qui reste ----------

consensus_fair = mod.consensus_fair


def test_consensus_needs_three_books():
    """Deux books belges partagent souvent une plateforme — Unibet, 711,
    Bingoal et Scooore sont tous Kambi. Leur accord ne prouve rien : il reflète
    une seule source de prix."""
    assert consensus_fair([2.00, 2.02]) is None
    assert consensus_fair([2.00, 2.02, 1.98]) is not None


def test_consensus_is_the_median_not_the_mean():
    """Un book qui déraille — c'est précisément ce qu'on cherche — déplacerait
    la moyenne, alors qu'il laisse la médiane intacte."""
    assert consensus_fair([2.00, 2.05, 2.02, 12.00]) == 2.035
    assert consensus_fair([2.00, 2.05, 2.02]) == 2.02


def test_consensus_of_nothing():
    assert consensus_fair([]) is None
