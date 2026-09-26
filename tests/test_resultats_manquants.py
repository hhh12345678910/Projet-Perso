"""La sonde des paris joués sans résultat.

LE PIÈGE QU'ELLE DÉMÊLE
-----------------------
« 100 réglés sur 200 » mélange des matchs PAS ENCORE JOUÉS (normal) et des
matchs finis dont le résultat n'est jamais arrivé — pour une raison chaque
fois différente, qui appelle chaque fois un geste différent. Une journée que
le pont n'a jamais demandée et un match que la source n'a pas donnent le même
symptôme : rien. La sonde doit les séparer, et ces tests tiennent la
frontière entre chaque cause.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from scripts import resultats_manquants as rm

MAINTENANT = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
FINAL = 6 * 3600


def _fichier(dossier, jour: date, capture: datetime, ext="json"):
    f = dossier / f"{jour.isoformat()}.{ext}"
    f.write_text("{}")
    os.utime(f, (capture.timestamp(), capture.timestamp()))
    return f


# ── L'état d'une journée, tel que le pont le voit ────────────────────

def test_une_journee_jamais_demandee_dans_la_fenetre(tmp_path):
    hier = date(2026, 9, 25)
    assert rm.etat_journee(hier, tmp_path, MAINTENANT, 2, FINAL) == rm.JAMAIS_DEMANDEE


def test_une_journee_hors_de_la_fenetre_ne_sera_plus_demandee(tmp_path):
    """Le cas qui explique le plus souvent un trou : la fenêtre par défaut est
    de DEUX jours. Sans clic quotidien, le troisième jour n'est jamais pris."""
    il_y_a_4_jours = date(2026, 9, 22)
    assert rm.etat_journee(il_y_a_4_jours, tmp_path, MAINTENANT, 2, FINAL) == rm.HORS_FENETRE
    assert rm.etat_journee(il_y_a_4_jours, tmp_path, MAINTENANT, 7, FINAL) == rm.JAMAIS_DEMANDEE


def test_une_pierre_tombale_seule_est_un_refus(tmp_path):
    j = date(2026, 9, 24)
    _fichier(tmp_path, j, MAINTENANT, ext="refused")
    assert rm.etat_journee(j, tmp_path, MAINTENANT, 7, FINAL) == rm.REFUSEE


def test_le_fichier_passe_avant_la_pierre_tombale(tmp_path):
    """Capturée tôt, puis refusée à la reprise : le fichier reste, et
    `results-update` le lit (il ignore les `.refused`). La journée n'est pas
    perdue — la déclarer « refusée » l'aurait fait croire."""
    j = date(2026, 9, 24)
    _fichier(tmp_path, j, MAINTENANT, ext="refused")
    _fichier(tmp_path, j, datetime(2026, 9, 25, 7, 0, tzinfo=timezone.utc))
    assert rm.etat_journee(j, tmp_path, MAINTENANT, 7, FINAL) == rm.FOOT_ABSENT


def test_capturee_tot_puis_refusee_ne_sera_plus_reprise(tmp_path):
    """Dans la fenêtre, mais la pierre tombale empêche le pont de la
    redemander : « sera reprise au prochain clic » serait faux — et « hors
    fenêtre » aussi : c'est le refus, pas la fenêtre, qui la bloque."""
    j = date(2026, 9, 24)
    _fichier(tmp_path, j, MAINTENANT, ext="refused")
    _fichier(tmp_path, j, datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc))
    assert rm.etat_journee(j, tmp_path, MAINTENANT, 7, FINAL) == rm.TROP_TOT_REFUSEE


def test_une_journee_capturee_trop_tot_sera_reprise(tmp_path):
    """Capturée à 02 h le lendemain : moins de 6 h après la fin de la journée,
    donc pas définitive — le pont la redemandera tant qu'elle est dans la
    fenêtre."""
    j = date(2026, 9, 24)
    _fichier(tmp_path, j, datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc))
    assert rm.etat_journee(j, tmp_path, MAINTENANT, 7, FINAL) == rm.TROP_TOT
    assert rm.etat_journee(j, tmp_path, MAINTENANT, 1, FINAL) == rm.TROP_TOT_HORS


def test_une_journee_definitive_designe_la_source(tmp_path):
    j = date(2026, 9, 24)
    _fichier(tmp_path, j, datetime(2026, 9, 25, 7, 0, tzinfo=timezone.utc))
    assert rm.etat_journee(j, tmp_path, MAINTENANT, 7, FINAL) == rm.FOOT_ABSENT


# ── Le classement d'un pari ──────────────────────────────────────────

def _pari(**kw):
    base = dict(market="h2h", outcome_label="home", line=None, winner=None,
                home_score=None, away_score=None, has_result=0, has_event=1,
                sport="soccer", start_time="2026-09-24T18:00:00+00:00",
                event_key="202609241800::a__vs__b")
    base.update(kw)
    return base


def _classer(tmp_path, **kw):
    return rm.classer(_pari(**kw), MAINTENANT, tmp_path, 7, FINAL)


def test_un_pari_regle(tmp_path):
    assert _classer(tmp_path, has_result=1, winner="home") == rm.REGLE


def test_un_match_a_venir_n_est_pas_un_manque(tmp_path):
    assert _classer(tmp_path, start_time="2026-09-27T18:00:00+00:00") == rm.A_VENIR
    # Fini depuis moins de 2 h : pas encore réclamé par results-update.
    assert _classer(tmp_path, start_time="2026-09-26T11:00:00+00:00") == rm.A_VENIR


def test_le_foot_du_jour_attend_le_lendemain(tmp_path):
    assert _classer(tmp_path, start_time="2026-09-26T08:00:00+00:00") == rm.FOOT_DU_JOUR


def test_le_tennis_n_a_pas_de_pont(tmp_path):
    assert _classer(tmp_path, sport="tennis") == rm.TENNIS


def test_un_sport_sans_source(tmp_path):
    assert _classer(tmp_path, sport="hockey") == rm.SANS_SOURCE


def test_sans_ligne_events(tmp_path):
    assert _classer(tmp_path, has_event=0, start_time=None) == rm.SANS_EVENTS


def test_sans_ligne_events_l_heure_vient_de_la_cle(tmp_path):
    """Sans `events`, l'horaire est lu dans la clé : un match futur reste
    « à venir » et n'est pas compté comme un manque."""
    assert _classer(tmp_path, has_event=0, start_time=None,
                    event_key="202609301800::a__vs__b") == rm.A_VENIR


def test_une_mi_temps_ne_se_regle_jamais(tmp_path):
    assert _classer(tmp_path, market="h2h_h1", has_result=1, winner="home") == rm.MARCHE


def test_un_resultat_sans_score_ne_regle_pas_un_total(tmp_path):
    """Un résultat en base qui porte le vainqueur mais pas les scores règle un
    1X2, jamais un over/under : le pari n'est pas réglé pour autant."""
    assert _classer(tmp_path, market="totals", outcome_label="over", line=2.5,
                    has_result=1, winner="home") == rm.INEXPLOITABLE


def test_un_pari_foot_fini_prend_l_etat_de_sa_journee(tmp_path):
    assert _classer(tmp_path) == rm.JAMAIS_DEMANDEE


# ── De bout en bout, sur une base ────────────────────────────────────

