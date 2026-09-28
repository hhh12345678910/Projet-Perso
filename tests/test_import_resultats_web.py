"""Import des résultats retrouvés sur le web (`scripts/import_resultats_web`).

La règle absolue : jamais de faux résultat. Une ligne qui ne désigne pas
exactement UN match joué est refusée, rien n'est écrit sans `--ecrire`, et un
résultat déjà en base n'est jamais écrasé."""
from __future__ import annotations

import csv
import sqlite3

from scripts import import_resultats_web as iw

ENTETE = ["date_utc", "sport", "domicile", "exterieur", "score_dom",
          "score_ext", "vainqueur", "source_1", "source_2", "note"]


def _base(tmp_path, paris, resultats=()):
    """`paris` : (event_key ou None, dedup_key, sport en base ou None)."""
    p = tmp_path / "v.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
        CREATE TABLE played_bets (dedup_key TEXT PRIMARY KEY, event_key TEXT);
        CREATE TABLE events (event_key TEXT PRIMARY KEY, sport TEXT,
            start_time TEXT);
        CREATE TABLE results (event_key TEXT PRIMARY KEY, winner TEXT,
            home_score REAL, away_score REAL, source TEXT, settled_at TEXT);
    """)
    for ek, dk, sport in paris:
        c.execute("INSERT INTO played_bets VALUES (?,?)", (dk, ek))
        if ek and sport:
            c.execute("INSERT OR IGNORE INTO events VALUES (?,?,NULL)",
                      (ek, sport))
    for ek, winner, h, a in resultats:
        c.execute("INSERT INTO results VALUES (?,?,?,?,'api','2026-09-01')",
                  (ek, winner, h, a))
    c.commit()
    c.close()
    return p


def _fichier(tmp_path, lignes):
    f = tmp_path / "web.csv"
    with open(f, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ENTETE)
        w.writeheader()
        for l in lignes:
            w.writerow({"source_1": "https://a.example/x",
                        "source_2": "https://b.example/y", **l})
    return f


def _lancer(tmp_path, capsys, paris, lignes, *extra, resultats=()):
    db = _base(tmp_path, paris, resultats)
    f = _fichier(tmp_path, lignes)
    iw.main(["--db", str(db), "--fichier", str(f), *extra])
    c = sqlite3.connect(str(db))
    ecrits = c.execute("SELECT event_key, winner, home_score, away_score, "
                       "source FROM results ORDER BY event_key").fetchall()
    c.close()
    return capsys.readouterr().out, ecrits


def _foot(date, dom, ext, h, a, **kw):
    return {"date_utc": date, "sport": "soccer", "domicile": dom,
            "exterieur": ext, "score_dom": h, "score_ext": a, **kw}


CLE = "202608231200::agf__vs__odense"


def test_la_simulation_n_ecrit_rien(tmp_path, capsys):
    out, ecrits = _lancer(tmp_path, capsys, [(CLE, "d1", "soccer")],
                          [_foot("2026-08-23 12:00", "agf", "odense", 2, 2)])
    assert ecrits == []
    assert "À ÉCRIRE" in out and "1 à écrire (simulation)" in out


def test_ecrit_le_score_et_le_vainqueur_avec_la_source_manuel_web(tmp_path, capsys):
    _, ecrits = _lancer(tmp_path, capsys, [(CLE, "d1", "soccer")],
                        [_foot("2026-08-23 12:00", "agf", "odense", 2, 2)],
                        "--ecrire")
    assert ecrits == [(CLE, "draw", 2.0, 2.0, "manuel-web")]


def test_un_resultat_deja_en_base_n_est_jamais_ecrase(tmp_path, capsys):
    out, ecrits = _lancer(tmp_path, capsys, [(CLE, "d1", "soccer")],
                          [_foot("2026-08-23 12:00", "agf", "odense", 2, 2)],
                          "--ecrire", resultats=[(CLE, "home", 1, 0)])
    assert ecrits == [(CLE, "home", 1.0, 0.0, "api")]
    assert "DÉJÀ" in out


def test_la_cle_d_un_clic_nu_se_lit_en_tete_du_dedup_key(tmp_path, capsys):
    _, ecrits = _lancer(tmp_path, capsys, [(None, f"{CLE}|h2h|away", None)],
                        [_foot("2026-08-23 12:00", "agf", "odense", 2, 2)],
                        "--ecrire")
    assert ecrits == [(CLE, "draw", 2.0, 2.0, "manuel-web")]


def test_hors_de_la_tolerance_horaire_rien_n_est_ecrit(tmp_path, capsys):
    out, ecrits = _lancer(tmp_path, capsys, [(CLE, "d1", "soccer")],
                          [_foot("2026-08-25 12:00", "agf", "odense", 2, 2)],
                          "--ecrire")
    assert ecrits == [] and "0 match(s)" in out


def test_le_meme_match_sous_deux_horaires_chaque_ligne_prend_sa_cle(tmp_path, capsys):
    """Krueger – Suresh existe à 17:00 et à 19:00 (heure révisée) : chaque
    ligne du fichier porte l'heure de sa clé."""
    k17 = "202606281700::mitchellkrueger__vs__dhakshineswarsuresh"
    k19 = "202606281900::mitchellkrueger__vs__dhakshineswarsuresh"
    tennis = {"sport": "tennis", "domicile": "mitchellkrueger",
              "exterieur": "dhakshineswarsuresh", "vainqueur": "away"}
    _, ecrits = _lancer(
        tmp_path, capsys, [(k17, "d1", "tennis"), (k19, "d2", "tennis")],
        [{"date_utc": "2026-06-28 17:00", **tennis},
         {"date_utc": "2026-06-28 19:00", **tennis}], "--ecrire")
    assert ecrits == [(k17, "away", None, None, "manuel-web"),
                      (k19, "away", None, None, "manuel-web")]


