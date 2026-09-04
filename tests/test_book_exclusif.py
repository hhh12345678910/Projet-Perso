"""Couper un book : la sonde chiffre-t-elle la bonne perte ?

LE PIÈGE QU'ELLE DOIT ÉVITER
----------------------------
« Combien de paris viennent de ce book » est la mauvaise question. Un pari que
trois autres books proposent aussi ne disparaît pas quand on coupe celui-ci —
il se rabat sur le meilleur des autres, à quelques centièmes de cote près.
Compter tout son volume comme une perte ferait renoncer à une coupure gratuite.

L'inverse est pire : compter une opportunité comme remplaçable quand personne
d'autre ne la propose ferait couper un book irremplaçable. Ces tests plantent
les deux, avec des chiffres calculés à la main.
"""
from __future__ import annotations

import sqlite3

import pytest

from scripts.book_exclusif import _cle, _clv, _resume, main


def _base(tmp_path, lignes):
    """`lignes` : (id, book, home, away, jour, marche, pari, cote, ev, clot,
    detecte_a)."""
    p = tmp_path / "v.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
        CREATE TABLE value_bets (id INTEGER PRIMARY KEY, event_key TEXT,
            book TEXT, market TEXT, outcome_label TEXT, line REAL,
            odd_taken REAL, ev_pct REAL, detected_at TEXT);
        CREATE TABLE clv_snapshots (id INTEGER PRIMARY KEY, value_bet_id INT,
            closing INT, fair_odd REAL);
        CREATE TABLE events (event_key TEXT PRIMARY KEY, sport TEXT,
            league TEXT, home TEXT, away TEXT, start_time TEXT);
    """)
    vus = set()
    for (i, book, home, away, jour, marche, pari, cote, ev, clot, det) in lignes:
        ek = f"{jour.replace('-', '')}1200::{home}__vs__{away}"
        if ek not in vus:
            c.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                      (ek, "soccer", "L1", home, away, f"{jour}T12:00:00+00:00"))
            vus.add(ek)
        c.execute("INSERT INTO value_bets VALUES (?,?,?,?,?,?,?,?,?)",
                  (i, ek, book, marche, pari, None, cote, ev, det))
        if clot is not None:
            c.execute("INSERT INTO clv_snapshots VALUES (?,?,?,?)",
                      (i, i, 1, clot))
    c.commit()
    c.close()
    return p


T = "2026-09-01T10:00:00+00:00"


def test_la_cle_ignore_l_event_key():
    """§17.8 : le tennis produit jusqu'à onze `event_key` pour un match.
    Grouper dessus éclaterait la même opportunité en onze."""
    r = {"home": "Roma", "away": "Lazio", "start_time": "2026-09-01T12:00:00",
         "market": "h2h", "outcome_label": "home", "line": None}
    assert _cle(r) == ("roma", "lazio", "2026-09-01", "h2h", "home", None)


def test_la_clv_absente_est_none_jamais_zero():
    assert _clv({"closing_fair_odd": None, "odd_taken": 2.0}) is None
    assert _clv({"closing_fair_odd": 0.0, "odd_taken": 2.0}) is None


def test_la_clv_est_calculee_contre_la_cloture_devigee():
    # 2.10 pris, clôture juste à 2.00 → +5 %.
    v = _clv({"closing_fair_odd": 2.0, "odd_taken": 2.1})
    assert v == pytest.approx(5.0, abs=0.01)


def test_resume_ne_compte_que_les_lignes_avec_cloture():
    rows = [{"closing_fair_odd": 2.0, "odd_taken": 2.1, "ev_pct": 5.0},
            {"closing_fair_odd": None, "odd_taken": 2.2, "ev_pct": 6.0}]
    d = _resume(rows)
    assert d["n"] == 2 and d["n_clv"] == 1


def test_un_book_seul_sur_tous_ses_paris_est_declare_irremplacable(
        tmp_path, capsys, monkeypatch):
    lignes = [(i, "elitesports", f"A{i}", f"B{i}", "2026-09-01", "h2h",
               "home", 2.10, 5.0, 2.00, T) for i in range(1, 6)]
    p = _base(tmp_path, lignes)
    monkeypatch.setattr("sys.argv", ["book_exclusif", "--db", str(p)])
    main()
    out = capsys.readouterr().out
    assert "5 opportunités DISPARAÎTRAIENT (100 %" in out
    assert "aucune n'est proposée ailleurs : TOUT disparaîtrait" in out


def test_un_book_qui_ne_fait_que_doubler_est_declare_gratuit(
        tmp_path, capsys, monkeypatch):
    """Chaque pari existe aussi chez Unibet À LA MÊME COTE : la coupure ne
    coûte rien du tout, et le tableau doit le dire."""
    lignes = []
    for i in range(1, 6):
        lignes.append((i, "elitesports", f"A{i}", f"B{i}", "2026-09-01",
                       "h2h", "home", 2.10, 5.0, 2.00, T))
        lignes.append((100 + i, "unibet_be", f"A{i}", f"B{i}", "2026-09-01",
                       "h2h", "home", 2.10, 5.0, 2.00, T))
    p = _base(tmp_path, lignes)
    monkeypatch.setattr("sys.argv", ["book_exclusif", "--db", str(p)])
    main()
    out = capsys.readouterr().out
    assert "0 opportunités DISPARAÎTRAIENT (0 %" in out
    assert "5 seraient rabattues" in out
    assert "cote +0.00 %" in out


def test_la_perte_de_cote_est_chiffree_pas_supposee(tmp_path, capsys,
                                                    monkeypatch):
    """EliteSports à 2,10 et Unibet à 2,00 : la coupure coûte −4,76 % de cote.
    Compter ces paris comme perdus (au lieu de rabattus) surestimerait la
    perte d'un facteur énorme."""
    lignes = []
    for i in range(1, 5):
        lignes.append((i, "elitesports", f"A{i}", f"B{i}", "2026-09-01",
                       "h2h", "home", 2.10, 5.0, 2.00, T))
        lignes.append((100 + i, "unibet_be", f"A{i}", f"B{i}", "2026-09-01",
                       "h2h", "home", 2.00, 0.0, 2.00, T))
    p = _base(tmp_path, lignes)
    monkeypatch.setattr("sys.argv", ["book_exclusif", "--db", str(p)])
    main()
    out = capsys.readouterr().out
    assert "cote -4.76 %" in out
    # CLV +5,00 % chez EliteSports → 0,00 % chez Unibet : −5 points.
    assert "+5.00 % → +0.00 % (-5.00 point(s))" in out


