"""Suivi du délai de correction des books.

Cette mesure ne se reconstitue pas après coup : elle demande de suivre une cote
de cycle en cycle, et les cotes sont purgées à deux jours. Une régression ici
ne produit aucune erreur — seulement une table qui reste vide, ou pire, des
délais faux qui paraissent plausibles.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.main import track_corrections
from src.models import Book, MarketType, OddQuote, Outcome
from src.storage import Storage


T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
EK = "202609012000::arsenal__vs__chelsea"


def _open_row(odd_taken=2.30, book="unibet_be", detected=T0, fair_odd=2.10):
    return {"value_bet_id": 1, "detected_at": detected.isoformat(),
            "book": book, "event_key": EK, "market": "h2h",
            "outcome_label": "home", "line": None, "odd_taken": odd_taken,
            "fair_odd": fair_odd, "corrected_at": None, "aligned_at": None}


def _quote(odd, book=Book.UNIBET_BE, label="home", line=None):
    return OddQuote(event_key=EK, book=book, market=MarketType.H2H,
                    outcome=Outcome(label=label, line=line), decimal_odd=odd,
                    fetched_at=T0, source_event_id="x")


def test_correction_recorded_when_the_price_drops_below_what_we_took():
    """La définition est opérationnelle : le prix qu'on avait n'existe plus,
    donc la fenêtre jouable est fermée."""
    obs, corr, algn, _hist = track_corrections([_open_row(2.30)], [_quote(2.20)],
                                  T0 + timedelta(minutes=7))
    assert len(obs) == 1
    assert len(corr) == 1
    ts, secs, odd, vid = corr[0]
    assert abs(secs - 420) < 1e-6      # 7 minutes
    assert odd == 2.20 and vid == 1


def test_no_correction_while_the_price_holds():
    obs, corr, algn, _hist = track_corrections([_open_row(2.30)], [_quote(2.30)],
                                  T0 + timedelta(minutes=7))
    assert len(obs) == 1, "l'observation compte même sans correction"
    assert corr == []


def test_a_longer_price_is_not_a_correction():
    """Le book qui allonge sa cote va dans l'autre sens : la valeur augmente."""
    obs, corr, algn, _hist = track_corrections([_open_row(2.30)], [_quote(2.45)], T0)
    assert corr == []


def test_a_missing_market_is_not_counted_as_uncorrected():
    """Ne rien voir ne prouve rien : le book a pu retirer le marché, ou le
    scraper échouer. Compter ça comme « toujours pas corrigé » ferait passer
    une panne de scraper pour de la lenteur de bookmaker."""
    obs, corr, algn, _hist = track_corrections([_open_row()], [], T0 + timedelta(hours=1))
    assert obs == [] and corr == []


def test_another_book_does_not_close_our_row():
    obs, corr, algn, _hist = track_corrections(
        [_open_row(2.30, book="unibet_be")],
        [_quote(1.90, book=Book.LADBROKES_BE)], T0)
    assert obs == [] and corr == []


def test_another_outcome_does_not_close_our_row():
    obs, corr, algn, _hist = track_corrections([_open_row(2.30)], [_quote(1.50, label="away")], T0)
    assert obs == [] and corr == []


def test_lowest_quote_of_the_cycle_wins():
    """Plusieurs cotes pour la même clé dans un cycle : c'est la plus basse qui
    décide, puisque c'est elle qui dit si le prix a disparu."""
    obs, corr, algn, _hist = track_corrections([_open_row(2.30)],
                                  [_quote(2.35), _quote(2.10)], T0)
    assert len(corr) == 1 and corr[0][2] == 2.10


def test_seeding_twice_does_not_restart_the_clock(tmp_path):
    """Une détection re-signalée au cycle suivant garderait sinon un délai
    toujours nul, et tous les books paraîtraient instantanés."""
    import sqlite3
    db = str(tmp_path / "v.db")
    s = Storage(db)
    row = (1, T0.isoformat(), None, "unibet_be", EK, "h2h", "home", None, 2.30, 2.10)
    s.seed_corrections([row])
    s.update_corrections([], [((T0 + timedelta(minutes=5)).isoformat(), 300.0, 2.2, 1)])
    later = (1, (T0 + timedelta(minutes=30)).isoformat(), None, "unibet_be",
             EK, "h2h", "home", None, 2.30, 2.10)
    s.seed_corrections([later])
    c = sqlite3.connect(db)
    got = c.execute("SELECT detected_at, seconds_to_corr FROM bet_corrections").fetchone()
    assert got[0] == T0.isoformat(), "la date de détection ne doit pas être écrasée"
    assert got[1] == 300.0, "la correction déjà mesurée ne doit pas être perdue"


