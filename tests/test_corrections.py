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


# ── Trajectoire complète — une ligne par CHANGEMENT, tous books ──────────
# Les deux jalons ne donnent que deux instants. Un graphe demande tous les
# points, et pour TOUS les books : le prix d'un seul ne dit pas si c'est lui qui
# a bougé ou le marché entier. Comme 97 à 99 % des cotes sont identiques d'un
# cycle à l'autre, n'écrire que les changements divise le volume par cinquante.

@pytest.fixture(autouse=True)
def _clear_curve_state():
    """`_CURVE_LAST` est un état de module, partagé dans le process."""
    from src.main import _CURVE_LAST
    _CURVE_LAST.clear()
    yield
    _CURVE_LAST.clear()


def _hist(open_rows, quotes, now=T0, fair=None, pin=None):
    return track_corrections(open_rows, quotes, now, fair, pin)[3]


def test_first_observation_always_opens_the_curve():
    """Sans point d'origine, on ne saurait pas d'où le book est parti."""
    h = _hist([_open_row(2.30)], [_quote(2.30)])
    assert len(h) == 1
    owner, book, _ts, odd, _fair, _ev = h[0]
    assert odd == 2.30 and book == "unibet_be"


def test_an_unchanged_odd_writes_nothing():
    """Le cœur de l'économie de volume : 97 à 99 % des cycles sont muets."""
    rows, q = [_open_row(2.30)], [_quote(2.30)]
    assert len(_hist(rows, q)) == 1        # premier passage
    assert _hist(rows, q) == []            # second : rien n'a bougé


def test_every_change_is_recorded():
    rows = [_open_row(2.30)]
    _hist(rows, [_quote(2.30)])
    h = _hist(rows, [_quote(2.25)])
    assert len(h) == 1 and h[0][3] == 2.25


def test_all_books_of_the_selection_are_tracked_not_just_the_detected_one():
    """Le graphe demandé montre TOUS les books. Le suivi est ouvert sur Unibet,
    mais StarCasino et Napoleon pricent la même sélection."""
    h = _hist([_open_row(2.30)], [
        _quote(2.30, book=Book.UNIBET_BE),
        _quote(2.45, book=Book.STARCASINO_SPORT),
        _quote(2.20, book=Book.NAPOLEON_BE),
    ])
    assert {p[1] for p in h} == {"unibet_be", "starcasino_sport", "napoleon_be"}


def test_pinnacle_is_tracked_as_a_series_of_its_own():
    """« Je veux voir aussi la cote de Pinnacle » : elle entre comme les autres,
    avec sa cote AFFICHÉE. `fair_odd` porte à part la même ligne dévigée —
    l'écart entre les deux est la commission, pas un edge."""
    pin = [_quote(2.05, book=Book.PINNACLE)]
    h = _hist([_open_row(2.30)], [_quote(2.30)], pin=pin)
    series = {p[1]: p for p in h}
    assert "pinnacle" in series
    assert series["pinnacle"][3] == 2.05
    assert series["pinnacle"][5] is None, "aucune EV pour la référence elle-même"


def test_one_selection_detected_on_three_books_writes_one_curve():
    """Sans déduplication, chacun des trois suivis relèverait tous les books et
    on écrirait trois copies de la même courbe."""
    rows = []
    for i, bk in enumerate(("unibet_be", "starcasino_sport", "napoleon_be"), 1):
        r = _open_row(2.30, book=bk)
        r["value_bet_id"] = i
        rows.append(r)
    h = _hist(rows, [_quote(2.30, book=Book.UNIBET_BE),
                     _quote(2.45, book=Book.STARCASINO_SPORT)])
    assert {p[0] for p in h} == {1}, "une seule courbe, portée par le plus petit id"
    assert len(h) == 2


def test_a_market_absent_from_the_cycle_writes_nothing():
    """Ne rien voir ne prouve rien : le book peut avoir retiré le marché, ou le
    scraper avoir échoué. Inventer un point ferait mentir la courbe."""
    assert _hist([_open_row(2.30)], []) == []


def test_the_reference_of_the_moment_travels_with_the_point():
    """La ligne juste bouge aussi. Enregistrer celle de la détection ferait
    croire à une convergence du book alors que c'est parfois Pinnacle qui s'est
    déplacé — l'inverse de ce qu'un graphe doit montrer."""
    from src.models import FairLine
    row = _open_row(2.30)
    fair = {(row["event_key"], MarketType(row["market"]), row["line"]):
            FairLine(event_key=row["event_key"], market=MarketType(row["market"]),
                     outcomes={row["outcome_label"]: 0.50})}
    h = _hist([row], [_quote(2.30)], fair=fair)
    assert len(h) == 1
    _owner, _book, _ts, odd, fair_odd, ev = h[0]
    assert fair_odd == pytest.approx(2.00)          # 1 / 0.50
    assert ev == pytest.approx(15.0)                # 2.30 / 2.00 - 1


