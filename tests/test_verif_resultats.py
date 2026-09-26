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
    # Une ligue SANS classe ne tranche rien : `repair_leagues --apply` la
    # remplit avec celle du match même qu'on met en doute. Le soupçon reste.
    autre = tmp_path / "b"
    autre.mkdir()
    d = _audit(autre, [("k", "Rosengard", "Djurgardens", "Sweden - Allsvenskan",
                        2, 1, "home")],
               [_fixture("Rosengard", "Djurgardens"),
                _fixture("Rosengård W", "Djurgården W", ligue="Damallsvenskan")])
    assert d["k"]["jumeau"] == "Rosengård W - Djurgården W"
    # Une ligue qui PORTE la classe, elle, la dit.
    feminin = tmp_path / "c"
    feminin.mkdir()
    d = _audit(feminin, [("k", "Rosengard", "Djurgardens", "Sweden - Women League",
                          0, 3, "away")],
               [_fixture("Rosengard", "Djurgardens"),
                _fixture("Rosengård W", "Djurgården W", hs=0, as_=3,
                         ligue="Damallsvenskan")])
    assert d["k"]["jumeau"] is None


def _en_production(tmp_path, monkeypatch, matchs, fixtures):
    """Un monde où la base est À SA PLACE de production (data/valuebet.db,
    relative au répertoire courant) : seul cas où la sonde imprime les
    commandes de correction. `.venv` y est relié pour que les commandes
    imprimées s'exécutent telles quelles."""
    db, dossier = _monde(tmp_path, matchs, fixtures)
    (tmp_path / "data").mkdir()
    db.rename(tmp_path / "data" / "valuebet.db")
    (tmp_path / ".venv").symlink_to(Path(vr.__file__).resolve().parents[1] / ".venv")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCORES_INGEST_DIR", str(tmp_path))
    return "data/valuebet.db", dossier


