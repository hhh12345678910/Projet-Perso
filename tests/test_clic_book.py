"""Un clic « Jouer » se rattache à la détection du book CLIQUÉ (04/10).

La clé d'un clic (event|marché|pari|ligne) ne porte pas de book. Le clic se
rattachait à la dernière détection de la sélection, chez n'importe quel
book : Vivatbet, qui détecte en continu les mêmes sélections, raflait des
clics joués chez Unibet ou Ladbrokes, et l'Analytics filtré sur Vivatbet
affichait 22 paris « joués » chez lui."""
from __future__ import annotations

import sqlite3

import pytest

from src.alerter import books_du_libelle
from src.storage import Storage

EK = "202610051800::anderlecht__vs__genk"
CLE = f"{EK}|h2h|home|None"


def _vb(c, book, quand, cote=2.1):
    c.execute("INSERT INTO value_bets(event_key,book,market,outcome_label,line,odd_taken,"
              "fair_prob,fair_odd,ev_pct,kelly_pct,detected_at) VALUES "
              "(?,?, 'h2h','home',NULL,?,0.5,2.0,5.0,1.0,?)", (EK, book, cote, quand))
    return c.execute("SELECT last_insert_rowid()").fetchone()[0]


@pytest.fixture
def base(tmp_path):
    db = tmp_path / "t.db"
    Storage(db)
    c = sqlite3.connect(db)
    ids = {"unibet": _vb(c, "unibet_be", "2026-10-04T10:00:00"),
           "vivat": _vb(c, "vivatbet", "2026-10-04T10:05:00", 2.2)}   # la plus récente
    c.commit()
    return db, ids


@pytest.mark.parametrize("libelle, attendu", [
    ("Unibet / 711 / Bingoal / Scooore", ["unibet_be"]),
    ("Ladbrokes", ["ladbrokes_be"]),
    ("Ladbrokes / Unibet / 711 / Bingoal / Scooore", ["ladbrokes_be", "unibet_be"]),
    ("StarCasino", ["starcasino_sport"]),
    ("vivatbet", ["vivatbet"]),
    ("Inconnu", []), ("", []), (None, []),
])
def test_le_libelle_de_l_alerte_donne_le_book(libelle, attendu):
    assert books_du_libelle(libelle) == attendu


def test_le_clic_va_a_la_detection_du_book_clique(base):
    db, ids = base
    st = Storage(db)
    vb = st.latest_value_bet_for(EK, "h2h", "home", None, books=["unibet_be"])
    assert vb["id"] == ids["unibet"]
    # Sans book : la plus récente, comme avant — c'était le bug.
    assert st.latest_value_bet_for(EK, "h2h", "home", None)["id"] == ids["vivat"]
    # Book cliqué sans détection : repli sur la sélection (la CLV reste).
    assert st.latest_value_bet_for(EK, "h2h", "home", None,
                                   books=["ladbrokes_be"])["id"] == ids["vivat"]


def test_le_bouton_jouer_rattache_au_book_de_l_alerte(base, tmp_path, monkeypatch):
    import bot_listener
    db, ids = base
    monkeypatch.setattr(bot_listener, "DB_PATH", db)
    monkeypatch.setattr(bot_listener, "TRACK_PATH", str(tmp_path / "track.csv"))
    bot_listener._record_played({"dedup_key": CLE, "book": "Unibet / 711 / Bingoal / Scooore",
                                 "sport": "soccer", "match": "Anderlecht vs Genk",
                                 "selection": "home", "cote": 2.1, "ev": 5.0})
    c = sqlite3.connect(db)
    assert c.execute("SELECT value_bet_id FROM played_bets").fetchone()[0] == ids["unibet"]


def test_la_reparation_deplace_les_clics_mal_rattaches(base, monkeypatch, capsys):
    from src import main
    db, ids = base
    st = Storage(db)
    # Un clic Unibet enregistré par l'ancien code, rattaché à Vivatbet.
    st.record_played_bet(CLE, __import__("datetime").datetime(2026, 10, 4, 10, 6), 25.0,
                         value_bet=st.latest_value_bet_for(EK, "h2h", "home", None),
                         book="Unibet / 711 / Bingoal / Scooore", odd_taken=2.1, ev_pct=5.0)
    monkeypatch.setattr(main.ScanConfig, "db_path", str(db), raising=False)
    monkeypatch.setattr(main, "ScanConfig", lambda: type("C", (), {"db_path": str(db)})())

    main.relink_played_books(appliquer=False)
    out = capsys.readouterr().out
    assert "vivatbet → unibet_be" in " ".join(out.split()) and "Rien n'est écrit" in out
    assert st.played_bet(CLE)["value_bet_id"] == ids["vivat"], "à blanc : rien ne bouge"

    main.relink_played_books(appliquer=True)
    pb = st.played_bet(CLE)
    assert pb["value_bet_id"] == ids["unibet"]
    assert pb["odd_taken"] == 2.1 and pb["stake"] == 25.0, "le clic garde sa cote et sa mise"

    main.relink_played_books(appliquer=False)
    assert "Tous les clics sont rattachés au book cliqué" in capsys.readouterr().out