def test_a_missing_reference_leaves_the_point_but_empties_the_fair():
    """Un point sans référence reste un point de prix valable ; le taire
    trouerait la courbe pour une information annexe."""
    h = _hist([_open_row(2.30)], [_quote(2.30)], fair=None)
    assert len(h) == 1 and h[0][4] is None and h[0][5] is None


def test_the_curve_runs_to_kickoff_not_to_alignment(tmp_path):
    """Un book qui rejoint la ligne juste en dix minutes cessait d'être observé
    pendant les six heures suivantes — or c'est là que le marché se forme."""
    from src.storage import Storage
    st = Storage(tmp_path / "t.db")
    ko = T0 + timedelta(hours=4)
    st.seed_corrections([(1, T0.isoformat(), ko.isoformat(), "unibet_be", EK,
                          "h2h", "home", None, 2.30, 2.10)])
    # Les deux jalons sont franchis : sous l'ancienne règle, le suivi fermait.
    st.update_corrections([], [(T0.isoformat(), 60.0, 2.05, 1)],
                          [(T0.isoformat(), 60.0, 2.05, 1)])
    assert len(st.open_corrections()) == 1, "le suivi doit rester ouvert"


def test_a_tracking_past_its_kickoff_is_closed(tmp_path):
    from src.storage import Storage
    st = Storage(tmp_path / "t.db")
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    st.seed_corrections([(1, datetime.now(timezone.utc).isoformat(),
                          past.isoformat(), "unibet_be", EK,
                          "h2h", "home", None, 2.30, 2.10)])
    assert st.open_corrections() == []


def test_history_survives_a_full_cycle_loop(tmp_path):
    """Bout en bout : une seule écriture par changement, sur deux books."""
    from src.storage import Storage
    st = Storage(tmp_path / "t.db")
    ko = datetime.now(timezone.utc) + timedelta(hours=3)
    st.seed_corrections([(1, datetime.now(timezone.utc).isoformat(),
                          ko.isoformat(), "unibet_be", EK,
                          "h2h", "home", None, 2.30, 2.10)])
    for odd in (2.30, 2.30, 2.25, 2.25, 2.20):
        rows = [dict(r) for r in st.open_corrections()]
        o, c, a, h = track_corrections(
            rows, [_quote(odd), _quote(odd + 0.1, book=Book.STARCASINO_SPORT)],
            datetime.now(timezone.utc),
        )
        st.update_corrections(o, c, a, h)
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "t.db"))
    got = con.execute("SELECT book, odd FROM odds_history ORDER BY rowid").fetchall()
    con.close()
    assert [g[1] for g in got if g[0] == "unibet_be"] == [2.30, 2.25, 2.20]
    assert len([g for g in got if g[0] == "starcasino_sport"]) == 3


def test_closed_curves_are_forgotten_once_the_map_grows():
    """`_CURVE_LAST` ne rétrécissait jamais : à ~2 000 entrées par jour, il
    finirait par peser plus que le service."""
    from src.main import _CURVE_LAST, _forget_closed_curves, _CURVE_LAST_MAX
    _CURVE_LAST.clear()
    for i in range(_CURVE_LAST_MAX + 10):
        _CURVE_LAST[(i, "unibet_be")] = 2.0
    # En dessous du seuil, on ne reconstruit rien pour ne rien supprimer.
    assert _forget_closed_curves({1, 2, 3}) > 0
    assert set(k[0] for k in _CURVE_LAST) == {1, 2, 3}
    _CURVE_LAST.clear()
    _CURVE_LAST[(99, "unibet_be")] = 2.0
    assert _forget_closed_curves(set()) == 0, "sous le seuil : aucun balayage"
    _CURVE_LAST.clear()


def test_the_age_bound_is_configurable_without_deploying(monkeypatch, tmp_path):
    """C'est le premier paramètre à baisser si un cycle s'allonge."""
    from src.storage import Storage
    st = Storage(tmp_path / "t.db")
    old = datetime.now(timezone.utc) - timedelta(hours=100)
    ko = datetime.now(timezone.utc) + timedelta(hours=2)
    st.seed_corrections([(1, old.isoformat(), ko.isoformat(), "unibet_be", EK,
                          "h2h", "home", None, 2.30, 2.10)])
    monkeypatch.setenv("CORRECTIONS_MAX_AGE_HOURS", "168")
    assert len(st.open_corrections()) == 1
    monkeypatch.setenv("CORRECTIONS_MAX_AGE_HOURS", "48")
    assert st.open_corrections() == [], "détecté il y a 100 h, hors borne à 48 h"
