"""Durée des cycles — ce qu'il faut compter, et surtout ce qu'il ne faut pas.

Un cycle qui s'allonge ne lève aucune erreur : les cotes arrivent plus vieilles
et les détections tombent après que le prix a bougé. C'est donc une mesure
qu'on regarde souvent, et une mesure fausse ferait conclure à l'envers.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "scripts" / "cycle_speed.py"


def _module(log_path):
    import os
    os.environ["CYCLE_LOG"] = str(log_path)
    spec = importlib.util.spec_from_file_location("cycle_speed", _SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _log(tmp_path, heures):
    p = tmp_path / "v.log"
    p.write_text("".join(
        f"══ CYCLE {i} — {h} UTC ══\n" for i, h in enumerate(heures, 1)))
    return p


def test_simple_durations(tmp_path):
    m = _module(_log(tmp_path, ["10:00:00", "10:00:45", "10:01:30"]))
    assert m.durations() == [45.0, 45.0]


def test_a_restart_is_not_a_cycle(tmp_path):
    """Un `systemctl restart` laisse plusieurs minutes entre deux en-têtes.

    Le compter ferait passer une journée saine pour une dérive."""
    m = _module(_log(tmp_path, ["10:00:00", "10:00:45", "10:20:00", "10:20:45"]))
    assert m.durations() == [45.0, 45.0]


def test_midnight_does_not_produce_a_negative(tmp_path):
    m = _module(_log(tmp_path, ["23:59:30", "00:00:15"]))
    assert m.durations() == [45.0]


def test_other_lines_are_ignored(tmp_path):
    p = tmp_path / "v.log"
    p.write_text("══ CYCLE 1 — 10:00:00 UTC ══\n"
                 "[soccer]   →  1234 quotes  Pinnacle\n"
                 "══ CYCLE 2 — 10:00:45 UTC ══\n")
    m = _module(p)
    assert m.durations() == [45.0]


def test_a_restart_with_a_SHORT_gap_is_excluded(tmp_path):
    """Le cas réel du 16/08 : cycle interrompu à 13:30:44, nouveau processus à
    13:30:46. Deux secondes d'écart — sous le plafond de durée, donc compté
    comme un cycle éclair. C'est le NUMÉRO qui trahit le redémarrage."""
    p = tmp_path / "v.log"
    p.write_text(
        "══ CYCLE 18 — 13:29:58 UTC ══\n"
        "══ CYCLE 19 — 13:30:44 UTC ══\n"
        "══ CYCLE 1 — 13:30:46 UTC ══\n"
        "══ CYCLE 2 — 13:31:34 UTC ══\n"
    )
    m = _module(p)
    assert m.durations() == [46.0, 48.0]     # jamais le 2 s du redémarrage
