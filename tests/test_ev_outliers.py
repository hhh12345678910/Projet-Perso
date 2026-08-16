"""Verdict sur une EV énorme : le book, ou la référence ?

Le §8 réclame un plafond d'EV depuis juillet, et le §18.8 en donne le cas
d'école — +82,32 % sur Dundee United, où DEUX books indépendants cotaient
3,50-3,65 contre une « juste » à 2,00. Un plafond posé au jugé couperait aussi
de la vraie value : il faut d'abord savoir de quel côté vient l'écart.

Ces tests fixent la règle de décision, parce qu'elle décidera de couper ou non
des paris réels.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "scripts" / "ev_outliers.py"
_spec = importlib.util.spec_from_file_location("ev_outliers", _SRC)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

verdict = mod.verdict


def test_dundee_united_case_blames_the_reference():
    """Plusieurs books s'accordent, seule la ligne juste s'en écarte."""
    peers = {"betano_be": 3.50, "circus_be": 3.65, "unibet_be": 3.55,
             "pinnacle": 2.05}
    tag, _ = verdict(3.60, peers, fair=2.00)
    assert tag == "RÉFÉRENCE SUSPECTE"


def test_lone_book_is_blamed_not_the_reference():
    """Le book détecté est seul très haut : c'est lui qu'il faut regarder.

    Typiquement un mauvais appariement d'événement — le cas redouté après le
    balayage, qui multiplie les matchs par sept."""
    peers = {"betano_be": 2.00, "circus_be": 1.98, "unibet_be": 2.02,
             "pinnacle": 2.05}
    tag, why = verdict(7.20, peers, fair=2.00)
    assert tag == "BOOK SUSPECT"
    assert "7.20" in why


def test_normal_value_is_left_alone():
    """Une vraie value ne doit surtout pas être signalée."""
    peers = {"betano_be": 2.10, "circus_be": 2.05, "unibet_be": 2.08,
             "pinnacle": 2.15}
    tag, _ = verdict(2.30, peers, fair=2.15)
    assert tag == "PLAUSIBLE"


def test_too_few_peers_is_undetermined():
    """Deux books peuvent se tromper ensemble — ils partagent une plateforme.

    Trancher sur deux points produirait du bruit présenté comme un verdict."""
    tag, _ = verdict(7.20, {"betano_be": 2.00, "pinnacle": 2.05}, fair=2.00)
    assert tag == "INDÉTERMINÉ"


def test_sharp_books_are_not_counted_as_peers():
    """Pinnacle et Smarkets SONT la référence : les compter comme pairs
    reviendrait à juger la référence avec elle-même."""
    peers = {"pinnacle": 2.00, "smarkets": 2.02, "betano_be": 3.50}
    tag, _ = verdict(3.60, peers, fair=2.00)
    assert tag == "INDÉTERMINÉ"
