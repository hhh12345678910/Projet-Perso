"""La sonde des résultats EN BASE mal orientés par l'ancienne règle.

Chaque cas rejoue un résultat stocké contre le fichier du pont, comme la
production l'a fait, et compare l'ancienne orientation à la nouvelle. Les
commandes de correction et d'annulation qu'elle imprime sont exécutées ici :
une commande jamais lancée n'est pas un conseil.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import verif_resultats as vr

J = datetime(2026, 9, 20, tzinfo=timezone.utc)
Q = "2026-09-20T15:00:00+00:00"


def _fixture(dom, ext, quand=Q, hs=2, as_=1, statut="FT", ligue="L"):
    return {"fixture": {"date": quand, "status": {"short": statut}},
            "league": {"name": ligue},
            "teams": {"home": {"name": dom}, "away": {"name": ext}},
            "score": {"fulltime": {"home": hs, "away": as_},
                      "extratime": {"home": None, "away": None}},
            "goals": {"home": hs, "away": as_}}


def _monde(tmp_path, matchs, fixtures):
    """`matchs` : (clé, dom, ext, ligue, score dom, score ext, gagnant)."""
    from src.storage import Storage
    db = tmp_path / "v.db"
    Storage(str(db))
    c = sqlite3.connect(str(db))
    for k, h, a, lg, hs, as_, w in matchs:
        c.execute("INSERT INTO events (event_key, sport, league, home, away, start_time)"
                  " VALUES (?,?,?,?,?,?)", (k, "soccer", lg, h, a, Q))
        c.execute("INSERT INTO value_bets (event_key, book, market, outcome_label,"
                  " odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct, detected_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (k, "u", "h2h", "home", 2.1, .5, 2.0, 5, 1, Q))
        c.execute("INSERT INTO results VALUES (?,?,?,?,?,?)",
                  (k, w, hs, as_, "api-football", "2026-09-21T10:00:00+00:00"))
    c.commit()
    c.close()
    dossier = tmp_path / "soccer"
    dossier.mkdir()
    (dossier / "2026-09-20.json").write_text(json.dumps({"response": fixtures}))
    return db, dossier


def _audit(tmp_path, matchs, fixtures, noms=None):
    db, dossier = _monde(tmp_path, matchs, fixtures)
    rows, n, _j = vr.charger(str(db), J.date())
    return {r["event_key"]: d for r, d in vr.analyser(rows, noms or n, dossier)}


def test_un_resultat_bien_oriente_est_confirme(tmp_path):
    d = _audit(tmp_path, [("k", "Arsenal", "Chelsea", "L", 2, 1, "home")],
               [_fixture("Arsenal", "Chelsea")])
    assert d["k"]["verdict"] == vr.OK


def test_dundee_utd_retourne_par_l_ancienne_regle_est_signale(tmp_path):
    """Même ordre, « Utd » : l'ancienne règle a retourné le score (2-1 de la
    source stocké 1-2). La nouvelle ne sait pas trancher : INDÉCIDABLE — et
    c'est bien un résultat faux en base."""
    d = _audit(tmp_path, [("k", "Dundee Utd", "Dundee", "L", 1, 2, "away")],
               [_fixture("Dundee United", "Dundee", hs=2, as_=1)])
    assert d["k"]["verdict"] == vr.INDECIDABLE, d


def test_inter_miami_a_l_envers_est_un_inverse_probable(tmp_path):
    """Source inversée, égalité pour l'ancienne règle : score stocké dans
    l'ordre de la source, donc à l'envers. La nouvelle règle le remet."""
    d = _audit(tmp_path, [("k", "Inter", "Inter Miami", "L", 3, 0, "home")],
               [_fixture("Inter Miami", "Inter", hs=3, as_=0)])
    assert d["k"]["verdict"] == vr.INVERSE, d
    assert d["k"]["corrige"] == (0, 3)


def test_un_nul_symetrique_n_est_pas_suspect(tmp_path):
    d = _audit(tmp_path, [("k", "Dundee Utd", "Dundee", "L", 1, 1, "draw")],
               [_fixture("Dundee United", "Dundee", hs=1, as_=1)])
    assert d["k"]["verdict"] == vr.SYMETRIQUE


def test_un_score_d_une_autre_origine_n_est_pas_audite(tmp_path):
    """Un score que le pont n'aurait pas donné (import CSV, saisie) : la sonde
    ne sait pas d'où il vient, elle ne le juge pas."""
    d = _audit(tmp_path, [("k", "Arsenal", "Chelsea", "L", 4, 4, "draw")],
               [_fixture("Arsenal", "Chelsea")])
    assert d["k"]["verdict"] == vr.AUTRE_ORIGINE


def test_un_match_absent_du_pont_n_est_pas_audite(tmp_path):
    d = _audit(tmp_path, [("k", "Arsenal", "Chelsea", "L", 2, 1, "home")],
               [_fixture("Kontu", "LPS")])
    assert d["k"]["verdict"] == vr.INTROUVABLE