def _base(tmp_path, paris):
    """`paris` : (clé, sport, départ ISO ou None, marché, pari, ligne, résultat
    (winner, h, a) ou None, avec_events)."""
    p = tmp_path / "v.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
        CREATE TABLE played_bets (dedup_key TEXT PRIMARY KEY, played_at TEXT,
            value_bet_id INTEGER, event_key TEXT, sport TEXT, book TEXT,
            market TEXT, outcome_label TEXT, line REAL, odd_taken REAL,
            fair_odd REAL, ev_pct REAL, stake REAL);
        CREATE TABLE events (event_key TEXT PRIMARY KEY, sport TEXT,
            league TEXT, home TEXT, away TEXT, start_time TEXT);
        CREATE TABLE results (event_key TEXT PRIMARY KEY, winner TEXT,
            home_score REAL, away_score REAL, source TEXT, settled_at TEXT);
    """)
    for i, (ek, sport, depart, marche, pari, ligne, res, avec) in enumerate(paris):
        c.execute("INSERT INTO played_bets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f"k{i}", "2026-09-20T10:00:00+00:00", i, ek, sport,
                   "unibet_be", marche, pari, ligne, 2.10, 1.9, 8.0, 40.0))
        if avec:
            c.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                      (ek, sport, "L1", f"dom{i}", f"ext{i}", depart))
        if res:
            c.execute("INSERT OR IGNORE INTO results VALUES (?,?,?,?,?,?)",
                      (ek, *res, "t", "2026-09-25"))
    c.commit()
    c.close()
    return p


def _lancer(tmp_path, monkeypatch, capsys, paris, jours_pont="2", *extra,
            retouche=None, env=None, depuis=10):
    """La base, un dossier de pont vide, et une date de départ relative à
    AUJOURD'HUI — `main` lit l'horloge réelle. `retouche(connexion, dossier)`
    abîme la base ou le pont avant le lancement ; `env` écrase des réglages."""
    scores = tmp_path / "scores"
    (scores / "soccer").mkdir(parents=True)
    monkeypatch.setattr(rm, "load_env_file", lambda *a, **k: 0)
    monkeypatch.setenv("SCORES_INGEST_DIR", str(scores))
    monkeypatch.setenv("SCORES_BRIDGE_DAYS", jours_pont)
    monkeypatch.setenv("SCORES_FOOTBALL_BRIDGE", "1")
    monkeypatch.setenv("SCORES_TENNIS_KEY", "cle")
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    p = _base(tmp_path, paris)
    if retouche:
        c = sqlite3.connect(str(p))
        retouche(c, scores / "soccer")
        c.commit()
        c.close()
    debut = (datetime.now(timezone.utc).date() - timedelta(days=depuis)).isoformat()
    rm.main(["--db", str(p), "--joues", "--depuis", debut, *extra])
    return capsys.readouterr().out, scores / "soccer"


def _jour(n: int) -> str:
    """Il y a n jours à 15 h UTC, en ISO."""
    d = datetime.now(timezone.utc).date() - timedelta(days=n)
    return f"{d.isoformat()}T15:00:00+00:00"


def _cle(n: int, i: int) -> str:
    d = datetime.now(timezone.utc).date() - timedelta(days=n)
    return f"{d.strftime('%Y%m%d')}1500::dom{i}__vs__ext{i}"


def test_le_bilan_separe_le_normal_du_manquant(tmp_path, monkeypatch, capsys):
    paris = [
        (_cle(3, 0), "soccer", _jour(3), "h2h", "home", None, ("home", 1, 0), True),
        (_cle(-2, 1), "soccer", _jour(-2), "h2h", "home", None, None, True),
        (_cle(5, 2), "soccer", _jour(5), "h2h", "home", None, None, True),
        (_cle(1, 3), "soccer", _jour(1), "h2h", "away", None, None, True),
        (_cle(4, 4), "tennis", _jour(4), "h2h", "home", None, None, True),
    ]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris)
    assert "Sur 5 paris joués" in out
    assert f"{rm.REGLE}" in out and f"{rm.A_VENIR}" in out
    # 3 manquants : un foot hors fenêtre (il y a 5 jours, fenêtre 2), un foot
    # d'hier jamais demandé, un tennis.
    assert "→ 3 pari(s) dont le match est FINI" in out
    assert rm.HORS_FENETRE in out and rm.JAMAIS_DEMANDEE in out and rm.TENNIS in out


def test_hors_fenetre_la_sonde_donne_la_commande_qui_elargit(tmp_path, monkeypatch,
                                                            capsys):
    paris = [(_cle(5, 0), "soccer", _jour(5), "h2h", "home", None, None, True)]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris)
    assert "SORTIES de la fenêtre" in out
    assert "echo 'SCORES_BRIDGE_DAYS=5' >> .env" in out
    assert "sudo systemctl restart betano-ingest" in out


def test_une_fenetre_assez_large_ne_propose_pas_d_elargir(tmp_path, monkeypatch,
                                                         capsys):
    paris = [(_cle(5, 0), "soccer", _jour(5), "h2h", "home", None, None, True)]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris, "10")
    assert "SORTIES de la fenêtre" not in out
    assert rm.JAMAIS_DEMANDEE in out
    assert rm.BOUTON in out


def test_rien_ne_manque_quand_tout_est_regle_ou_a_venir(tmp_path, monkeypatch, capsys):
    paris = [
        (_cle(3, 0), "soccer", _jour(3), "h2h", "home", None, ("home", 1, 0), True),
        (_cle(-1, 1), "soccer", _jour(-1), "h2h", "home", None, None, True),
    ]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris)
    assert "→ 0 pari(s)" in out
    assert "Rien ne manque" in out


def test_la_liste_nomme_les_paris_sans_resultat(tmp_path, monkeypatch, capsys):
    paris = [
        (_cle(3, 0), "soccer", _jour(3), "h2h", "home", None, ("home", 1, 0), True),
        (_cle(5, 7), "soccer", _jour(5), "totals", "over 2.5", 2.5, None, True),
    ]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris, "2", "--lister")
    liste = out.split("UN PAR UN", 1)[1]
    assert "dom1 - ext1" in liste and "over 2.5" in liste
    # Un pari réglé n'a rien à faire dans la liste des manques.
    assert "dom0" not in liste


def test_la_journee_de_foot_est_resumee(tmp_path, monkeypatch, capsys):
    paris = [(_cle(5, i), "soccer", _jour(5), "h2h", "home", None, None, True)
             for i in range(3)]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris)
    bloc = out.split("JOURNÉE PAR JOURNÉE", 1)[1]
    jour = (datetime.now(timezone.utc).date() - timedelta(days=5)).isoformat()
    assert f"{jour}    3 pari(s)" in bloc


def test_la_fenetre_par_defaut_est_annoncee_comme_telle(tmp_path, monkeypatch, capsys):
    """Sans SCORES_BRIDGE_DAYS dans l'environnement, la sonde juge sur le
    défaut du serveur — et doit le DIRE, sinon on croirait la valeur lue."""
    scores = tmp_path / "scores"
    (scores / "soccer").mkdir(parents=True)
    monkeypatch.setattr(rm, "load_env_file", lambda *a, **k: 0)
    monkeypatch.setenv("SCORES_INGEST_DIR", str(scores))
    monkeypatch.delenv("SCORES_BRIDGE_DAYS", raising=False)
    p = _base(tmp_path, [])
    rm.main(["--db", str(p), "--joues"])
    assert "SCORES_BRIDGE_DAYS = 2 (absent de .env" in capsys.readouterr().out


def test_une_date_mal_ecrite_est_refusee(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, "load_env_file", lambda *a, **k: 0)
    with pytest.raises(SystemExit):
        rm.main(["--db", str(_base(tmp_path, [])), "--joues", "--depuis", "26/09/2026"])


def test_elargir_passe_avant_cliquer(tmp_path, monkeypatch, capsys):
    """Un clic sur l'ancienne fenêtre ne reprendrait pas les journées sorties :
    on croirait le trou comblé. L'élargissement doit être proposé d'abord."""
    paris = [(_cle(5, 0), "soccer", _jour(5), "h2h", "home", None, None, True)]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris)
    assert out.index("SORTIES de la fenêtre") < out.index(rm.BOUTON)


def test_la_ligne_n_est_pas_ecrite_deux_fois(tmp_path, monkeypatch, capsys):
    paris = [(_cle(5, 0), "soccer", _jour(5), "totals", "over", 2.5, None, True),
             (_cle(5, 1), "soccer", _jour(5), "totals", "under 3.5", 3.5, None, True)]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris, "2", "--lister")
    assert "over 2.5" in out and "under 3.5" in out and "3.5 3.5" not in out


# ── Pourquoi un match de football n'a pas été rapproché ─────────────

import json as _json  # noqa: E402


def _fixture(dom, ext, quand, statut="FT", ligue="L"):
    """Un match tel que le pont le range. Le score est là parce que la
    production ne rapproche que ce que `parse_apifootball_results` retient."""
    return {"fixture": {"date": quand, "status": {"short": statut}},
            "league": {"name": ligue},
            "teams": {"home": {"name": dom}, "away": {"name": ext}},
            "score": {"fulltime": {"home": 1, "away": 0},
                      "extratime": {"home": None, "away": None}},
            "goals": {"home": 1, "away": 0}}


def _jour_source(dossier, jour: str, fixtures):
    (dossier / f"{jour}.json").write_text(_json.dumps({"response": fixtures}))


def _notre(dom, ext, quand, ligue="L1"):
    return {"home": dom, "away": ext, "start_time": quand, "league": ligue,
            "event_key": "x"}


def _verdict(tmp_path, notre, fixtures, jour="2026-09-22", noms=None):
    _jour_source(tmp_path, jour, fixtures)
    return rm.diagnostiquer(notre, noms or {}, tmp_path, {})


Q = "2026-09-22T18:45:00+00:00"


def test_un_match_absent_de_la_source(tmp_path):
    d = _verdict(tmp_path, _notre("Kontu", "LPS", Q),
                 [_fixture("Arsenal", "Chelsea", Q)])
    assert d["verdict"] == rm.ABSENT


def test_la_barriere_de_classe_est_nommee(tmp_path):
    """Chez nous « Rangers B » (réserve), chez la source « Rangers » : les noms
    sont identiques, seule la classe les sépare. C'est corrigeable, et la sonde
    doit le dire au lieu de conclure « absent »."""
    d = _verdict(tmp_path, _notre("rangersxreserve", "clyde", Q),
                 [_fixture("Clyde", "Rangers", Q)],
                 noms={"rangersxreserve": "Rangers B", "clyde": "Clyde"})
    assert d["verdict"] == rm.CLASSE, d


def test_un_horaire_decale_est_nomme(tmp_path):
    d = _verdict(tmp_path, _notre("Kingstonian", "Bedfont", Q),
                 [_fixture("Kingstonian FC", "Bedfont", "2026-09-22T19:45:00+00:00")])
    assert d["verdict"] == rm.HORAIRE
    assert round(d["cand"]["dt"]) == 60


def test_un_match_reporte(tmp_path):
    """Un PST tel que l'API l'envoie : ni score ni buts. Ce n'est pas « terminé
    sans score » — il n'a pas été joué."""
    f = _fixture("Vancouver FC", "Inter Toronto", Q, statut="PST")
    f["score"]["fulltime"] = {"home": None, "away": None}
    f["goals"] = {"home": None, "away": None}
    d = _verdict(tmp_path, _notre("Vancouver", "Inter Toronto", Q), [f])
    assert d["verdict"] == rm.STATUT and d["cand"]["statut"] == "PST"


def test_une_prolongation(tmp_path):
    d = _verdict(tmp_path, _notre("Clyde", "Stranraer", Q),
                 [_fixture("Clyde", "Stranraer", Q, statut="AET")])
    assert d["verdict"] == rm.PROLONG


def test_un_match_appariable_designe_results_update(tmp_path):
    d = _verdict(tmp_path, _notre("Lower Breck", "Atherton Collieries", Q),
                 [_fixture("Lower Breck FC", "Atherton Collieries", Q)])
    assert d["verdict"] == rm.APPARIABLE


def test_des_noms_seulement_proches_sont_montres_pas_declares(tmp_path):
    notre = _notre("Deportivo Maipu", "Atletico Tucuman", Q)
    d = _verdict(tmp_path, notre, [_fixture("Dep. Maipu", "Atl. Tucuman", Q)])
    # Le jeu d'essai doit tomber dans la zone grise, sinon le test ne prouve rien.
    assert rm.PLANCHER_CANDIDAT <= d["cand"]["u"] < rm.SEUIL_APPARIEMENT, d
    assert d["verdict"] == rm.NOMS


def test_un_bruit_proche_dans_l_heure_reste_absent(tmp_path):
    """Cas réel : un match guatémaltèque, et la source sert « Juventus -
    Atalanta » à 60 min. Moyenne 80, mais aucun camp n'est le même club et
    un seul est proche : c'est du bruit, le match est absent."""
    d = _verdict(tmp_path, _notre("Amatitlan", "Juventud Copalera", Q),
                 [_fixture("Juventus", "Atalanta", "2026-09-22T19:45:00+00:00")])
    assert rm.PLANCHER_CANDIDAT <= d["cand"]["u"] < rm.SEUIL_APPARIEMENT, d
    assert d["verdict"] == rm.ABSENT, d


