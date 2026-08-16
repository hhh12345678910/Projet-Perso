"""Un pari ne doit jamais exister sans son match.

Le daemon ne construisait ses lignes `events` qu'à partir des événements de
Pinnacle. Tout match qu'il ne price pas — donc précisément ceux où la référence
de repli se déclenche — restait sans ligne : pari sans nom d'équipe, sans
ligue, sans heure de coup d'envoi. Invérifiable, sans CLV possible, et compté
quand même dans les moyennes. Vu le 16/08 : une détection à +90,2 % affichée
« None — None ».
"""
from __future__ import annotations

from src.main import build_event_rows


def test_a_fallback_event_gets_its_row():
    """Le cas de la panne : un match absent de Pinnacle, valorisé sur le repli."""
    rows = build_event_rows({"202608161500::hodd__vs__strommen"}, "soccer", {})
    assert len(rows) == 1
    ek, sport, league, home, away, start = rows[0]
    assert (home, away, sport) == ("hodd", "strommen", "soccer")
    assert start.startswith("2026-08-16T15:00")


def test_the_league_is_carried_when_known():
    """La ligue peut venir de la source de repli, pas seulement de Pinnacle."""
    rows = build_event_rows({"202608161500::hodd__vs__strommen"}, "soccer",
                            {"202608161500::hodd__vs__strommen": "Norway - 1st Division"})
    assert rows[0][2] == "Norway - 1st Division"


def test_an_unreadable_key_is_skipped_not_guessed():
    """Mieux vaut aucune ligne qu'une ligne inventée : une date fausse ferait
    capturer la clôture au mauvais moment, sans lever d'erreur."""
    assert build_event_rows({"nimportequoi"}, "soccer", {}) == []


def test_no_keys_no_rows():
    assert build_event_rows(set(), "soccer", {}) == []