def test_un_match_sans_ligue_a_jumeau_feminin_est_signale(tmp_path):
    """Juin-juillet, sans ligue : rien ne dit si c'était le match des seniors
    ou son jumeau féminin, que la source avait au même horaire."""
    d = _audit(tmp_path, [("k", "Rosengard", "Djurgardens", "", 2, 1, "home")],
               [_fixture("Rosengard", "Djurgardens"),
                _fixture("Rosengård W", "Djurgården W", ligue="Damallsvenskan")])
    assert d["k"]["jumeau"] == "Rosengård W - Djurgården W"
    # Avec une ligue, la classe est connue : pas de soupçon.
    autre = tmp_path / "b"
    autre.mkdir()
    d = _audit(autre, [("k", "Rosengard", "Djurgardens", "Sweden - Allsvenskan",
                        2, 1, "home")],
               [_fixture("Rosengard", "Djurgardens"),
                _fixture("Rosengård W", "Djurgården W", ligue="Damallsvenskan")])
    assert d["k"]["jumeau"] is None


def test_la_sortie_et_les_commandes_corrigent_et_s_annulent(tmp_path, capsys):
    """De bout en bout : la sonde liste, la commande retire SEULEMENT ce qui
    vaut encore ce qu'elle a lu, l'annulation remet tout."""
    db, dossier = _monde(tmp_path, [
        ("k1", "Inter", "Inter Miami", "L", 3, 0, "home"),       # inversé
        ("k2", "Arsenal", "Chelsea", "L", 2, 1, "home"),         # juste
        ("k3", "Dundee Utd", "Dundee", "L", 1, 2, "away")],      # indécidable
        [_fixture("Inter Miami", "Inter", hs=3, as_=0),
         _fixture("Arsenal", "Chelsea"),
         _fixture("Dundee United", "Dundee", hs=2, as_=1)])
    os.environ["SCORES_INGEST_DIR"] = str(tmp_path)
    try:
        sortie = tmp_path / "suspects.jsonl"
        vr.main(["--db", str(db), "--depuis", "2026-09-19", "--sortie", str(sortie)])
    finally:
        del os.environ["SCORES_INGEST_DIR"]
    out = capsys.readouterr().out
    assert {json.loads(l)["event_key"] for l in sortie.read_text().splitlines()} == {"k1", "k3"}
    retirer = vr.CMD_RETIRER.format(f=sortie, db=db)
    annuler = vr.CMD_ANNULER.format(f=sortie, db=db)
    assert retirer in out and annuler in out

    # Une ligne corrigée entre-temps ne doit PAS être retirée.
    c = sqlite3.connect(str(db))
    c.execute("UPDATE results SET home_score = 0, away_score = 3, winner = 'away' "
              "WHERE event_key = 'k1'")
    c.commit()
    racine = Path(vr.__file__).resolve().parents[1]
    r = subprocess.run(retirer, shell=True, cwd=racine, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert {k for (k,) in c.execute("SELECT event_key FROM results")} == {"k1", "k2"}

    r = subprocess.run(annuler, shell=True, cwd=racine, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lignes = dict((k, (w, h, a)) for k, w, h, a in c.execute(
        "SELECT event_key, winner, home_score, away_score FROM results"))
    assert lignes["k3"] == ("away", 1, 2)              # remis tel quel
    assert lignes["k1"] == ("away", 0, 3)              # réécrit depuis : intact


def test_les_paris_joues_touches_sont_listes(tmp_path, capsys):
    db, dossier = _monde(tmp_path, [("k", "Inter", "Inter Miami", "L", 3, 0, "home")],
                         [_fixture("Inter Miami", "Inter", hs=3, as_=0)])
    c = sqlite3.connect(str(db))
    c.execute("INSERT INTO played_bets (dedup_key, played_at, event_key, sport, market,"
              " outcome_label, odd_taken, stake) VALUES ('d','x','k','soccer','h2h',"
              "'home',2.4,45)")
    c.commit()
    c.close()
    rows, noms, joues = vr.charger(str(db), J.date())
    vr.imprimer(vr.analyser(rows, noms, dossier), joues, J.date(), str(db), None)
    out = capsys.readouterr().out
    assert "TES PARIS JOUÉS SUR CES MATCHS (1)" in out
    assert "h2h home @2.40" in out and "mise 45" in out


def test_rien_a_corriger(tmp_path, capsys):
    db, dossier = _monde(tmp_path, [("k", "Arsenal", "Chelsea", "L", 2, 1, "home")],
                         [_fixture("Arsenal", "Chelsea")])
    rows, noms, joues = vr.charger(str(db), J.date())
    vr.imprimer(vr.analyser(rows, noms, dossier), joues, J.date(), str(db), None)
    assert "Rien : aucun résultat" in capsys.readouterr().out