def test_un_candidat_a_des_heures_de_distance_reste_absent(tmp_path):
    """Cas réel : « YSCC Yokohama » contre « Yokohama FC », 22 h plus tard.
    Un camp identique ne suffit pas quand l'horaire n'a rien à voir."""
    _jour_source(tmp_path, "2026-09-23",
                 [_fixture("Yokohama FC", "Imabari", "2026-09-23T16:45:00+00:00")])
    d = rm.diagnostiquer(_notre("YSCC Yokohama", "Briobecca Urayasu SC", Q),
                         {}, tmp_path, {})
    assert d["cand"]["u"] >= rm.PLANCHER_CANDIDAT, d
    assert d["verdict"] == rm.ABSENT, d


def test_un_club_renomme_est_montre(tmp_path):
    """Cas réel : « York United » est devenu « Inter Toronto ». Un camp
    identique, même heure : c'est un candidat à regarder, pas un absent."""
    d = _verdict(tmp_path, _notre("Vancouver FC", "Inter Toronto", Q),
                 [_fixture("Vancouver FC", "York United", Q)])
    assert d["verdict"] == rm.NOMS, d
    assert "York United" in d["cand"]["nom"]


def test_le_bruit_mieux_note_ne_masque_pas_le_candidat_plausible(tmp_path):
    """Le bruit (83, quinze heures plus tard) est devant le club renommé (75)
    au score brut ; c'est pourtant le second qu'il faut montrer."""
    bruit = _fixture("Valencia", "Toronto", "2026-09-23T10:00:00+00:00")
    _jour_source(tmp_path, "2026-09-23", [bruit])
    d = _verdict(tmp_path, _notre("Vancouver FC", "Inter Toronto", Q),
                 [_fixture("Vancouver FC", "York United", Q)])
    # Le jeu d'essai doit vraiment mettre le bruit devant au score brut.
    brut = rm._paire(rm._sim_sans_classe, "Vancouver FC", "Inter Toronto",
                     "Valencia", "Toronto")
    assert brut > d["cand"]["u"], (brut, d)
    assert d["verdict"] == rm.NOMS, d
    assert "York United" in d["cand"]["nom"], d


def test_le_fichier_du_lendemain_est_aussi_lu(tmp_path):
    """Un match à 23 h 50 chez nous, daté de 00 h 05 le lendemain chez la
    source : le chercher dans un seul fichier le déclarerait absent à tort."""
    _jour_source(tmp_path, "2026-09-23",
                 [_fixture("Clyde", "Stranraer", "2026-09-23T00:05:00+00:00")])
    d = rm.diagnostiquer(_notre("Clyde", "Stranraer", "2026-09-22T23:50:00+00:00"),
                         {}, tmp_path, {})
    assert d["verdict"] == rm.HORAIRE


def test_le_diagnostic_apparait_dans_la_sortie(tmp_path, monkeypatch, capsys):
    """De bout en bout : une journée complète, un match classé autrement."""
    scores = tmp_path / "scores"
    (scores / "soccer").mkdir(parents=True)
    monkeypatch.setattr(rm, "load_env_file", lambda *a, **k: 0)
    monkeypatch.setenv("SCORES_INGEST_DIR", str(scores))
    monkeypatch.setenv("SCORES_BRIDGE_DAYS", "14")
    monkeypatch.setenv("SCORES_FOOTBALL_BRIDGE", "1")
    n = 4
    jour = (datetime.now(timezone.utc).date() - timedelta(days=n))
    quand = f"{jour.isoformat()}T15:00:00+00:00"
    p = _base(tmp_path, [(_cle(n, 0), "soccer", quand, "h2h", "home", None,
                          None, True)])
    # La journée est capturée le lendemain à 12 h : complète, définitive.
    f = scores / "soccer" / f"{jour.isoformat()}.json"
    f.write_text(_json.dumps({"response": [_fixture("dom0 FC", "ext0", quand)]}))
    capture = datetime.combine(jour + timedelta(days=1), datetime.min.time(),
                               tzinfo=timezone.utc) + timedelta(hours=12)
    os.utime(f, (capture.timestamp(), capture.timestamp()))
    rm.main(["--db", str(p), "--joues", "--depuis", (jour - timedelta(days=1)).isoformat()])
    out = capsys.readouterr().out
    # Le contrôle de production le lie : ce n'est plus un « absent » à
    # diagnostiquer, c'est un passage de results-update qui manque.
    assert rm.APPARIABLE in out and rm.FOOT_ABSENT not in out
    assert "permettent DÉJÀ de\n    régler" not in out
    assert "permettent DÉJÀ de régler" in out
    assert f"results-update --days {n + 1} --sport soccer" in out


def test_les_noms_affiches_sont_utilises_pas_la_cle_compactee(tmp_path):
    """`events.home` porte la forme COMPACTÉE (« deportivorincon ») ; le
    registre `teams` rend le nom affiché. Comparé compacté, ce match tombe à
    84 — sous le seuil — alors qu'avec les vrais noms il est appariable. La
    sonde doit juger comme `results-update`, qui passe par le registre."""
    d = _verdict(tmp_path, _notre("deportivorincon", "independientesantafe", Q),
                 [_fixture("Dep. Rincón", "Santa Fe", Q)],
                 noms={"deportivorincon": "Deportivo Rincon",
                       "independientesantafe": "Independiente Santa Fe"})
    assert d["verdict"] == rm.APPARIABLE, d


def test_la_classe_de_la_ligue_source_est_reportee(tmp_path):
    """Le féminin : la classe vit dans la LIGUE des deux côtés (« NWSL Women »
    chez la source). Sans report côté source, la sonde verrait une barrière de
    classe là où la production apparie normalement."""
    d = _verdict(tmp_path, _notre("Houston Dash", "Orlando Pride", Q,
                                  ligue="USA - National Womens Soccer League"),
                 [_fixture("Houston Dash", "Orlando Pride", Q, ligue="NWSL Women")])
    assert d["verdict"] == rm.APPARIABLE, d


# ── Ce que la revue adverse du 26/09 a trouvé ────────────────────────

def test_jours_a_couvrir_prend_le_match_a_00_h_30(tmp_path):
    """`--days 10` à 17 h 10 ne prend plus un match à 00 h 30 il y a dix
    jours : `since = maintenant - days`, à l'heure près."""
    maintenant = datetime(2026, 9, 26, 17, 10, tzinfo=timezone.utc)
    depart = datetime(2026, 9, 16, 0, 30, tzinfo=timezone.utc)
    n = rm.jours_a_couvrir(maintenant, [depart, None])
    assert maintenant - timedelta(days=n) <= depart
    assert maintenant - timedelta(days=n - 1) > depart     # et pas plus


def test_la_commande_couvre_le_plus_vieux_manque(tmp_path, monkeypatch, capsys):
    """Un pari de foot il y a 12 jours, fenêtre du pont à 14 : la commande
    imprimée doit le réclamer — `--days 10` le ratait, pour toujours."""
    paris = [(_cle(12, 0), "soccer", _jour(12), "h2h", "home", None, None, True),
             (_cle(2, 1), "soccer", _jour(2), "h2h", "home", None, None, True)]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris, "14", depuis=20)
    assert "results-update --days 13 --sport soccer" in out
    assert "--days 10" not in out


def test_l_elargissement_vise_la_plus_vieille_journee(tmp_path, monkeypatch, capsys):
    paris = [(_cle(5, 0), "soccer", _jour(5), "h2h", "home", None, None, True),
             (_cle(12, 1), "soccer", _jour(12), "h2h", "home", None, None, True)]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris, "2", depuis=20)
    assert "echo 'SCORES_BRIDGE_DAYS=12' >> .env" in out


def test_depuis_exclut_les_matchs_plus_anciens(tmp_path, monkeypatch, capsys):
    paris = [(_cle(15, 0), "soccer", _jour(15), "h2h", "home", None, None, True),
             (_cle(3, 1), "soccer", _jour(3), "h2h", "home", None, ("home", 1, 0), True)]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris, "14")
    assert "Sur 1 paris joués" in out


def test_le_tennis_recoit_sa_propre_fenetre(tmp_path, monkeypatch, capsys):
    paris = [(_cle(8, 0), "tennis", _jour(8), "h2h", "home", None, None, True)]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris, "14")
    assert "results-update --days 9 --sport tennis" in out


def test_une_journee_refusee_donne_la_reprise_payante(tmp_path, monkeypatch, capsys):
    """Refusée ≠ perdue : avec un abonnement payant, effacer la pierre tombale
    la fait redemander. La sonde doit le dire, et ne plus parler de
    « définitif »."""
    jour = (datetime.now(timezone.utc).date() - timedelta(days=6)).isoformat()
    paris = [(_cle(6, 0), "soccer", _jour(6), "h2h", "home", None, None, True)]
    out, dossier = _lancer(
        tmp_path, monkeypatch, capsys, paris, "2",
        retouche=lambda c, d: (d / f"{jour}.refused").write_text("{}"))
    assert rm.REFUSEE in out
    assert f"rm -f {dossier}/*.refused" in out
    assert "echo 'SCORES_BRIDGE_DAYS=6' >> .env" in out
    assert "définitif" not in out


def test_un_clic_non_rattache_est_nomme(tmp_path, monkeypatch, capsys):
    """Le listener écrit d'abord (dedup_key, played_at) seuls. Sans marché, la
    sonde disait « mi-temps » ; la vraie réparation est backfill-played-bets."""
    passe = _cle(3, 0) + "|h2h|home|None"
    futur = _cle(-2, 1) + "|h2h|home|None"

    def nus(c, _d):
        for k in (passe, futur):
            c.execute("INSERT INTO played_bets (dedup_key, played_at) VALUES (?, ?)",
                      (k, "2026-09-20T10:00:00+00:00"))
    out, _ = _lancer(tmp_path, monkeypatch, capsys, [], "14", retouche=nus)
    assert f"{rm.NON_RATTACHE}" in out and rm.MARCHE not in out
    assert "backfill-played-bets" in out
    assert "→ 1 pari(s) dont le match est FINI" in out      # le futur est à venir


def test_un_marche_non_reglable_a_venir_n_est_pas_un_manque(tmp_path):
    assert _classer(tmp_path, market="h2h_h1",
                    start_time="2026-09-28T18:00:00+00:00") == rm.A_VENIR


