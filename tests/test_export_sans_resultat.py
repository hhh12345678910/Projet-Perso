"""scripts/export_sans_resultat : tous les matchs détectés sans résultat."""
import csv
import sqlite3
from datetime import date, datetime, timezone

from scripts import export_sans_resultat as ex
from src.storage import Storage

MAINTENANT = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _base(tmp_path, events, results=(), joues=(), sans_detection=()):
    db = tmp_path / "v.db"
    Storage(str(db))
    c = sqlite3.connect(db)
    c.executemany("INSERT INTO events VALUES (?,?,?,?,?,?)", events)
    for k, *_ in events:
        if k in sans_detection:
            continue
        c.execute(
            "INSERT INTO value_bets(event_key, book, market, outcome_label, "
            "odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct, detected_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (k, "unibet_be", "h2h", "home", 2.0, 0.5, 2.0, 6.0, 1.0, "2026-10-01"))
    for k in results:
        c.execute("INSERT INTO results VALUES (?,?,?,?,?,?)",
                  (k, "home", 1, 0, "test", "2026-10-02"))
    for i, k in enumerate(joues):
        c.execute("INSERT INTO played_bets(dedup_key, event_key) VALUES (?,?)",
                  (f"d{i}", k))
    c.commit()
    c.close()
    return str(db)


def _ev(k, sport, ligue, h, a, t):
    return (k, sport, ligue, h, a, t)


def test_un_match_sans_resultat_est_liste_avec_ligue_heure_et_sport(tmp_path):
    db = _base(tmp_path, [_ev("202610011500::a__vs__b", "soccer", "Ligue 1",
                              "Team A", "Team B", "2026-10-01T15:00:00+00:00")])
    m = ex.matchs_sans_resultat(db, MAINTENANT)
    assert [(g["sport"], g["ligue"], g["domicile"], g["exterieur"]) for g in m] == [
        ("soccer", "Ligue 1", "Team A", "Team B")]
    assert m[0]["quand"] == datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)


def test_un_match_regle_n_est_pas_liste(tmp_path):
    k = "202610011500::a__vs__b"
    db = _base(tmp_path, [_ev(k, "soccer", "L", "A", "B", "2026-10-01T15:00:00+00:00")],
               results=[k])
    assert ex.matchs_sans_resultat(db, MAINTENANT) == []


def test_une_autre_cle_reglee_du_meme_match_l_ecarte(tmp_path):
    db = _base(tmp_path, [
        _ev("202610021500::c__vs__d", "soccer", "L", "C", "D", "2026-10-02T15:00:00+00:00"),
        _ev("202610021600::c__vs__d", "soccer", "L", "C", "D", "2026-10-02T16:00:00+00:00"),
    ], results=["202610021600::c__vs__d"])
    assert ex.matchs_sans_resultat(db, MAINTENANT) == []


def test_deux_cles_sans_resultat_font_une_seule_ligne(tmp_path):
    db = _base(tmp_path, [
        _ev("202610031200::x__vs__y", "tennis", "ATP", "X", "Y", "2026-10-03T12:00:00+00:00"),
        _ev("202610031300::y__vs__x", "tennis", "ATP", "Y", "X", "2026-10-03T13:00:00+00:00"),
    ])
    m = ex.matchs_sans_resultat(db, MAINTENANT)
    assert len(m) == 1
    assert m[0]["detections"] == 2
    assert sorted(m[0]["cles"]) == ["202610031200::x__vs__y", "202610031300::y__vs__x"]


def test_les_paris_joues_sont_comptes_mais_pas_exiges(tmp_path):
    db = _base(tmp_path, [
        _ev("202610041200::p__vs__q", "hockey", "NHL", "P", "Q", "2026-10-04T12:00:00+00:00"),
        _ev("202610041500::r__vs__s", "hockey", "NHL", "R", "S", "2026-10-04T15:00:00+00:00"),
    ], joues=["202610041200::p__vs__q"])
    m = ex.matchs_sans_resultat(db, MAINTENANT)
    assert [(g["domicile"], g["paris_joues"]) for g in m] == [("P", 1), ("R", 0)]


def test_ni_match_sans_detection_ni_match_pas_fini(tmp_path):
    db = _base(tmp_path, [
        _ev("202610041300::n__vs__z", "soccer", "L", "N", "Z", "2026-10-04T13:00:00+00:00"),
        _ev("202610081000::f__vs__u", "soccer", "L", "F", "U", "2026-10-08T10:00:00+00:00"),
    ], sans_detection=["202610041300::n__vs__z"])
    assert ex.matchs_sans_resultat(db, MAINTENANT) == []


def test_depuis_borne_le_debut(tmp_path):
    db = _base(tmp_path, [
        _ev("202607011500::a__vs__b", "soccer", "L", "A", "B", "2026-07-01T15:00:00+00:00"),
        _ev("202609011500::c__vs__d", "soccer", "L", "C", "D", "2026-09-01T15:00:00+00:00"),
    ])
    m = ex.matchs_sans_resultat(db, MAINTENANT, depuis=date(2026, 8, 1))
    assert [g["domicile"] for g in m] == ["C"]


def test_le_csv_porte_les_colonnes_annoncees(tmp_path):
    db = _base(tmp_path, [_ev("202610011500::a__vs__b", "soccer", "Ligue 1",
                              "Team A", "Team B", "2026-10-01T15:00:00+00:00")])
    sortie = tmp_path / "out.csv"
    ex.ecrire(ex.matchs_sans_resultat(db, MAINTENANT), sortie)
    with open(sortie, encoding="utf-8") as f:
        lignes = list(csv.DictReader(f))
    assert list(lignes[0]) == ex.COLONNES
    assert lignes[0]["date_utc"] == "2026-10-01 15:00"
    assert lignes[0]["ligue"] == "Ligue 1"
