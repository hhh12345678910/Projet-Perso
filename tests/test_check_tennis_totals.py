"""La sonde des totaux de jeux du tennis doit distinguer les deux regimes.

L'enjeu est le §21.2 : avant le correctif, Pinnacle ne rendait qu'une ligne de
SETS (2,5) en face des 15-25 JEUX des books, et rien ne le signalait. Une sonde
qui rendrait le meme verdict dans les deux cas ne servirait a rien — c'est le
piege du §11, applique a l'outil de diagnostic lui-meme.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts import check_tennis_totals as ctt

_DDL_START = "CREATE TABLE IF NOT EXISTS events"
_DDL_END = "CREATE TABLE IF NOT EXISTS clv_snapshots"

_INSERT_QUOTE = (
    "INSERT INTO quotes(event_key, book, market, outcome_label, line, "
    "decimal_odd, fetched_at, source_event_id) "
    "VALUES (?, ?, ?, ?, ?, ?, datetime('now', '-10 minutes'), 'x')"
)
_INSERT_VB = (
    "INSERT INTO value_bets(event_key, book, market, outcome_label, line, "
    "odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct, detected_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '-3 hours'))"
)


def _schema() -> str:
    """Le vrai DDL de production, jamais une copie.

    §19.8 : un extracteur duplique donne tort au bon code. Une copie du schema
    laisserait ce test vert pendant qu'une colonne renommee casse la sonde.
    """
    src = Path("src/storage.py").read_text()
    return src[src.index(_DDL_START):src.index(_DDL_END)]


def _migrate(conn: sqlite3.Connection) -> None:
    """Appliquer les MIGRATIONS de production, pas seulement le DDL de base.

    `reference_book` et consorts n'existent que la : une base de test batie sur
    le seul SCHEMA n'a pas la forme de celle de la VM, et la sonde y echouerait
    sur une colonne que la production a pourtant. Meme motif qu'au §19.8 —
    on importe la definition, on ne la recopie pas.
    """
    from src.storage import MIGRATIONS

    for table, column, coltype in MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        except sqlite3.OperationalError:
            pass  # deja presente


def _build(
    path: Path,
    pinnacle_lines: list[float],
    detections: int,
    det_line: float = 20.5,
) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(_schema())
    _migrate(conn)
    for i in range(3):
        conn.execute(
            "INSERT INTO events VALUES (?, 'tennis', 'ATP', ?, ?, ?)",
            (f"ev{i}", f"J{i}A", f"J{i}B", "2026-08-20T18:00:00"),
        )
    for i in range(3):
        for line in pinnacle_lines:
            for side in ("over", "under"):
                conn.execute(
                    _INSERT_QUOTE, (f"ev{i}", "pinnacle", "totals", side, line, 1.9)
                )
        for line in (19.5, 20.5, 21.5):
            conn.execute(
                _INSERT_QUOTE, (f"ev{i}", "unibet_be", "totals", "over", line, 1.95)
            )
    for i in range(detections):
        conn.execute(
            _INSERT_VB,
            (f"ev{i % 3}", "unibet_be", "totals", "over", det_line, 2.0, 0.55,
             1.82, 8.5, 1.0),
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def before(tmp_path: Path) -> Path:
    """Pinnacle sur la seule ligne de sets — l'etat d'avant le §21.2."""
    return _build(tmp_path / "before.db", [2.5], detections=0)


@pytest.fixture
def after(tmp_path: Path) -> Path:
    """Pinnacle publie aussi les lignes de jeux, et ca detecte."""
    return _build(tmp_path / "after.db", [2.5, 19.5, 20.5, 21.5], detections=5)


def test_the_probe_fails_when_pinnacle_only_prices_sets(before, capsys):
    assert ctt.main(["", str(before)]) == 0
    out = capsys.readouterr().out
    assert "ECHEC" in out
    assert "n'est PAS en service" in out
    assert "reprendre la chasse" in out


def test_the_probe_passes_once_games_lines_are_collected(after, capsys):
    assert ctt.main(["", str(after)]) == 0
    out = capsys.readouterr().out
    assert "OK : Pinnacle publie" in out
    assert "ECHEC" not in out
    assert "conforme" in out


def test_a_missing_database_is_reported_not_crashed(tmp_path, capsys):
    assert ctt.main(["", str(tmp_path / "nowhere.db")]) == 2
    assert "introuvable" in capsys.readouterr().err


def test_overlap_counts_only_games_lines(after):
    """Le recouvrement ne doit jamais compter la ligne de sets.

    Les deux familles cohabitent sous la MEME event_key (§21.3) : compter 2,5
    ferait passer pour couvert un match ou seuls les sets sont prices, et la
    sonde annoncerait un gisement qui n'existe pas.
    """
    conn = ctt._connect(after)
    try:
        ref, with_book = ctt._overlap(conn, hours=1)
    finally:
        conn.close()
    assert (ref, with_book) == (3, 3)


def test_a_sets_only_database_shows_no_overlap(before):
    conn = ctt._connect(before)
    try:
        assert ctt._overlap(conn, hours=1) == (0, 0)
    finally:
        conn.close()


def test_the_probe_never_writes(after):
    """Ouverture en lecture seule : le daemon peut tourner pendant."""
    conn = ctt._connect(after)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM quotes")
    finally:
        conn.close()


# --- Volet D : l'audit d'echelle ------------------------------------------
#
# Le volet C ne sait que compter. Un comptage au-dessus du predit a deux
# lectures opposees — un gisement reel, ou la panne §21.3 qui fabrique des
# paris de valeur inventes — et le §11 dit laquelle coute cher. D les separe.


@pytest.mark.parametrize(
    "line, expected",
    [
        (2.5, "sets"),    # Bo3
        (3.5, "sets"),    # Bo5
        (4.5, "sets"),    # borne haute des sets
        (9.5, "AMBIGU"),  # jeux d'UN set : la fuite du §21.3
        (13.5, "AMBIGU"), # un set va jusqu'a 13 jeux, tie-break compris
        (14.5, "jeux"),   # plus court total de match observe (unibet_be)
        (20.5, "jeux"),
        (27.0, "jeux"),
    ],
)
def test_scale_separates_sets_from_games_and_flags_the_gap(line, expected):
    """Entre 4,5 et 14,5, une ligne de totaux n'a aucun sens legitime.

    C'est ce trou qui rend le diagnostic possible : un total de jeux par set
    (6 a 13) ne peut pas se deguiser en total de match sans y tomber.
    """
    assert ctt._scale(line) == expected


def test_section_d_denounces_a_per_set_games_line(tmp_path, capsys):
    """La panne §21.3, telle qu'elle se presenterait : une ligne a 9,5.

    Sans ce test, la sonde dirait « nettement au-dessus du predit » et
    laisserait l'operateur conclure a une bonne nouvelle.
    """
    db = _build(tmp_path / "leak.db", [2.5, 9.5, 20.5], detections=6, det_line=9.5)
    assert ctt.main(["", str(db)]) == 0
    out = capsys.readouterr().out
    assert "SUSPECT" in out
    assert "§21.3" in out
    assert "Ne pas jouer" in out


def test_section_d_clears_detections_that_stay_on_the_games_scale(after, capsys):
    assert ctt.main(["", str(after)]) == 0
    out = capsys.readouterr().out
    assert "OK : aucune ligne dans la bande" in out
    assert "SUSPECT" not in out


def test_section_d_reports_concentration(after, capsys):
    """Cinq detections sur trois matchs, ce n'est pas cinq matchs mal prices.

    Un match mal price rend une detection par book et par ligne. Sans cette
    ligne, un comptage groupe se lit comme un gisement large.
    """
    assert ctt.main(["", str(after)]) == 0
    assert "detections : 5 sur 3 match(s) distinct(s)" in capsys.readouterr().out


def test_section_d_is_silent_without_detections(before, capsys):
    assert ctt.main(["", str(before)]) == 0
    assert "Audit d'echelle" not in capsys.readouterr().out


def test_poisson_tail_frames_the_prediction_as_a_mean():
    """4-5 par jour est une moyenne. 10 doit rester improbable sans etre exclu."""
    assert ctt._poisson_tail(0, 4.5) == pytest.approx(100.0)
    assert 1.0 < ctt._poisson_tail(10, 4.5) < 5.0
    assert ctt._poisson_tail(5, 4.5) > ctt._poisson_tail(10, 4.5)


def test_section_d_flags_a_one_sided_reference(tmp_path, capsys):
    """Un devig a un seul cote ne peut pas donner une ligne juste."""
    db = _build(tmp_path / "onesided.db", [2.5], detections=2, det_line=20.5)
    conn = sqlite3.connect(db)
    conn.execute(_INSERT_QUOTE, ("ev0", "pinnacle", "totals", "over", 20.5, 1.9))
    conn.commit()
    conn.close()
    assert ctt.main(["", str(db)]) == 0
    assert "REFERENCE A UN SEUL COTE" in capsys.readouterr().out