def test_une_ligne_events_sans_sport_est_nommee_et_reparee(tmp_path, monkeypatch,
                                                           capsys):
    """`repair_events` écrit sport = 'unknown', que results-update ignore. La
    sonde doit le voir, et la commande qu'elle imprime doit VRAIMENT réparer."""
    import subprocess
    from pathlib import Path
    paris = [(_cle(3, 0), "soccer", _jour(3), "h2h", "home", None, None, True)]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris, "14",
                     retouche=lambda c, _d: c.execute(
                         "UPDATE events SET sport = 'unknown'"))
    assert rm.SPORT_INCONNU in out
    db = str(tmp_path / "v.db")
    cmd = rm._commande_sport(db)
    assert cmd in out
    racine = Path(rm.__file__).resolve().parents[1]
    r = subprocess.run(cmd, shell=True, cwd=racine, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    c = sqlite3.connect(db)
    assert c.execute("SELECT sport FROM events").fetchone()[0] == "soccer"


def test_sans_le_pont_le_foot_n_est_pas_juge_sur_ses_fichiers(tmp_path, monkeypatch,
                                                               capsys):
    paris = [(_cle(3, 0), "soccer", _jour(3), "h2h", "home", None, None, True)]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris, "14",
                     env={"SCORES_FOOTBALL_BRIDGE": "0"})
    assert rm.FOOT_SANS_PONT in out and rm.JAMAIS_DEMANDEE not in out
    assert "SCORES_FOOTBALL_BRIDGE=1" in out


def test_sans_cle_le_tennis_n_accuse_pas_les_doubles(tmp_path, monkeypatch, capsys):
    paris = [(_cle(3, 0), "tennis", _jour(3), "h2h", "home", None, None, True)]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris, "14",
                     env={"SCORES_TENNIS_KEY": ""})
    assert rm.TENNIS_SANS_CLE in out and "DOUBLES" not in out


def test_un_commentaire_en_ligne_dans_env_ne_casse_rien(tmp_path, monkeypatch, capsys):
    out, _ = _lancer(tmp_path, monkeypatch, capsys, [],
                     "3          # journées ACHEVÉES")
    assert "SCORES_BRIDGE_DAYS = 3 " in out


def _prolongation(dom, ext, quand, statut, ft, et, buts):
    f = _fixture(dom, ext, quand, statut=statut)
    f["score"] = {"fulltime": {"home": ft[0], "away": ft[1]},
                  "extratime": {"home": et[0], "away": et[1]}}
    f["goals"] = {"home": buts[0], "away": buts[1]}
    return f


def test_une_prolongation_prouvable_est_appariable(tmp_path):
    """`goals == fulltime + extratime` prouve le score à 90 min : la production
    règle ce match (402 cas sur 409), la sonde ne doit pas le dire perdu."""
    d = _verdict(tmp_path, _notre("Clyde", "Stranraer", Q),
                 [_prolongation("Clyde", "Stranraer", Q, "AET", (1, 1), (1, 0), (2, 1))])
    assert d["verdict"] == rm.APPARIABLE, d


def test_une_prolongation_non_prouvable_reste_a_la_main(tmp_path):
    d = _verdict(tmp_path, _notre("Clyde", "Stranraer", Q),
                 [_prolongation("Clyde", "Stranraer", Q, "AET", (3, 4), (0, 1), (3, 4))])
    assert d["verdict"] == rm.PROLONG, d


def test_le_lendemain_charge_par_la_production_rend_appariable(tmp_path):
    """23 h 58 chez nous, 00 h 03 le lendemain chez la source. results-update
    met dans UN lot les résultats de chaque journée qui a un match en attente
    (src/main.py, `fetched.extend`) : si le lendemain en a un, il lie ce
    match. Constaté par la revue du 27/09 en lançant la vraie commande."""
    _jour_source(tmp_path, "2026-09-23",
                 [_fixture("Clyde", "Stranraer", "2026-09-23T00:03:00+00:00")])
    notre = _notre("Clyde", "Stranraer", "2026-09-22T23:58:00+00:00")
    d = rm.diagnostiquer(notre, {}, tmp_path, {},
                         jours={date(2026, 9, 22), date(2026, 9, 23)})
    assert d["verdict"] == rm.APPARIABLE, d


def test_le_lendemain_non_charge_reste_voisin(tmp_path):
    """Le même match, mais le lendemain n'a AUCUN autre match en attente :
    results-update ne charge pas son fichier, et ne lie pas."""
    _jour_source(tmp_path, "2026-09-23",
                 [_fixture("Clyde", "Stranraer", "2026-09-23T00:03:00+00:00")])
    notre = _notre("Clyde", "Stranraer", "2026-09-22T23:58:00+00:00")
    d = rm.diagnostiquer(notre, {}, tmp_path, {}, jours={date(2026, 9, 22)})
    assert d["verdict"] == rm.VOISIN, d


def test_l_ambiguite_se_juge_sur_le_lot_entier(tmp_path):
    """Le même couple 23 h 55 dans le fichier du jour et 00 h 05 dans celui
    du lendemain : sur le lot entier, la garde d'ambiguïté refuse — la sonde
    ne doit pas crier « appariable » en ne regardant qu'un fichier."""
    _jour_source(tmp_path, "2026-09-22",
                 [_fixture("Clyde", "Stranraer", "2026-09-22T23:55:00+00:00")])
    _jour_source(tmp_path, "2026-09-23",
                 [_fixture("Clyde", "Stranraer", "2026-09-23T00:05:00+00:00")])
    d = rm.diagnostiquer(_notre("Clyde", "Stranraer", "2026-09-23T00:00:00+00:00"),
                         {}, tmp_path, {})
    assert d["verdict"] == rm.AMBIGU, d


def test_le_diagnostic_dit_absent_de_bout_en_bout(tmp_path, monkeypatch, capsys):
    """Journée complète, match absent du fichier : le conseil « ABSENTS »."""
    jour = (datetime.now(timezone.utc).date() - timedelta(days=4))
    quand = f"{jour.isoformat()}T15:00:00+00:00"
    paris = [(_cle(4, 0), "soccer", quand, "h2h", "home", None, None, True)]

    def complet(_c, d):
        f = d / f"{jour.isoformat()}.json"
        f.write_text(_json.dumps({"response": [_fixture("Arsenal", "Chelsea", quand)]}))
        t = (datetime.combine(jour + timedelta(days=1), datetime.min.time(),
                              tzinfo=timezone.utc) + timedelta(hours=12)).timestamp()
        os.utime(f, (t, t))
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris, "14", retouche=complet)
    assert "ABSENTS de la source" in out


# ── TOUTES les détections (le défaut) ────────────────────────────────

def _det(**kw):
    base = dict(event_key="202609241800::a__vs__b", sport="soccer", league="L1",
                home="a", away="b", start_time="2026-09-24T18:00:00+00:00",
                n_det=2, has_result=0, has_scores=0, league_bf=None)
    base.update(kw)
    return base


def _classer_det(tmp_path, reglees=None, noms=None, pont=True, cle=True, **kw):
    return rm.classer_detection(_det(**kw), MAINTENANT, tmp_path, 7, FINAL, pont,
                                cle, reglees or {}, noms or {}, {})


def _t(iso):
    return datetime.fromisoformat(iso)


def test_detection_reglee_avec_ou_sans_score(tmp_path):
    assert _classer_det(tmp_path, has_result=1, has_scores=1) == rm.REGLE
    assert _classer_det(tmp_path, has_result=1, has_scores=0) == rm.REGLE_SANS_SCORE


def test_detection_a_venir_puis_les_sports(tmp_path):
    assert _classer_det(tmp_path, start_time="2026-09-27T18:00:00+00:00") == rm.A_VENIR
    assert _classer_det(tmp_path, sport="unknown") == rm.SPORT_INCONNU
    assert _classer_det(tmp_path, sport="basketball") == rm.SANS_SOURCE
    assert _classer_det(tmp_path, pont=False) == rm.FOOT_SANS_PONT
    assert _classer_det(tmp_path, start_time="2026-09-26T08:00:00+00:00") == rm.FOOT_DU_JOUR
    assert _classer_det(tmp_path) == rm.JAMAIS_DEMANDEE


def test_la_cle_voisine_exige_meme_jour_et_meme_ligue(tmp_path):
    """Horaire révisé : la clé neuve a son résultat, l'ancienne non. Mêmes
    équipes, même jour, MÊME ligue — dans un sens comme dans l'autre. Sans la
    ligue, un match féminin (classe portée par la seule ligue, noms compactés
    identiques) passerait pour celui des seniors."""
    r = _det(league="L1")
    j = date(2026, 9, 24)
    t = [_t("2026-09-24T19:00:00+00:00")]
    assert rm.ecart_cle_voisine(r, {("a", "b", j, "L1"): t}) == 60
    assert rm.ecart_cle_voisine(r, {("b", "a", j, "L1"): t}) == 60
    assert rm.ecart_cle_voisine(r, {("a", "b", date(2026, 9, 23), "L1"): t}) is None
    assert rm.ecart_cle_voisine(r, {("a", "b", j, "L1 Women"): t}) is None
    assert rm.ecart_cle_voisine(_det(league=""), {("a", "b", j, ""): t}) is None


def test_tennis_a_moins_de_12_h_se_relie_au_dela_non(tmp_path):
    """Au tennis, `bind_results` relie une clé à un résultat à 12 h près : une
    clé voisine réglée à 3 h sera reliée au prochain passage, pas à 13 h."""
    j = date(2026, 9, 24)
    pres = {("a", "b", j, "L1"): [_t("2026-09-24T21:00:00+00:00")]}
    loin = {("a", "b", j, "L1"): [_t("2026-09-24T00:30:00+00:00")]}
    assert _classer_det(tmp_path, sport="tennis", reglees=pres) == rm.TENNIS_RELIER
    assert _classer_det(tmp_path, sport="tennis", reglees=loin) == rm.AUTRE_CLE