def test_deux_horaires_sans_cle_a_l_heure_exacte_restent_refuses(tmp_path, capsys):
    k17 = "202606281700::mitchellkrueger__vs__dhakshineswarsuresh"
    k19 = "202606281900::mitchellkrueger__vs__dhakshineswarsuresh"
    out, ecrits = _lancer(
        tmp_path, capsys, [(k17, "d1", "tennis"), (k19, "d2", "tennis")],
        [{"date_utc": "2026-06-28 18:00", "sport": "tennis",
          "domicile": "mitchellkrueger", "exterieur": "dhakshineswarsuresh",
          "vainqueur": "away"}], "--ecrire")
    assert ecrits == [] and "2 match(s)" in out


def test_des_noms_tronques_qui_designent_deux_matchs_sont_refuses(tmp_path, capsys):
    """« sydney » colle aux hommes ET aux femmes : l'horaire exact ne départage
    que des noms EXACTS, jamais deux classes différentes."""
    hommes = "202607290945::sydneyfc__vs__tottenhamhotspur"
    femmes = "202607290700::sydneyfcw__vs__tottenhamhotspurw"
    out, ecrits = _lancer(
        tmp_path, capsys, [(hommes, "d1", "soccer"), (femmes, "d2", "soccer")],
        [_foot("2026-07-29 09:45", "sydney", "tottenham", 1, 1)], "--ecrire")
    assert ecrits == [] and "2 match(s)" in out


def test_le_nom_ecrit_en_entier_designe_sa_cle(tmp_path, capsys):
    femmes = "202608231200::agf__vs__odensew"
    _, ecrits = _lancer(
        tmp_path, capsys, [(CLE, "d1", "soccer"), (femmes, "d2", "soccer")],
        [_foot("2026-08-23 12:00", "agf", "odense", 2, 2)], "--ecrire")
    assert ecrits == [(CLE, "draw", 2.0, 2.0, "manuel-web")]


def test_un_sport_qui_contredit_la_base_est_refuse(tmp_path, capsys):
    out, ecrits = _lancer(tmp_path, capsys, [(CLE, "d1", "basketball")],
                          [_foot("2026-08-23 12:00", "agf", "odense", 2, 2)],
                          "--ecrire")
    assert ecrits == [] and "sport « basketball » en base" in out


def test_une_ligne_a_une_seule_source_est_refusee(tmp_path, capsys):
    out, ecrits = _lancer(tmp_path, capsys, [(CLE, "d1", "soccer")],
                          [_foot("2026-08-23 12:00", "agf", "odense", 2, 2,
                                 source_2="")], "--ecrire")
    assert ecrits == [] and "il faut deux sources" in out


def test_le_tennis_exige_un_vainqueur_jamais_deduit_des_jeux(tmp_path, capsys):
    cle = "202606281700::mitchellkrueger__vs__dhakshineswarsuresh"
    out, ecrits = _lancer(
        tmp_path, capsys, [(cle, "d1", "tennis")],
        [{"date_utc": "2026-06-28 17:00", "sport": "tennis",
          "domicile": "mitchellkrueger", "exterieur": "dhakshineswarsuresh",
          "score_dom": 12, "score_ext": 5}], "--ecrire")
    assert ecrits == [] and "vainqueur (home/away) obligatoire" in out


def test_le_volley_n_ecrit_que_le_vainqueur(tmp_path, capsys):
    cle = "202607171430::turkiye__vs__ukraine"
    _, ecrits = _lancer(
        tmp_path, capsys, [(cle, "d1", "volleyball")],
        [{"date_utc": "2026-07-17 14:30", "sport": "volleyball",
          "domicile": "turkiye", "exterieur": "ukraine", "score_dom": 3,
          "score_ext": 1, "vainqueur": "home"}], "--ecrire")
    assert ecrits == [(cle, "home", None, None, "manuel-web")]


def test_un_nul_hors_du_football_est_refuse(tmp_path, capsys):
    cle = "202606302300::newyorkliberty__vs__lasvegasaces"
    out, ecrits = _lancer(
        tmp_path, capsys, [(cle, "d1", "basketball")],
        [{"date_utc": "2026-06-30 23:00", "sport": "basketball",
          "domicile": "newyorkliberty", "exterieur": "lasvegasaces",
          "score_dom": 90, "score_ext": 90}], "--ecrire")
    assert ecrits == [] and "match nul impossible" in out
