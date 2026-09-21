"""La sonde de survie des prix : ce qu'elle compte, et ce qu'elle refuse de compter.

⚠️ UNE SONDE QUI SE TROMPE EST PIRE QU'AUCUNE SONDE. Celle-ci décide s'il vaut
la peine d'accélérer le daemon : un taux de survie surestimé ferait engager des
modifications inutiles sur des fichiers protégés, un taux sous-estimé ferait
abandonner un gain réel. Les deux erreurs coûtent, dans les deux sens.
"""
from __future__ import annotations

import sqlite3

import pytest

from scripts.survie_cote import PALIERS, mesurer
from src.storage import Storage


def _base(tmp_path, paris, il_y_a_jours: float = 0.0):
    """`paris` : liste de (id, book, odd_annoncee, [(secondes, cote), …]).

    ⚠️ LES DATES SONT RELATIVES À MAINTENANT, jamais fixes. Une date en dur
    tombe d'un côté ou de l'autre de `datetime('now', '-N days')` selon
    l'heure à laquelle la suite tourne : le test passerait le matin et
    échouerait le soir."""
    chemin = tmp_path / "v.db"
    Storage(str(chemin))
    con = sqlite3.connect(str(chemin))
    jour = con.execute(
        "SELECT datetime('now', ?)", (f"-{il_y_a_jours} days",)).fetchone()[0]
    jour = jour.replace(" ", "T") + "+00:00"
    for ident, book, odd, trajectoire in paris:
        con.execute(
            "INSERT INTO value_bets (id, event_key, book, market, outcome_label,"
            " odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct, detected_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ident, f"E{ident}", book, "h2h", "home", odd, 0.5, 2.0, 5.0, 1.0, jour))
        for secondes, cote in trajectoire:
            t = con.execute("SELECT datetime(?, ?)",
                            (jour, f"+{secondes} seconds")).fetchone()[0]
            con.execute("INSERT INTO odds_history VALUES (?,?,?,?,?,?)",
                        (ident, book, t.replace(" ", "T") + "+00:00",
                         cote, 2.0, 0.0))
    con.commit()
    con.close()
    return sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)


def test_la_survie_est_le_delai_jusqua_la_PREMIERE_baisse(tmp_path):
    con = _base(tmp_path, [
        (1, "ladbrokes_be", 2.10, [(0, 2.10), (20, 2.00), (40, 1.90)]),
    ])
    r = mesurer(con, "ladbrokes_be", 7)
    assert r["ont_baisse"] == 1
    assert r["paliers"][15] == 0, "comptée trop tôt"
    assert r["paliers"][30] == 1, "la première baisse est à 20 s"


def test_une_HAUSSE_nest_pas_une_perte(tmp_path):
    """⚠️ Une cote qui MONTE est une meilleure affaire, pas un prix perdu.
    La compter comme une baisse ferait conclure que les prix meurent alors
    qu'ils s'améliorent."""
    con = _base(tmp_path, [
        (1, "ladbrokes_be", 2.10, [(0, 2.10), (10, 2.30)]),
    ])
    r = mesurer(con, "ladbrokes_be", 7)
    assert r["suivis"] == 1
    assert r["ont_baisse"] == 0


def test_un_pari_SANS_trajectoire_est_exclu_pas_compte_survivant(tmp_path):
    """⚠️ LE PIÈGE CENTRAL. Un pari qu'on n'a jamais suivi n'a pas « gardé son
    prix » : on n'en sait rien. Le compter au dénominateur ferait baisser
    mécaniquement tous les taux de perte, et la sonde conclurait que les prix
    tiennent — exactement l'inverse de ce qu'il faut savoir."""
    con = _base(tmp_path, [
        (1, "ladbrokes_be", 2.10, [(0, 2.10), (10, 2.00)]),
        (2, "ladbrokes_be", 2.10, []),          # jamais suivi
        (3, "ladbrokes_be", 2.10, []),          # jamais suivi
    ])
    r = mesurer(con, "ladbrokes_be", 7)
    assert r["total"] == 3, "les détections restent comptées"
    assert r["suivis"] == 1, "seul le pari suivi entre au dénominateur"
    assert r["paliers"][15] == 1
    # 100 % des paris SUIVIS ont perdu leur prix — pas 33 %.
    assert 100.0 * r["paliers"][15] / r["suivis"] == 100.0


def test_les_books_ne_se_MELANGENT_pas(tmp_path):
    """La trajectoire d'Unibet ne doit jamais servir à juger un prix
    Ladbrokes : `odds_history` porte plusieurs books pour une même sélection."""
    con = _base(tmp_path, [
        (1, "ladbrokes_be", 2.10, [(0, 2.10)]),
        (2, "unibet_be", 2.10, [(0, 2.10), (5, 1.80)]),
    ])
    lad = mesurer(con, "ladbrokes_be", 7)
    uni = mesurer(con, "unibet_be", 7)
    assert lad["ont_baisse"] == 0
    assert uni["ont_baisse"] == 1


def test_les_paliers_sont_CUMULATIFS_et_croissants(tmp_path):
    con = _base(tmp_path, [
        (1, "ladbrokes_be", 2.10, [(0, 2.10), (8, 2.00)]),
        (2, "ladbrokes_be", 2.10, [(0, 2.10), (50, 2.00)]),
        (3, "ladbrokes_be", 2.10, [(0, 2.10), (200, 2.00)]),
    ])
    r = mesurer(con, "ladbrokes_be", 7)
    valeurs = [r["paliers"][p] for p in PALIERS]
    assert valeurs == sorted(valeurs), "un palier plus large compte moins"
    assert r["paliers"][5] == 0 and r["paliers"][10] == 1
    assert r["paliers"][60] == 2 and r["paliers"][300] == 3


def test_la_fenetre_en_jours_est_APPLIQUEE(tmp_path):
    """Une détection de 30 jours sort d'une fenêtre de 7 et entre dans une
    de 60 : sans ça, `--jours` serait un paramètre décoratif."""
    con = _base(tmp_path, [(1, "ladbrokes_be", 2.10, [(0, 2.10), (10, 2.00)])],
                il_y_a_jours=30)
    assert mesurer(con, "ladbrokes_be", 7)["total"] == 0
    assert mesurer(con, "ladbrokes_be", 60)["total"] == 1
    assert mesurer(con, "ladbrokes_be", 60)["paliers"][15] == 1


def test_la_sonde_nECRIT_RIEN(tmp_path):
    """Elle tourne sur la base de PRODUCTION pendant que le daemon écrit."""
    con = _base(tmp_path, [(1, "ladbrokes_be", 2.10, [(0, 2.10), (10, 2.00)])])
    mesurer(con, "ladbrokes_be", 7)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        con.execute("INSERT INTO odds_history VALUES (9,'x','y',1.0,1.0,1.0)")