def test_foot_la_cle_voisine_se_juge_apres_la_production(tmp_path):
    """Football : si les fichiers du pont lient la clé orpheline (écart dans
    la tolérance), c'est un passage de results-update qui manque, pas un
    défaut de code. AUTRE_CLE seulement quand la production échoue."""
    j = date(2026, 9, 24)
    _complet(tmp_path, j, [_fixture("A", "B", "2026-09-24T18:05:00+00:00")])
    reglees = {("a", "b", j, "L1"): [_t("2026-09-24T18:05:00+00:00")]}
    res = rm.analyser_detections([_det()], MAINTENANT, tmp_path, 7, FINAL, True,
                                 True, reglees, {})
    assert res["comptes"][rm.APPARIABLE] == 1
    loin = tmp_path / "loin"
    loin.mkdir()
    _complet(loin, j, [_fixture("A", "B", "2026-09-24T19:30:00+00:00")])
    reglees = {("a", "b", j, "L1"): [_t("2026-09-24T19:30:00+00:00")]}
    res = rm.analyser_detections([_det()], MAINTENANT, loin, 7, FINAL, True, True,
                                 reglees, {})
    assert res["comptes"][rm.AUTRE_CLE] == 1


def test_un_double_se_lit_sur_le_nom_affiche(tmp_path):
    noms = {"bolellisvavassoria": "Bolelli S / Vavassori A"}
    assert _classer_det(tmp_path, sport="tennis", home="bolellisvavassoria",
                        noms=noms) == rm.DOUBLE
    assert _classer_det(tmp_path, sport="tennis", home="sinnerj") == rm.TENNIS_SIMPLE
    assert _classer_det(tmp_path, sport="tennis", cle=False) == rm.TENNIS_SANS_CLE


def _base_detections(tmp_path):
    """Une base au VRAI schéma (`Storage`) : la sonde lit la même base que la
    production, pas un schéma recopié."""
    from src.storage import Storage
    p = tmp_path / "v.db"
    Storage(str(p))
    return p


def _ev(c, k, t, sport="soccer", league="L1", h="a", a="b", n_vb=1):
    c.execute("INSERT INTO events (event_key, sport, league, home, away, start_time)"
              " VALUES (?,?,?,?,?,?)", (k, sport, league, h, a, t))
    for _ in range(n_vb):
        c.execute("INSERT INTO value_bets (event_key, book, market, outcome_label,"
                  " odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct, detected_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (k, "unibet_be", "h2h", "home", 2.1, .5, 2.0, 5, 1, t))


def test_la_population_est_celle_de_la_production(tmp_path):
    """Les matchs finis sans résultat de la sonde sont EXACTEMENT ceux que
    `results-update` réclame (`Storage.events_awaiting_result`)."""
    from src.storage import Storage
    p = _base_detections(tmp_path)
    c = sqlite3.connect(str(p))
    _ev(c, "k1", "2026-09-20T15:00:00+00:00", n_vb=3)
    _ev(c, "k2", "2026-09-21T15:00:00+00:00", h="c", a="d")
    _ev(c, "k3", "2026-09-22T15:00:00+00:00", h="e", a="f", n_vb=0)   # sans value bet
    _ev(c, "k4", "2026-09-10T15:00:00+00:00", h="g", a="h")           # avant --depuis
    _ev(c, "k5", "2026-09-23T15:00:00+00:00", h="i", a="j")
    c.execute("INSERT INTO results VALUES ('k5','home',1,0,'t','2026-09-24')")
    c.commit()
    c.close()
    rows, _noms, reglees = rm.charger_detections(str(p), date(2026, 9, 15))
    depuis = datetime(2026, 9, 15, tzinfo=timezone.utc)
    attendu = {r["event_key"] for r in Storage(str(p)).events_awaiting_result(
        depuis, MAINTENANT - rm.GRACE)}
    sonde = {r["event_key"] for r in rows if not r["has_result"]}
    assert sonde == attendu == {"k1", "k2"}
    assert {r["event_key"]: r["n_det"] for r in rows}["k1"] == 3
    assert reglees[("i", "j", date(2026, 9, 23), "L1")] == [
        datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)]


def test_la_ligue_de_bet_features_est_lue(tmp_path):
    p = _base_detections(tmp_path)
    c = sqlite3.connect(str(p))
    _ev(c, "k1", "2026-09-20T15:00:00+00:00", league="")
    c.execute("INSERT INTO bet_features (value_bet_id, detected_at, event_key, sport,"
              " league, book, market, outcome_label, odd_taken, fair_odd, ev_pct)"
              " VALUES (1,'x','k1','soccer','Sweden - Damallsvenskan','u','h2h',"
              "'home',2.1,2.0,5)")
    c.commit()
    c.close()
    rows, _n, _r = rm.charger_detections(str(p), date(2026, 9, 15))
    assert rows[0]["league_bf"] == "Sweden - Damallsvenskan"


def test_une_base_sans_bet_features_se_lit_quand_meme(tmp_path):
    p = tmp_path / "v.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
        CREATE TABLE events (event_key TEXT PRIMARY KEY, sport TEXT, league TEXT,
            home TEXT, away TEXT, start_time TEXT);
        CREATE TABLE value_bets (id INTEGER PRIMARY KEY, event_key TEXT,
            market TEXT);
        CREATE TABLE results (event_key TEXT PRIMARY KEY, winner TEXT,
            home_score REAL, away_score REAL, source TEXT, settled_at TEXT);
        INSERT INTO events VALUES ('k1','soccer','L','a','b','2026-09-20T15:00:00+00:00');
        INSERT INTO value_bets (event_key, market) VALUES ('k1', 'h2h');
    """)
    c.commit()
    c.close()
    rows, noms, _r = rm.charger_detections(str(p), date(2026, 9, 15))
    assert len(rows) == 1 and rows[0]["league_bf"] is None and noms == {}


def test_une_ambiguite_est_nommee(tmp_path):
    """Deux matchs de la source quasi identiques à 10 min près : `match_event`
    refuse de choisir. La sonde doit le dire, pas crier « appariable »."""
    d = _verdict(tmp_path, _notre("Clyde", "Stranraer", Q),
                 [_fixture("Clyde", "Stranraer", Q),
                  _fixture("Clyde FC", "Stranraer", "2026-09-22T18:50:00+00:00")])
    assert d["verdict"] == rm.AMBIGU, d


def test_un_match_termine_sans_score_n_est_pas_une_contradiction(tmp_path):
    f = _fixture("Clyde", "Stranraer", Q)
    f["score"]["fulltime"] = {"home": None, "away": None}
    d = _verdict(tmp_path, _notre("Clyde", "Stranraer", Q), [f])
    assert d["verdict"] == rm.SANS_SCORE_SOURCE, d


def test_la_barriere_de_classe_dit_quelle_classe(tmp_path):
    """« Rangers B » (réserve) contre « Rangers U21 » (jeunes) : le conflit
    nommé dit quelle correction le lèverait."""
    d = _verdict(tmp_path, _notre("rangersxreserve", "clyde", Q),
                 [_fixture("Clyde", "Rangers U21", Q)],
                 noms={"rangersxreserve": "Rangers B", "clyde": "Clyde"})
    assert d["verdict"] == rm.CLASSE, d
    assert d["cand"]["conflits"] == [("réserve", "jeunes")], d


def test_la_recherche_rapide_ne_perd_aucun_candidat_plausible():
    """`_proches` (élagué à 70) et `_flou` (plancher 40) contre la référence
    `_sim_sans_classe`, sur des noms réels : aucun nom à 70 n'est perdu, et
    toute valeur à 40 ou plus est exacte."""
    noms = ["Vittsjö W", "Växjö W", "Deportivo La Coruña II", "Deportivo Fabril",
            "Athletic Club II", "Rangers U21", "Clyde", "Vancouver FC",
            "York United", "Inter Toronto", "Juventus", "Atalanta", "Amatitlan",
            "Juventud Copalera", "Dep. Maipu", "Deportivo Maipu", "Os", "AZ W",
            "Shijiazhuang Gongfu", "Yokohama FC", "YSCC Yokohama", "Masar",
            "MPS", "Proxy", "SexyPöxyt", "Carrick Rangers", "Ballymacash Rangers"]
    formes = [rm._pour_flou(n) for n in noms]
    for q in formes:
        proches = rm._proches(q, formes)
        for f in formes:
            ref = max(rm.fuzz.token_set_ratio(q, f), rm.fuzz.partial_ratio(q, f))
            assert (ref >= rm.SEUIL_BRUT) == (f in proches), (q, f, ref)
            if ref >= rm.PLANCHER_CAMP:
                assert rm._flou(q, f) == ref, (q, f)


def _lancer_det(tmp_path, monkeypatch, capsys, construire, *extra, depuis=10):
    scores = tmp_path / "scores"
    (scores / "soccer").mkdir(parents=True)
    monkeypatch.setattr(rm, "load_env_file", lambda *a, **k: 0)
    monkeypatch.setenv("SCORES_INGEST_DIR", str(scores))
    monkeypatch.setenv("SCORES_BRIDGE_DAYS", "14")
    monkeypatch.setenv("SCORES_FOOTBALL_BRIDGE", "1")
    monkeypatch.setenv("SCORES_TENNIS_KEY", "cle")
    p = _base_detections(tmp_path)
    c = sqlite3.connect(str(p))
    construire(c, scores / "soccer")
    c.commit()
    c.close()
    debut = (datetime.now(timezone.utc).date() - timedelta(days=depuis)).isoformat()
    rm.main(["--db", str(p), "--depuis", debut, *extra])
    return capsys.readouterr().out


def _complet(dossier, jour, fixtures):
    f = dossier / f"{jour.isoformat()}.json"
    f.write_text(_json.dumps({"response": fixtures}))
    t = (datetime.combine(jour + timedelta(days=1), datetime.min.time(),
                          tzinfo=timezone.utc) + timedelta(hours=12)).timestamp()
    os.utime(f, (t, t))


def test_les_detections_de_bout_en_bout(tmp_path, monkeypatch, capsys):
    jour = datetime.now(timezone.utc).date() - timedelta(days=4)
    quand = f"{jour.isoformat()}T15:00:00+00:00"
    plus_tard = f"{jour.isoformat()}T15:45:00+00:00"

    def construire(c, d):
        _ev(c, "k1", quand, h="arsenal", a="chelsea", n_vb=3)
        c.execute("INSERT INTO results VALUES ('k1','home',1,0,'t','x')")
        _ev(c, "k2", plus_tard, h="arsenal", a="chelsea", n_vb=2)      # autre clé
        _ev(c, "k3", quand, h="kontu", a="lps")                         # absent
        _ev(c, "k4", quand, league="Sweden - Damallsvenskan", h="vittsjo",
            a="vaxjo", n_vb=4)                                          # classe
        _ev(c, "k5", quand, sport="basketball", h="lakers", a="celtics")
        _complet(d, jour, [_fixture("Arsenal", "Chelsea", quand),
                           _fixture("Vittsjö W", "Växjö W", quand,
                                    ligue="Damallsvenskan")])
    out = _lancer_det(tmp_path, monkeypatch, capsys, construire)
    assert "DÉTECTIONS SANS RÉSULTAT" in out
    assert "Sur 5 matchs détectés (11 détections)" in out
    assert "→ 4 match(s) FINIS sans résultat, portant 8 détections" in out
    assert "COUVERTURE DES MATCHS FINIS, PAR SPORT" in out
    assert "basketball" in out and "aucune source" in out
    assert rm.AUTRE_CLE in out and "orpheline" in out
    assert "classes (nous → source) : aucune → féminin : 1" in out
    assert "ABSENTS de la source" in out


def test_la_ligue_perdue_est_chiffree(tmp_path, monkeypatch, capsys):
    """`events.league` vide, `bet_features` connaît la ligue féminine : la
    sonde dit combien la production rapprocherait de plus avec elle."""
    jour = datetime.now(timezone.utc).date() - timedelta(days=4)
    quand = f"{jour.isoformat()}T15:00:00+00:00"

    def construire(c, d):
        _ev(c, "k1", quand, league="", h="vittsjo", a="vaxjo")
        c.execute("INSERT INTO bet_features (value_bet_id, detected_at, event_key,"
                  " sport, league, book, market, outcome_label, odd_taken,"
                  " fair_odd, ev_pct) VALUES (1,'x','k1','soccer',"
                  "'Sweden - Women Damallsvenskan','u','h2h','home',2.1,2.0,5)")
        _complet(d, jour, [_fixture("Vittsjö W", "Växjö W", quand,
                                    ligue="Damallsvenskan")])
    out = _lancer_det(tmp_path, monkeypatch, capsys, construire)
    assert "1 de ces matchs n'ont pas de ligue" in out
    assert f"(du {jour.isoformat()} au {jour.isoformat()})" in out
    assert "ligue pour 1 ;" in out
    assert "rapprocherait 1 de plus" in out


def test_les_exemples_sont_bornes_sauf_avec_lister(tmp_path, monkeypatch, capsys):
    jour = datetime.now(timezone.utc).date() - timedelta(days=4)
    quand = f"{jour.isoformat()}T15:00:00+00:00"

    def construire(c, d):
        for i in range(rm.EXEMPLES + 3):
            _ev(c, f"k{i}", quand, h=f"absent{i}", a=f"nulle{i}")
        _complet(d, jour, [_fixture("Arsenal", "Chelsea", quand)])
    out = _lancer_det(tmp_path, monkeypatch, capsys, construire)
    assert "… et 3 autres (--lister pour tout voir)" in out
    tmp2 = tmp_path / "b"
    tmp2.mkdir()
    out = _lancer_det(tmp2, monkeypatch, capsys, construire, "--lister")
    assert "autres (--lister" not in out
    assert all(f"Absent{i}" in out for i in range(rm.EXEMPLES + 3))


def test_les_exemples_montres_sont_les_plus_lourds(tmp_path, monkeypatch, capsys):
    """Un match qui porte beaucoup de détections pèse plus sur le ROI : c'est
    lui qu'on montre, pas le premier venu."""
    jour = datetime.now(timezone.utc).date() - timedelta(days=4)
    quand = f"{jour.isoformat()}T15:00:00+00:00"

    def construire(c, d):
        for i in range(rm.EXEMPLES + 3):
            _ev(c, f"k{i}", quand, h=f"absent{i}", a=f"nulle{i}",
                n_vb=9 if i == rm.EXEMPLES + 2 else 1)
        _complet(d, jour, [_fixture("Arsenal", "Chelsea", quand)])
    out = _lancer_det(tmp_path, monkeypatch, capsys, construire)
    bloc = out.split(f"── {rm.ABSENT}", 1)[1].split("… et", 1)[0]
    assert bloc.count(" dét.\n") == rm.EXEMPLES
    assert f"Absent{rm.EXEMPLES + 2} - Nulle{rm.EXEMPLES + 2}" in bloc