def test_a_closed_window_keeps_being_watched_until_alignment(tmp_path):
    """Fermer la fenêtre ne clôt pas le suivi : passer de 2,30 à 2,29 ne dit
    rien de la convergence, et c'est justement ce trajet-là qu'on veut voir."""
    db = str(tmp_path / "v.db")
    s = Storage(db)
    now = datetime.now(timezone.utc)
    s.seed_corrections([
        (1, now.isoformat(), None, "unibet_be", EK, "h2h", "home", None, 2.3, 2.1),
        (2, now.isoformat(), None, "ladbrokes_be", EK, "h2h", "away", None, 3.1, 2.9),
    ])
    assert len(s.open_corrections()) == 2
    s.update_corrections([], [(now.isoformat(), 60.0, 2.2, 1)])
    assert {int(r["value_bet_id"]) for r in s.open_corrections()} == {1, 2}
    # Les deux jalons franchis : le suivi sort enfin.
    s.update_corrections([], [], [(now.isoformat(), 900.0, 2.05, 1)])
    assert [int(r["value_bet_id"]) for r in s.open_corrections()] == [2]


def test_alignment_needs_the_fair_line_not_just_the_taken_price():
    """Le second jalon est bien plus exigeant : à 2,20 la fenêtre est fermée
    (on avait 2,30) mais la ligne juste, à 2,10, n'est pas atteinte."""
    obs, corr, algn, _hist = track_corrections(
        [_open_row(odd_taken=2.30, fair_odd=2.10)], [_quote(2.20)],
        T0 + timedelta(minutes=3))
    assert len(corr) == 1, "la fenêtre doit être fermée"
    assert algn == [], "mais l'alignement n'est pas atteint"


def test_alignment_recorded_when_the_fair_line_is_reached():
    obs, corr, algn, _hist = track_corrections(
        [_open_row(odd_taken=2.30, fair_odd=2.10)], [_quote(2.05)],
        T0 + timedelta(minutes=40))
    assert len(algn) == 1
    _ts, secs, odd, vid = algn[0]
    assert abs(secs - 2400) < 1e-6 and odd == 2.05 and vid == 1


def test_a_milestone_already_crossed_is_not_recorded_twice():
    """Sans ça, chaque cycle suivant verrait la condition encore vraie et
    repousserait l'instant du franchissement indéfiniment — le délai mesuré
    finirait par valoir l'âge du pari."""
    row = _open_row(odd_taken=2.30, fair_odd=2.10)
    row["corrected_at"] = T0.isoformat()
    row["aligned_at"] = T0.isoformat()
    obs, corr, algn, _hist = track_corrections([row], [_quote(1.90)], T0 + timedelta(hours=2))
    assert obs and corr == [] and algn == []


def test_alignment_is_skipped_when_the_fair_line_is_unknown():
    """Les suivis créés avant l'ajout de la colonne n'ont pas de fair_odd :
    ils ne doivent pas s'aligner par accident sur une valeur absente."""
    obs, corr, algn, _hist = track_corrections(
        [_open_row(odd_taken=2.30, fair_odd=None)], [_quote(1.10)], T0)
    assert len(corr) == 1 and algn == []


def test_observations_accumulate_and_track_the_lowest_price(tmp_path):
    """`observations` et `min_odd_seen` disent qu'on a vraiment regardé, et
    jusqu'où le prix est descendu sans franchir le seuil."""
    import sqlite3
    db = str(tmp_path / "v.db")
    s = Storage(db)
    now = datetime.now(timezone.utc)
    s.seed_corrections([(1, now.isoformat(), None, "unibet_be", EK, "h2h",
                         "home", None, 2.30, 2.10)])
    s.update_corrections([(now.isoformat(), 2.28, 1)], [])
    s.update_corrections([(now.isoformat(), 2.25, 1)], [])
    c = sqlite3.connect(db)
    obs, low = c.execute(
        "SELECT observations, min_odd_seen FROM bet_corrections").fetchone()
    assert obs == 2
    assert abs(low - 2.25) < 1e-9


