"""Dépouillement des sondes MagicBetting — retrouver les événements.

Ces sondes servent à répondre à deux questions du §18.6 : quel endpoint rend
l'offre complète, et quels sont les identifiants du tennis. La première se
tranche en COMPTANT les événements par endpoint — donc si le compteur se
trompe, on classe le mauvais endpoint en tête et on part sur une fausse piste.

D'où ces tests : on ne connaît pas la forme de l'enveloppe que rendra
l'endpoint de l'offre complète, et supposer « un tableau nu comme
gettopeventslist » est exactement le genre d'hypothèse qui casse en silence.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "scripts" / "magic_probe_report.py"
_spec = importlib.util.spec_from_file_location("magic_probe_report", _SRC)
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

_events = report._events


def ev(i: int, home: str = "A", away: str = "B") -> dict:
    return {"Id": i, "HT": home, "AT": away, "SId": 1,
            "StakeTypes": [{"Id": 1, "Stakes": [{"N": home, "F": 2.0}]}]}


def test_tableau_nu():
    """La forme de gettopeventslist, celle qui tourne aujourd'hui."""
    assert len(_events([ev(1), ev(2)])) == 2


def test_enveloppe_objet():
    assert len(_events({"Result": [ev(1), ev(2), ev(3)]})) == 3


def test_enveloppe_imbriquee():
    assert len(_events({"data": {"Sports": {"Events": [ev(1)]}}})) == 1


def test_groupe_par_competition():
    """Le cas qui compte : une API qui groupe par compétition.

    S'arrêter au premier groupe rendrait 2 au lieu de 5, et l'endpoint de
    l'offre complète paraîtrait plus pauvre que gettopeventslist."""
    payload = [
        {"CompetitionId": 10, "Events": [ev(1), ev(2)]},
        {"CompetitionId": 11, "Events": [ev(3), ev(4), ev(5)]},
    ]
    assert len(_events(payload)) == 5


def test_doublons_ecartes():
    """Une même liste référencée deux fois ne doit pas gonfler le compte."""
    shared = [ev(1), ev(2)]
    assert len(_events({"top": shared, "all": shared})) == 2


def test_entrees_sans_marches_ignorees():
    """Un menu ou un calendrier nomme des équipes sans porter de cotes.

    Les compter ferait croire à une offre là où il n'y a qu'une liste."""
    menu = [{"Id": 9, "HT": "A", "AT": "B"}, {"Id": 8, "Name": "Premier League"}]
    assert _events(menu) == []


def test_reponse_vide():
    assert _events([]) == []
    assert _events({}) == []
    assert _events(None) == []