def test_la_couverture_ignore_le_foot_du_jour(tmp_path, capsys):
    """Un match d'aujourd'hui n'a pas encore pu être capturé : le compter
    dans les « finis » ferait baisser la couverture à tort."""
    rows = [_det(event_key="k1", has_result=1, has_scores=1),
            _det(event_key="k2", home="psg", away="om",
                 start_time="2026-09-26T08:00:00+00:00")]
    res = rm.analyser_detections(rows, MAINTENANT, tmp_path, 7, FINAL, True, True,
                                 {}, {})
    assert res["comptes"][rm.FOOT_DU_JOUR] == 1
    rm.imprimer_detections(res, date(2026, 9, 20), tmp_path, 7, "t", MAINTENANT,
                           False, "v.db", True, True)
    ligne = next(l for l in capsys.readouterr().out.splitlines()
                 if l.strip().startswith("soccer"))
    assert "1 / 1" in ligne, ligne


def test_la_source_oublie_les_journees_depassees(tmp_path):
    """Quatre mois de fichiers ouverts à la fois pèseraient plus d'un Go sur
    la VM du daemon : on n'en garde que la fenêtre utile."""
    src = rm.SourceFoot(tmp_path)
    for n in range(10):
        src.fenetre(date(2026, 9, 1) + timedelta(days=n))
    src.oublier(date(2026, 9, 9))
    assert src.journees_ouvertes() == 3          # le 9, le 10 et le 11
    assert all(d >= date(2026, 9, 9) for d in src._fen)


def test_les_detections_ne_gardent_qu_une_fenetre(tmp_path, monkeypatch):
    """De bout en bout : trente journées diagnostiquées, jamais plus de
    quatre ouvertes à la fois."""
    ouvertes = []
    vrai = rm.diagnostiquer

    def espion(r, noms, dossier, cache, *args):
        d = vrai(r, noms, dossier, cache, *args)
        ouvertes.append(cache[dossier].journees_ouvertes())
        return d
    monkeypatch.setattr(rm, "diagnostiquer", espion)
    for n in range(30):
        j = date(2026, 8, 20) + timedelta(days=n)
        _complet(tmp_path, j, [_fixture("Arsenal", "Chelsea", f"{j}T15:00:00+00:00")])
    rows = [_det(event_key=f"k{n}", home=f"x{n}", away=f"y{n}",
                 start_time=f"{date(2026, 8, 20) + timedelta(days=n)}T15:00:00+00:00")
            for n in range(30)]
    rm.analyser_detections(rows, MAINTENANT, tmp_path, 60, FINAL, True, True,
                           {}, {})
    assert len(ouvertes) == 30 and max(ouvertes) <= 4, ouvertes


def test_hors_tolerance_un_fragment_ne_suffit_plus(tmp_path):
    """Cas réel : « Kossa FC - Marist Fire » tenu pour « Os - Førde » à 11 h
    d'écart, parce que « os » est un fragment de « kossa » (100 en score de
    production). Hors tolérance, l'heure ne retient plus ce faux ami."""
    d = _verdict(tmp_path, _notre("Kossa FC", "Marist Fire", Q),
                 [_fixture("Os", "Førde", "2026-09-23T05:45:00+00:00")])
    assert d["verdict"] == rm.ABSENT, d


def test_un_fragment_n_est_pas_un_camp_identique(tmp_path):
    """Cas réel : « SK Brann W - Aalesunds W » et « Bra - Fezzanese » —
    « bra » vaut 100 contre « brann » en score de production, mais un club
    renommé garde son autre nom EN ENTIER."""
    d = _verdict(tmp_path, _notre("SK Brann W", "Aalesunds W", Q),
                 [_fixture("Bra", "Fezzanese", Q)])
    assert d["verdict"] == rm.ABSENT, d


def test_un_match_decale_de_trois_heures_reste_un_horaire(tmp_path):
    """Cas réel, amicaux de juillet : mêmes noms, trois heures d'écart."""
    d = _verdict(tmp_path, _notre("BG Pathum United", "Buriram United", Q),
                 [_fixture("BG Pathum United", "Buriram United",
                           "2026-09-22T21:45:00+00:00")])
    assert d["verdict"] == rm.HORAIRE and round(d["cand"]["dt"]) == 180, d


def test_les_tirs_au_but_directs_sont_reconnus(tmp_path):
    """Pas de prolongation saisie, buts = temps réglementaire : la forme d'un
    match allé directement aux tirs au but. La sonde le compte à part — c'est
    une règle de production à trancher sur ce chiffre."""
    d = _verdict(tmp_path, _notre("Bromley FC", "Reading", Q),
                 [_prolongation("Bromley", "Reading", Q, "PEN", (1, 1),
                                (None, None), (1, 1))])
    assert d["verdict"] == rm.PROLONG, d
    assert d["cand"]["prolong"] == rm.TAB_DIRECTS
    d = _verdict(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path,
                 _notre("Bromley FC", "Reading", Q),
                 [_prolongation("Bromley", "Reading", Q, "AET", (2, 2),
                                (None, None), (3, 2))])
    assert d["cand"]["prolong"] == rm.PROLONG_SANS_DETAIL


def test_le_fragment_est_ecarte_des_la_sortie_de_tolerance(tmp_path):
    """Même faux ami qu'à 11 h, mais à une heure : le seuil « en entier »
    s'applique dès que l'heure ne garantit plus rien, pas seulement au loin."""
    d = _verdict(tmp_path, _notre("Kossa FC", "Marist Fire", Q),
                 [_fixture("Os", "Førde", "2026-09-22T19:45:00+00:00")])
    assert d["verdict"] == rm.ABSENT, d


def test_au_loin_il_faut_des_noms_quasi_identiques(tmp_path):
    """« Siegburger 04 » / « Siegburger SV » (87 en entier) passe à trois
    heures, pas à huit : au-delà de six heures, seul un match reporté garde
    ses noms à l'identique."""
    pres = _verdict(tmp_path, _notre("Rot Weiss Koblenz", "Siegburger 04", Q),
                    [_fixture("Rot Weiss Koblenz", "Siegburger SV",
                              "2026-09-22T21:45:00+00:00")])
    assert pres["verdict"] == rm.HORAIRE, pres
    loin = tmp_path / "loin"
    loin.mkdir()
    d = _verdict(loin, _notre("Rot Weiss Koblenz", "Siegburger 04", Q),
                 [_fixture("Rot Weiss Koblenz", "Siegburger SV",
                           "2026-09-23T02:45:00+00:00")])
    assert d["verdict"] == rm.ABSENT, d