# ── Trajectoire complète — une ligne par CHANGEMENT ───────────────────────
# Les deux jalons ne donnent que deux instants. Un graphe demande tous les
# points. Comme 97 à 99 % des cotes sont identiques d'un cycle à l'autre,
# n'écrire que les changements divise le volume par cinquante — et la série
# se reconstruit en propageant la dernière valeur connue.

def _hist(open_rows, quotes, now=T0, fair=None):
    return track_corrections(open_rows, quotes, now, fair)[3]


def test_first_observation_always_opens_the_curve():
    """Sans point d'origine, on ne saurait pas d'où le book est parti."""
    h = _hist([_open_row(2.30)], [_quote(2.30)])
    assert len(h) == 1 and h[0][2] == 2.30


def test_an_unchanged_odd_writes_nothing():
    """Le cœur de l'économie de volume : 97 à 99 % des cycles sont muets."""
    row = _open_row(2.30)
    row["last_odd_seen"] = 2.30
    assert _hist([row], [_quote(2.30)]) == []


def test_every_change_is_recorded():
    row = _open_row(2.30)
    row["last_odd_seen"] = 2.30
    h = _hist([row], [_quote(2.25)])
    assert len(h) == 1
    vid, ts, odd, fair, ev = h[0]
    assert odd == 2.25 and vid == int(row["value_bet_id"])


def test_a_market_absent_from_the_cycle_writes_nothing():
    """Ne rien voir ne prouve rien : le book peut avoir retiré le marché, ou le
    scraper avoir échoué. Inventer un point ferait mentir la courbe."""
    assert _hist([_open_row(2.30)], []) == []


def test_the_reference_of_the_moment_travels_with_the_point():
    """La ligne juste bouge aussi. Enregistrer celle de la détection ferait
    croire à une convergence du book alors que c'est parfois Pinnacle qui s'est
    déplacé — l'inverse de ce qu'un graphe doit montrer."""
    from src.models import FairLine, MarketType
    row = _open_row(2.30)
    fair = {(row["event_key"], MarketType(row["market"]), row["line"]):
            FairLine(event_key=row["event_key"], market=MarketType(row["market"]),
                     outcomes={row["outcome_label"]: 0.50})}
    h = _hist([row], [_quote(2.30)], fair=fair)
    assert len(h) == 1
    _vid, _ts, odd, fair_odd, ev = h[0]
    assert fair_odd == pytest.approx(2.00)          # 1 / 0.50
    assert ev == pytest.approx(15.0)                # 2.30 / 2.00 - 1


def test_a_missing_reference_leaves_the_point_but_empties_the_fair():
    """Un point sans référence reste un point de prix valable ; le taire
    trouerait la courbe pour une information annexe."""
    h = _hist([_open_row(2.30)], [_quote(2.30)], fair=None)
    assert len(h) == 1 and h[0][3] is None and h[0][4] is None


def test_history_and_last_odd_seen_stay_consistent(tmp_path):
    """Bout en bout : deux cycles, une seule écriture par changement, et
    last_odd_seen suit — sinon le cycle suivant réécrirait le même point."""
    from src.storage import Storage
    st = Storage(tmp_path / "t.db")
    st.seed_corrections([(1, T0.isoformat(), None, "unibet_be", EK,
                          "h2h", "home", None, 2.30, 2.10)])
    for odd in (2.30, 2.30, 2.25, 2.25, 2.20):
        rows = [dict(r) for r in st.open_corrections()]
        o, c, a, h = track_corrections(rows, [_quote(odd)], T0)
        st.update_corrections(o, c, a, h)
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "t.db"))
    pts = con.execute("SELECT odd FROM odds_history WHERE value_bet_id=1 "
                      "ORDER BY rowid").fetchall()
    con.close()
    assert [p[0] for p in pts] == [2.30, 2.25, 2.20], "un point par changement"