def test_une_redetection_ne_compte_pas_deux_fois(tmp_path, capsys, monkeypatch):
    """Le daemon revoit le même pari à chaque cycle. Trois lignes du même book
    sur la même opportunité restent UNE opportunité — et c'est la meilleure
    cote qui la représente."""
    lignes = [(i, "elitesports", "A", "B", "2026-09-01", "h2h", "home",
               2.00 + i / 100, 5.0, 2.00, T) for i in range(1, 4)]
    p = _base(tmp_path, lignes)
    monkeypatch.setattr("sys.argv", ["book_exclusif", "--db", str(p)])
    main()
    out = capsys.readouterr().out
    assert "1 opportunités où il est présent" in out
    # 2,03 contre 2,00 → +1,50 %.
    assert "+1.50 %" in out


def test_le_compagnon_a_sa_propre_ligne(tmp_path, capsys, monkeypatch):
    lignes = [
        (1, "elitesports", "A", "B", "2026-09-01", "h2h", "home", 2.10, 5.0, 2.00, T),
        (2, "unibet_be", "A", "B", "2026-09-01", "h2h", "home", 2.05, 2.5, 2.00, T),
        (3, "elitesports", "C", "D", "2026-09-01", "h2h", "home", 2.20, 10.0, 2.00, T),
        (4, "circus_be", "C", "D", "2026-09-01", "h2h", "home", 2.15, 7.5, 2.00, T),
    ]
    p = _base(tmp_path, lignes)
    monkeypatch.setattr("sys.argv", ["book_exclusif", "--db", str(p)])
    main()
    out = capsys.readouterr().out
    assert "avec unibet_be" in out
    assert "avec un autre book, quel qu'il soit" in out
    # La ligne « avec unibet_be » ne porte QUE la première opportunité.
    ligne = [l for l in out.splitlines() if "avec unibet_be" in l][0]
    assert ligne.split()[2] == "1", ligne


def test_l_ecart_temporel_est_avoue(tmp_path, capsys, monkeypatch):
    """Deux détections du même jour à six heures d'écart comptent comme la
    même opportunité — l'erreur FLATTE la coupure, donc elle doit être dite."""
    lignes = [
        (1, "elitesports", "A", "B", "2026-09-01", "h2h", "home", 2.10, 5.0,
         2.00, "2026-09-01T04:00:00+00:00"),
        (2, "unibet_be", "A", "B", "2026-09-01", "h2h", "home", 2.05, 2.5,
         2.00, "2026-09-01T10:00:00+00:00"),
    ]
    p = _base(tmp_path, lignes)
    monkeypatch.setattr("sys.argv", ["book_exclusif", "--db", str(p)])
    main()
    out = capsys.readouterr().out
    assert "1 des 1 « accompagnées » (100 %)" in out
    assert "FLATTE" in out


def test_un_book_absent_le_dit_et_liste_ceux_qui_existent(tmp_path, monkeypatch):
    p = _base(tmp_path, [(1, "unibet_be", "A", "B", "2026-09-01", "h2h",
                          "home", 2.0, 3.0, 2.0, T)])
    monkeypatch.setattr("sys.argv", ["book_exclusif", "--db", str(p),
                                     "--book", "elitesports"])
    with pytest.raises(SystemExit) as e:
        main()
    assert "unibet_be" in str(e.value)


def test_sans_cloture_la_valeur_est_inconnue_pas_nulle(tmp_path, capsys,
                                                       monkeypatch):
    lignes = [(i, "elitesports", f"A{i}", f"B{i}", "2026-09-01", "h2h",
               "home", 2.10, 5.0, None, T) for i in range(1, 4)]
    p = _base(tmp_path, lignes)
    monkeypatch.setattr("sys.argv", ["book_exclusif", "--db", str(p)])
    main()
    out = capsys.readouterr().out
    assert "INCONNUE, pas nulle" in out