def test_une_prolongation_chiffree_n_est_pas_un_tab_direct(tmp_path):
    """Le cas du Schweizer Cup : `fulltime` 3-4 porte DÉJÀ la prolongation
    (0-1), buts 3-4. Buts = `fulltime`, mais une prolongation est saisie :
    ce n'est pas un match allé directement aux tirs au but."""
    d = _verdict(tmp_path, _notre("Bromley FC", "Reading", Q),
                 [_prolongation("Bromley", "Reading", Q, "AET", (3, 4),
                                (0, 1), (3, 4))])
    assert d["verdict"] == rm.PROLONG, d
    assert d["cand"]["prolong"] == rm.PROLONG_SANS_DETAIL


def test_un_club_renomme_se_reconnait_aussi_a_l_envers(tmp_path):
    """La source inverse domicile et extérieur : le camp identique se juge
    dans l'orientation retenue, pas dans l'ordre des colonnes."""
    d = _verdict(tmp_path, _notre("Vancouver FC", "Inter Toronto", Q),
                 [_fixture("York United", "Vancouver FC", Q)])
    assert d["verdict"] == rm.NOMS, d



# ── Ce que la revue adverse du 27/09 a trouvé ────────────────────────

def test_un_fichier_illisible_est_une_panne_generale(tmp_path, monkeypatch, capsys):
    """`BridgedFootballScores` lève sur un JSON illisible et results-update met
    alors TOUT le football en panne. Le lire comme une journée vide ferait dire
    « absent, rien à corriger » — le contraire de la vérité."""
    jour = datetime.now(timezone.utc).date() - timedelta(days=4)
    quand = f"{jour.isoformat()}T15:00:00+00:00"

    def construire(c, d):
        _ev(c, "k1", quand, h="arsenal", a="chelsea")
        f = d / f"{jour.isoformat()}.json"
        f.write_text('{"response": [ {"fixture": ')           # tronqué
    out = _lancer_det(tmp_path, monkeypatch, capsys, construire)
    assert rm.FICHIER_ILLISIBLE in out and rm.ABSENT not in out
    assert f"rm -f {tmp_path / 'scores' / 'soccer'}/{jour.isoformat()}.json" in out


def test_un_fichier_lisible_mais_mal_forme_est_illisible(tmp_path):
    """Une liste au lieu d'un objet : `parse_apifootball_results` lève, la
    production aussi. La sonde rejoue exactement sa lecture."""
    j = date(2026, 9, 24)
    f = tmp_path / f"{j.isoformat()}.json"
    f.write_text("[]")
    t = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc).timestamp()
    os.utime(f, (t, t))
    assert rm.etat_journee(j, tmp_path, MAINTENANT, 7, FINAL) == rm.FICHIER_ILLISIBLE


def test_le_memo_distingue_les_journees(tmp_path):
    """Deux journées dans le même passage : l'une complète, l'autre jamais
    récupérée. Un mémo qui garderait un seul état les confondrait toutes."""
    _complet(tmp_path, date(2026, 9, 23), [])
    rows = [_det(event_key="k1", start_time="2026-09-23T15:00:00+00:00"),
            _det(event_key="k2", home="c", away="d",
                 start_time="2026-09-24T15:00:00+00:00")]
    res = rm.analyser_detections(rows, MAINTENANT, tmp_path, 7, FINAL, True, True,
                                 {}, {})
    assert res["comptes"][rm.FOOT_ABSENT] == 1
    assert res["comptes"][rm.JAMAIS_DEMANDEE] == 1


def test_la_grace_de_deux_heures(tmp_path):
    """Un match commencé il y a une heure n'est pas encore réclamé par
    results-update (`until = maintenant - 2 h`) : ce n'est pas un manque."""
    assert _classer_det(tmp_path, sport="tennis",
                        start_time="2026-09-26T11:00:00+00:00") == rm.A_VENIR
    assert _classer_det(tmp_path, sport="tennis",
                        start_time="2026-09-26T09:30:00+00:00") == rm.TENNIS_SIMPLE


def test_la_couverture_compte_les_detections_reglables(tmp_path, capsys):
    """Un résultat sans score règle les 1X2 du match, jamais ses totaux ; un
    match à venir n'entre pas dans les « finis »."""
    rows = [_det(event_key="k1", has_result=1, has_scores=0, n_det=5, n_h2h=2),
            _det(event_key="k2", home="c", away="d", n_det=3, n_h2h=3),
            _det(event_key="k3", home="e", away="f", n_det=4, n_h2h=4,
                 start_time="2026-09-27T15:00:00+00:00")]
    res = rm.analyser_detections(rows, MAINTENANT, tmp_path, 7, FINAL, True, True,
                                 {}, {})
    rm.imprimer_detections(res, date(2026, 9, 20), tmp_path, 7, "t", MAINTENANT,
                           False, "v.db", True, True)
    ligne = next(l for l in capsys.readouterr().out.splitlines()
                 if l.strip().startswith("soccer"))
    assert "1 / 2" in ligne and "2 / 8" in ligne, ligne


def test_les_mi_temps_ne_comptent_pas_dans_les_detections(tmp_path):
    p = _base_detections(tmp_path)
    c = sqlite3.connect(str(p))
    _ev(c, "k1", "2026-09-20T15:00:00+00:00", n_vb=2)
    c.execute("INSERT INTO value_bets (event_key, book, market, outcome_label,"
              " odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct, detected_at)"
              " VALUES ('k1','u','h2h_h1','home',2.1,.5,2.0,5,1,'x')")
    c.execute("INSERT INTO value_bets (event_key, book, market, outcome_label,"
              " odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct, detected_at)"
              " VALUES ('k1','u','totals','over 2.5',2.1,.5,2.0,5,1,'x')")
    c.execute("INSERT INTO results VALUES ('k1','home',NULL,NULL,'t','x')")
    c.commit()
    c.close()
    rows, _n, _r = rm.charger_detections(str(p), date(2026, 9, 15))
    assert (rows[0]["n_det"], rows[0]["n_h2h"]) == (3, 2)
    assert rows[0]["has_result"] and not rows[0]["has_scores"]


def test_la_ligue_de_bet_features_ignore_les_vides(tmp_path):
    p = _base_detections(tmp_path)
    c = sqlite3.connect(str(p))
    _ev(c, "k1", "2026-09-20T15:00:00+00:00", league="")
    for vb, lg in ((1, ""), (2, "Norway - Toppserien")):
        c.execute("INSERT INTO bet_features (value_bet_id, detected_at, event_key,"
                  " sport, league, book, market, outcome_label, odd_taken,"
                  " fair_odd, ev_pct) VALUES (?,'x','k1','soccer',?,'u','h2h',"
                  "'home',2.1,2.0,5)", (vb, lg))
    c.commit()
    c.close()
    rows, _n, _r = rm.charger_detections(str(p), date(2026, 9, 15))
    assert rows[0]["league_bf"] == "Norway - Toppserien"
    assert rows[0]["sport_bf"] == "soccer"


def test_la_ligue_rendue_ne_compte_que_ce_qui_se_lie(tmp_path):
    """La ligue de `bet_features` ne compte que si elle rend le match
    appariable — et seulement pour un match SANS ligue dans `events`."""
    j = date(2026, 9, 24)
    _complet(tmp_path, j, [_fixture("Vittsjö W", "Växjö W", "2026-09-24T18:00:00+00:00",
                                    ligue="Damallsvenskan")])
    rows = [_det(event_key="k1", home="vittsjo", away="vaxjo", league="",
                 league_bf="Sweden - Women Damallsvenskan"),
            _det(event_key="k2", home="kontu", away="lps", league="",
                 league_bf="Sweden - Women Damallsvenskan"),
            _det(event_key="k3", home="vittsjo", away="vaxjo", league="L1",
                 league_bf="Sweden - Women Damallsvenskan",
                 start_time="2026-09-24T18:01:00+00:00")]
    res = rm.analyser_detections(rows, MAINTENANT, tmp_path, 7, FINAL, True, True,
                                 {}, {})
    assert res["ligue_rendue"] == 1


def test_la_recherche_trouve_tous_les_candidats_d_une_journee_chargee():
    """`process.extract` rend 5 noms par défaut : sans `limit=None`, un samedi
    à dix « Rangers » perdrait des candidats."""
    choix = [rm._pour_flou(n) for n in (
        "Rangers", "Rangers U21", "Rangers W", "Queens Park Rangers",
        "Berwick Rangers", "Carrick Rangers", "Forfar Rangers", "Rangers FC Kuopio",
        "Bangor Rangers", "Ranger's")]
    assert len(rm._proches(rm._pour_flou("Rangers"), choix)) == len(set(choix))


def test_la_classe_de_ligue_des_deux_cotes_dans_l_explication(tmp_path):
    """Un match féminin décalé d'une heure : la classe vient de la LIGUE des
    deux côtés. Sans elle, la sonde dirait « barrière de classe » au lieu
    d'« horaire »."""
    d = _verdict(tmp_path, _notre("Houston Dash", "Orlando Pride", Q,
                                  ligue="USA - National Womens Soccer League"),
                 [_fixture("Houston Dash", "Orlando Pride",
                           "2026-09-22T19:45:00+00:00", ligue="NWSL Women")])
    assert d["verdict"] == rm.HORAIRE, d


def test_le_fichier_de_la_veille_est_lu(tmp_path):
    """00 h 05 chez nous, 23 h 58 la veille chez la source."""
    _jour_source(tmp_path, "2026-09-21",
                 [_fixture("Clyde", "Stranraer", "2026-09-21T23:58:00+00:00")])
    notre = _notre("Clyde", "Stranraer", "2026-09-22T00:05:00+00:00")
    assert rm.diagnostiquer(notre, {}, tmp_path, {})["verdict"] == rm.APPARIABLE
    loin = tmp_path / "b"
    loin.mkdir()
    _jour_source(loin, "2026-09-21",
                 [_fixture("Clyde", "Stranraer", "2026-09-21T23:58:00+00:00")])
    d = rm.diagnostiquer(notre, {}, loin, {}, jours={date(2026, 9, 22)})
    assert d["verdict"] == rm.VOISIN, d


