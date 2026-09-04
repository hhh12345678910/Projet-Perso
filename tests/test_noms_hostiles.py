"""La sonde des noms hostiles voit-elle ce qui casse, et SEULEMENT ça ?

Une sonde qui crie au loup sur des noms parfaitement valides est pire
qu'absente : elle enverrait échapper deux fois, ou soupçonner un book innocent.
"""
from __future__ import annotations

import sqlite3

import pytest

from scripts.noms_hostiles import hostile, main


@pytest.mark.parametrize("v,motif", [
    ("Brighton & Hove Albion", "& nu"),
    ("Bosnia & Herzegovina", "& nu"),
    ("U<19 Cup", "<"),
    ("A & B < C", "& nu"),          # le premier motif trouvé suffit
])
def test_les_noms_qui_cassent_sont_vus(v, motif):
    assert hostile(v) == motif


@pytest.mark.parametrize("v", [
    "Premier League", "AS Roma", "Saint-Étienne", "1. FC Köln",
    'Dinamo "Kiev"', "Over/Under 2.5", "Bayern &amp; Dortmund",
    "&lt;19", "&#233;quipe", None, "",
])
def test_les_noms_valides_ne_sont_pas_accuses(v):
    """`&amp;`, `&lt;` et `&#233;` sont des entités VALIDES : les signaler
    ferait échapper deux fois et afficherait « &amp;amp; » à l'écran."""
    assert hostile(v) is None


def _base(tmp_path, events, teams=()):
    p = tmp_path / "v.db"
    c = sqlite3.connect(str(p))
    c.executescript(
        "CREATE TABLE events (event_key TEXT PRIMARY KEY, sport TEXT, "
        "league TEXT, home TEXT, away TEXT, start_time TEXT);"
        "CREATE TABLE teams (normalized_name TEXT PRIMARY KEY, "
        "display_name TEXT, last_seen_at TEXT);")
    c.executemany("INSERT INTO events VALUES (?,?,?,?,?,?)", events)
    c.executemany("INSERT INTO teams VALUES (?,?,?)", teams)
    c.commit()
    c.close()
    return p


def test_une_base_saine_est_declaree_saine_sans_pretendre_innocenter(
        tmp_path, capsys, monkeypatch):
    """⚠️ LE SENS DU RÉSULTAT NÉGATIF. `events` ne garde que les matchs
    connus ; un match joué en sort. Zéro nom hostile aujourd'hui ne dit RIEN
    de ce que la base contenait pendant la panne, et la sonde doit le dire au
    lieu de laisser lire « innocent »."""
    p = _base(tmp_path, [("k", "soccer", "Serie A", "Roma", "Lazio", "x")])
    monkeypatch.setattr("sys.argv", ["noms_hostiles", "--base", str(p)])
    main()
    out = capsys.readouterr().out
    assert "Aucune" in out
    assert "pas preuve d'innocence" in out


def test_une_base_contaminee_nomme_les_coupables(tmp_path, capsys, monkeypatch):
    p = _base(
        tmp_path,
        [("k1", "soccer", "Bosnia & Herzegovina", "Sarajevo", "Zrinjski", "x"),
         ("k2", "soccer", "Premier League", "Brighton & Hove", "Leeds", "x")],
        [("bh", "Brighton & Hove", "2026-09-04")])
    monkeypatch.setattr("sys.argv", ["noms_hostiles", "--base", str(p)])
    main()
    out = capsys.readouterr().out
    assert "Bosnia & Herzegovina" in out
    assert "Brighton & Hove" in out
    assert "3 valeur(s) hostiles" in out


def test_une_base_absente_le_dit(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["noms_hostiles", "--base",
                                     str(tmp_path / "rien.db")])
    with pytest.raises(SystemExit) as e:
        main()
    assert "introuvable" in str(e.value)


def test_la_base_est_ouverte_en_lecture_seule(tmp_path, monkeypatch, capsys):
    """Une sonde n'écrit jamais. `mode=ro` le garantit au niveau de SQLite,
    pas seulement par convention."""
    import inspect

    from scripts import noms_hostiles
    assert "mode=ro" in inspect.getsource(noms_hostiles.main)