def test_la_sortie_et_les_commandes_corrigent_et_s_annulent(tmp_path, monkeypatch, capsys):
    """De bout en bout : la sonde liste, la commande retire SEULEMENT ce qui
    vaut encore ce qu'elle a lu, l'annulation remet tout."""
    db, dossier = _en_production(tmp_path, monkeypatch, [
        ("k1", "Inter", "Inter Miami", "L", 3, 0, "home"),       # inversé
        ("k2", "Arsenal", "Chelsea", "L", 2, 1, "home"),         # juste
        ("k3", "Dundee Utd", "Dundee", "L", 1, 2, "away")],      # indécidable
        [_fixture("Inter Miami", "Inter", hs=3, as_=0),
         _fixture("Arsenal", "Chelsea"),
         _fixture("Dundee United", "Dundee", hs=2, as_=1)])
    sortie = "suspects.jsonl"
    vr.main(["--db", db, "--depuis", "2026-09-19", "--sortie", sortie])
    out = capsys.readouterr().out
    lignes = (tmp_path / sortie).read_text().splitlines()
    assert {json.loads(l)["event_key"] for l in lignes} == {"k1", "k3"}
    retirer = vr.CMD_RETIRER.format(f=sortie, db=db)
    annuler = vr.CMD_ANNULER.format(f=sortie, db=db)
    assert retirer in out and annuler in out

    # Une ligne corrigée entre-temps ne doit PAS être retirée.
    c = sqlite3.connect(db)
    c.execute("UPDATE results SET home_score = 0, away_score = 3, winner = 'away' "
              "WHERE event_key = 'k1'")
    c.commit()
    r = subprocess.run(retirer, shell=True, cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert {k for (k,) in c.execute("SELECT event_key FROM results")} == {"k1", "k2"}

    r = subprocess.run(annuler, shell=True, cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lignes = dict((k, (w, h, a)) for k, w, h, a in c.execute(
        "SELECT event_key, winner, home_score, away_score FROM results"))
    assert lignes["k3"] == ("away", 1, 2)              # remis tel quel
    assert lignes["k1"] == ("away", 0, 3)              # réécrit depuis : intact


def test_une_copie_de_la_base_n_a_pas_de_commandes(tmp_path, capsys):
    """results-update et track-update agissent sur data/valuebet.db : corriger
    une copie laisserait l'erreur là où le P&L la lit."""
    db, dossier = _monde(tmp_path, [("k", "Inter", "Inter Miami", "L", 3, 0, "home")],
                         [_fixture("Inter Miami", "Inter", hs=3, as_=0)])
    os.environ["SCORES_INGEST_DIR"] = str(tmp_path)
    try:
        vr.main(["--db", str(db), "--depuis", "2026-09-19",
                 "--sortie", str(tmp_path / "s.jsonl")])
    finally:
        del os.environ["SCORES_INGEST_DIR"]
    out = capsys.readouterr().out
    assert "Ce n'est pas data/valuebet.db" in out
    assert "DELETE FROM results" not in out
    assert not (tmp_path / "s.jsonl").exists()


def test_un_double_regle_par_un_simple_est_faux_a_coup_sur(tmp_path, monkeypatch, capsys):
    """La source tennis n'a aucun double : un résultat qu'elle a écrit sur un
    double est le score d'un simple. La sonde le liste et le propose au
    retrait — sans relecture, il restera sans résultat."""
    db, dossier = _en_production(tmp_path, monkeypatch, [], [])
    c = sqlite3.connect(db)
    for k, h, a, lg in (("d1", "parkslang", "shnaiderkim", "ITF W50 Incheon"),
                        ("d2", "arevalopavic", "ramsalisbury", "ATP Cincinnati - Doubles"),
                        ("s1", "sinnerj", "alcarazc", "ATP Beijing")):
        c.execute("INSERT INTO events (event_key, sport, league, home, away, start_time)"
                  " VALUES (?,?,?,?,?,?)", (k, "tennis", lg, h, a, Q))
        c.execute("INSERT INTO results VALUES (?,?,?,?,?,?)",
                  (k, "away", 7, 12, "livetennisapi", "x"))
    c.execute("INSERT INTO teams VALUES ('parkslang', 'Sohyun Park / Lanlan Tang', 'x')")
    c.commit()
    c.close()
    vr.main(["--db", db, "--depuis", "2026-09-19", "--sortie", "s.jsonl"])
    out = capsys.readouterr().out
    assert "DOUBLES RÉGLÉS PAR UN SIMPLE (2)" in out
    assert "Sohyun Park / Lanlan Tang" in out
    cles = {json.loads(l)["event_key"] for l in (tmp_path / "s.jsonl").read_text().splitlines()}
    assert cles == {"d1", "d2"}
    assert "results-update" not in out.split("QUE FAIRE", 1)[1]


def test_un_resultat_importe_est_liste_jamais_retire(tmp_path, monkeypatch, capsys):
    """Un score saisi ou importé (`settle --from`) peut être une correction à
    la main : suspect, il est listé — jamais proposé au retrait."""
    db, dossier = _en_production(tmp_path, monkeypatch,
                                 [("k", "Dundee Utd", "Dundee", "L", 1, 2, "away")],
                                 [_fixture("Dundee United", "Dundee", hs=2, as_=1)])
    c = sqlite3.connect(db)
    c.execute("UPDATE results SET source = 'paris_track.csv'")
    c.commit()
    c.close()
    vr.main(["--db", db, "--depuis", "2026-09-19", "--sortie", "s.jsonl"])
    out = capsys.readouterr().out
    assert "SAISIS OU IMPORTÉS" in out and "origine : paris_track.csv" in out
    assert not (tmp_path / "s.jsonl").exists()


def test_un_dossier_de_sortie_absent_est_refuse_avant_le_rejeu(tmp_path):
    import pytest as _pt
    with _pt.raises(SystemExit):
        vr.main(["--db", str(tmp_path / "v.db"), "--sortie",
                 str(tmp_path / "absent" / "s.jsonl")])


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
    out = capsys.readouterr().out
    assert "Rien parmi les 1 résultats audités" in out
    assert "(0 n'ont pas pu être audités" in out



def test_un_score_stocke_par_une_ancienne_version_est_juge(tmp_path):
    """Avant le 18/08, rien n'était retourné : « Arsenal v Chelsea » contre la
    source « Chelsea - Arsenal » 2-1 était stocké 2-1, à l'envers. La sonde
    juge le sens STOCKÉ, pas celui qu'une règle d'aujourd'hui aurait pris :
    elle doit le voir, et non le ranger en « autre origine »."""
    d = _audit(tmp_path, [("k", "Arsenal", "Chelsea", "L", 2, 1, "home")],
               [_fixture("Chelsea", "Arsenal", hs=2, as_=1)])
    assert d["k"]["verdict"] == vr.INVERSE, d
    assert d["k"]["corrige"] == (1, 2)


def test_le_lendemain_absent_a_l_epoque_ne_cache_pas_le_resultat(tmp_path):
    """Un results-update quotidien lie le match de 23 h 55 AVANT que le
    fichier du lendemain existe. Si ce fichier ajoute depuis un rival qui rend
    le lot ambigu, la sonde rejoue sans lui, comme la production l'a vu."""
    db, dossier = _monde(tmp_path, [("k", "Inter", "Inter Miami", "L", 3, 0, "home")],
                         [])
    c = sqlite3.connect(str(db))
    c.execute("UPDATE events SET start_time = '2026-09-20T23:55:00+00:00'")
    c.commit()
    c.close()
    (dossier / "2026-09-20.json").write_text(json.dumps({"response": [
        _fixture("Inter Miami", "Inter", quand="2026-09-20T23:55:00+00:00", hs=3, as_=0)]}))
    (dossier / "2026-09-21.json").write_text(json.dumps({"response": [
        _fixture("Inter Miami", "Inter", quand="2026-09-21T00:03:00+00:00", hs=1, as_=1)]}))
    rows, noms, _j = vr.charger(str(db), J.date())
    d = dict((r["event_key"], dd) for r, dd in vr.analyser(rows, noms, dossier))
    assert d["k"]["verdict"] != vr.INTROUVABLE, d


def test_la_sauvegarde_n_est_jamais_ecrasee(tmp_path):
    """Relancer la sonde après une correction réécrirait un fichier SANS les
    lignes retirées : l'annulation n'aurait plus rien à remettre."""
    import pytest as _pt
    db, dossier = _monde(tmp_path, [("k", "Inter", "Inter Miami", "L", 3, 0, "home")],
                         [_fixture("Inter Miami", "Inter", hs=3, as_=0)])
    sortie = tmp_path / "sauvegarde.jsonl"
    sortie.write_text('{"event_key": "precieux"}\n')
    os.environ["SCORES_INGEST_DIR"] = str(tmp_path)
    try:
        with _pt.raises(SystemExit):
            vr.main(["--db", str(db), "--depuis", "2026-09-19", "--sortie", str(sortie)])
    finally:
        del os.environ["SCORES_INGEST_DIR"]
    assert "precieux" in sortie.read_text()


def test_sans_suspect_aucun_fichier(tmp_path):
    db, dossier = _monde(tmp_path, [("k", "Arsenal", "Chelsea", "L", 2, 1, "home")],
                         [_fixture("Arsenal", "Chelsea")])
    rows, noms, _j = vr.charger(str(db), J.date())
    assert vr.ecrire_sortie(str(tmp_path / "s.jsonl"), vr.analyser(rows, noms, dossier)) == 0
    assert not (tmp_path / "s.jsonl").exists()


def test_un_score_nul_ne_fait_pas_planter(tmp_path, capsys):
    """`settle --from` accepte un CSV sans score : un jumeau de classe au score
    NULL doit s'afficher, pas lever."""
    db, dossier = _monde(tmp_path, [("k", "Rosengard", "Djurgardens", "", None, None, "home")],
                         [_fixture("Rosengard", "Djurgardens"),
                          _fixture("Rosengård W", "Djurgården W", ligue="Damallsvenskan")])
    rows, noms, joues = vr.charger(str(db), J.date())
    vr.imprimer(vr.analyser(rows, noms, dossier), joues, J.date(), str(db), None)
    assert "en base : ?-?" in capsys.readouterr().out


def test_un_pari_n_est_liste_qu_une_fois(tmp_path, capsys):
    """Suspect ET jumeau de classe : le même pari ne doit pas compter deux fois."""
    db, dossier = _monde(tmp_path, [("k", "Arsenal", "Chelsea", "", 2, 1, "home")],
                         [_fixture("Chelsea", "Arsenal", hs=2, as_=1),
                          _fixture("Chelsea W", "Arsenal W", ligue="WSL Women")])
    c = sqlite3.connect(str(db))
    c.execute("INSERT INTO played_bets (dedup_key, played_at, event_key, sport, market,"
              " outcome_label, odd_taken, stake) VALUES ('d','x','k','soccer','h2h',"
              "'home',2.4,45)")
    c.commit()
    c.close()
    rows, noms, joues = vr.charger(str(db), J.date())
    vr.imprimer(vr.analyser(rows, noms, dossier), joues, J.date(), str(db), None)
    assert "TES PARIS JOUÉS SUR CES MATCHS (1)" in capsys.readouterr().out


def test_ecrire_sortie_refuse_un_fichier_existant(tmp_path):
    import pytest as _pt
    db, dossier = _monde(tmp_path, [("k", "Arsenal", "Chelsea", "L", 2, 1, "home")],
                         [_fixture("Chelsea", "Arsenal", hs=2, as_=1)])
    rows, noms, _j = vr.charger(str(db), J.date())
    sortie = tmp_path / "s.jsonl"
    sortie.write_text("sauvegarde\n")
    with _pt.raises(FileExistsError):
        vr.ecrire_sortie(str(sortie), vr.analyser(rows, noms, dossier))
    assert sortie.read_text() == "sauvegarde\n"


def test_la_fenetre_de_relecture_garde_une_semaine_de_marge():
    """Un results-update lancé demain doit encore reprendre le plus vieux."""
    il_y_a_10 = (datetime.now(timezone.utc) - timedelta(days=10)).replace(hour=0, minute=30)
    assert vr._jours([({"start_time": il_y_a_10.isoformat(), "event_key": "k"}, {})]) == 17


def test_les_commandes_attendent_le_verrou_60_s():
    """Le daemon écrit en continu ; 5 s (le défaut) échouent, le projet en
    exige 60 (SQLITE_BUSY_TIMEOUT_SEC)."""
    assert "timeout=60" in vr.CMD_RETIRER and "timeout=60" in vr.CMD_ANNULER