@pytest.mark.parametrize("u, camps, dt, entiers, attendu", [
    (69.9, (70, 69.8), 0, (100, 100), False),     # sous le plancher
    (70, (75, 65), 0, (100, 65), True),           # camp identique (ratio 100)
    (70, (75, 65), 0, (94.9, 65), False),         # presque identique : non
    (75, (75, 75), 0, (70, 70), True),            # deux camps proches
    (74.9, (75, 74.8), 0, (70, 70), False),       # un camp sous 75
    (75, (75, 75), 120, (70, 70), True),          # à 2 h pile, montré
    (75, (75, 75), 121, (70, 70), False),         # au-delà, non
    (90, (90, 90), 11, (60, 60), True),           # hors tolérance, 60 en entier
    (90, (90, 90), 11, (59.9, 100), False),       # un camp sous 60
    (90, (90, 90), 360, (60, 60), True),          # 6 h pile
    (90, (90, 90), 361, (89.9, 100), False),      # au-delà de 6 h, il faut 90
    (90, (90, 90), 361, (90, 90), True),
])
def test_les_seuils_de_plausibilite(u, camps, dt, entiers, attendu):
    assert rm._plausible(u, camps, dt, entiers) is attendu


def test_un_aet_n_est_jamais_un_tir_au_but_direct(tmp_path):
    """Décidé en prolongation, un AET en a joué une — même si la source ne
    l'a pas chiffrée et que buts = temps réglementaire."""
    d = _verdict(tmp_path, _notre("Bromley FC", "Reading", Q),
                 [_prolongation("Bromley", "Reading", Q, "AET", (1, 1),
                                (None, None), (1, 1))])
    assert d["cand"]["prolong"] == rm.PROLONG_SANS_DETAIL


def _journee_classee(tmp_path, capsys, lignes, jours_pont=7):
    """Imprime les conseils pour des matchs déjà classés."""
    classes = [(_det(event_key=f"k{i}", start_time=t), c)
               for i, (t, c) in enumerate(lignes)]
    rm._conseils(classes, Counter(c for _r, c in classes), Counter(), tmp_path,
                 jours_pont, MAINTENANT, "v.db", "match(s)")
    return capsys.readouterr().out


from collections import Counter  # noqa: E402


def test_une_seule_commande_de_fenetre_et_jamais_plus_petite(tmp_path, capsys):
    """Hors fenêtre à 9 jours et refusée à 12 : UNE commande, à 12. Deux
    `sed` successifs laisseraient le second écraser le premier."""
    out = _journee_classee(tmp_path, capsys, [
        ("2026-09-17T15:00:00+00:00", rm.HORS_FENETRE),
        ("2026-09-14T15:00:00+00:00", rm.REFUSEE)])
    assert out.count("echo 'SCORES_BRIDGE_DAYS=") == 1
    assert "echo 'SCORES_BRIDGE_DAYS=12' >> .env" in out
    assert f"rm -f {tmp_path}/*.refused" in out


def test_un_refus_dans_la_fenetre_n_elargit_pas(tmp_path, capsys):
    """Capturée tôt puis refusée, DANS la fenêtre (14 jours) : effacer le
    refus suffit ; « élargir » à 3 jours l'aurait RÉTRÉCIE."""
    out = _journee_classee(tmp_path, capsys, [
        ("2026-09-23T15:00:00+00:00", rm.TROP_TOT_REFUSEE)], jours_pont=14)
    assert f"rm -f {tmp_path}/*.refused" in out
    assert "SCORES_BRIDGE_DAYS=" not in out
    assert "SORTIES" not in out


def test_le_days_ignore_ce_qu_aucune_relance_ne_regle(tmp_path, capsys):
    """Un match absent de la source il y a 20 jours n'a pas à élargir
    `--days` : seul le jamais-récupéré d'il y a 2 jours compte."""
    out = _journee_classee(tmp_path, capsys, [
        ("2026-09-06T15:00:00+00:00", rm.SANS_SOURCE),
        ("2026-09-24T15:00:00+00:00", rm.JAMAIS_DEMANDEE)])
    assert "results-update --days 3 --sport soccer" in out


def test_les_conseils_des_nouvelles_causes(tmp_path, capsys):
    out = _journee_classee(tmp_path, capsys, [
        ("2026-09-20T15:00:00+00:00", rm.TENNIS_RELIER),
        ("2026-09-22T15:00:00+00:00", rm.TENNIS_SIMPLE),
        ("2026-09-22T15:00:00+00:00", rm.DOUBLE),
        ("2026-09-22T15:00:00+00:00", rm.APPARIABLE)])
    assert "results-update --days 7 --sport tennis" in out
    assert "notre lecteur les écarte" in out
    assert "permettent DÉJÀ de régler" in out
    assert "results-update --days 5 --sport soccer" in out


def test_sans_pont_pas_de_journees_completes(tmp_path, monkeypatch, capsys):
    """Pont coupé : rien ne dit que les journées sont complètes — la sonde ne
    doit pas l'affirmer à côté du conseil d'activer le pont."""
    jour = datetime.now(timezone.utc).date() - timedelta(days=4)

    def construire(c, _d):
        _ev(c, "k1", f"{jour.isoformat()}T15:00:00+00:00")
    tmp_path.joinpath("x").mkdir()
    monkeypatch.setenv("SCORES_FOOTBALL_BRIDGE", "0")
    scores = tmp_path / "scores"
    (scores / "soccer").mkdir(parents=True)
    monkeypatch.setattr(rm, "load_env_file", lambda *a, **k: 0)
    monkeypatch.setenv("SCORES_INGEST_DIR", str(scores))
    p = _base_detections(tmp_path)
    c = sqlite3.connect(str(p))
    construire(c, None)
    c.commit()
    c.close()
    rm.main(["--db", str(p), "--depuis", (jour - timedelta(days=1)).isoformat()])
    out = capsys.readouterr().out
    assert rm.FOOT_SANS_PONT in out and "Toutes complètes" not in out


def test_le_sport_inconnu_se_repare_et_la_commande_marche(tmp_path, monkeypatch,
                                                           capsys):
    import subprocess
    from pathlib import Path
    jour = datetime.now(timezone.utc).date() - timedelta(days=4)

    def construire(c, _d):
        _ev(c, "k1", f"{jour.isoformat()}T15:00:00+00:00", sport="unknown")
        _ev(c, "k2", f"{jour.isoformat()}T16:00:00+00:00", sport="unknown",
            h="x", a="y")                      # aucun sport connu ailleurs
        c.execute("INSERT INTO bet_features (value_bet_id, detected_at, event_key,"
                  " sport, league, book, market, outcome_label, odd_taken,"
                  " fair_odd, ev_pct) VALUES (1,'x','k1','soccer','L','u','h2h',"
                  "'home',2.1,2.0,5)")
    out = _lancer_det(tmp_path, monkeypatch, capsys, construire)
    assert "en connaît le sport pour 1" in out
    db = str(tmp_path / "v.db")
    cmd = rm._commande_sport(db, rm.SQL_SPORT_DET)
    assert cmd in out
    racine = Path(rm.__file__).resolve().parents[1]
    r = subprocess.run(cmd, shell=True, cwd=racine, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    c = sqlite3.connect(db)
    assert dict(c.execute("SELECT event_key, sport FROM events")) == {
        "k1": "soccer", "k2": "unknown"}          # rien n'est inventé


def test_les_ventilations_imprimees(tmp_path, monkeypatch, capsys):
    """Les tranches d'horaire et les formes de prolongation : ce sont elles qui
    chiffreront les règles de production — elles doivent être justes."""
    jour = datetime.now(timezone.utc).date() - timedelta(days=4)
    q = f"{jour.isoformat()}T15:00:00+00:00"
    plus45 = f"{jour.isoformat()}T15:45:00+00:00"

    def construire(c, d):
        _ev(c, "k1", q, h="clyde", a="stranraer", n_vb=3)
        _ev(c, "k2", q, h="bromley", a="reading", n_vb=2)
        _complet(d, jour, [_fixture("Clyde", "Stranraer", plus45),
                           _prolongation("Bromley", "Reading", q, "PEN", (1, 1),
                                         (None, None), (1, 1))])
    out = _lancer_det(tmp_path, monkeypatch, capsys, construire)
    assert "écart d'horaire : ≤ 60 min : 1" in out
    assert f"forme : {rm.TAB_DIRECTS} : 1" in out
    assert f"      1  (    3 dét.)  {rm.HORAIRE}" in out
    assert f"      1  (    2 dét.)  {rm.PROLONG}" in out


def test_joues_le_diagnostic_compte_des_paris(tmp_path, monkeypatch, capsys):
    """En mode --joues, les verdicts comptent des PARIS : les écrire
    « match(s) » compterait un match autant de fois qu'il a été parié."""
    jour = datetime.now(timezone.utc).date() - timedelta(days=4)
    quand = f"{jour.isoformat()}T15:00:00+00:00"
    paris = [(_cle(4, 0), "soccer", quand, "h2h", "home", None, None, True)]

    def complet(_c, d):
        _complet(d, jour, [_fixture("Arsenal", "Chelsea", quand)])
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris, "14", retouche=complet)
    assert "1 pari(s) ABSENTS de la source" in out


def test_oublier_libere_tous_les_caches(tmp_path):
    src = rm.SourceFoot(tmp_path)
    for n in range(6):
        j = date(2026, 9, 1) + timedelta(days=n)
        src.fenetre(j)
        src.proches("x", j)
    src.oublier(date(2026, 9, 5))
    assert all(d >= date(2026, 9, 5) for d in src._res)
    assert all(d >= date(2026, 9, 5) for d in src._fen)
    assert all(k[1] >= date(2026, 9, 5) for k in src._proches)


def test_un_sens_indecidable_n_est_pas_une_ambiguite(tmp_path):
    """« Dundee Utd v Dundee » : la source n'a qu'UN match, mais la nouvelle
    règle d'orientation ne sait pas dans quel sens le lire. Ce n'est pas « deux
    matchs de la source qui se ressemblent » — c'est un choix volontaire."""
    d = _verdict(tmp_path, _notre("Dundee Utd", "Dundee", Q),
                 [_fixture("Dundee United", "Dundee", Q)])
    assert d["verdict"] == rm.ORIENTATION, d
