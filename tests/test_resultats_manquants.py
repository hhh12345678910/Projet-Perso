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


def test_une_pierre_tombale_est_definitive(tmp_path):
    j = date(2026, 9, 24)
    _fichier(tmp_path, j, MAINTENANT, ext="refused")
    assert rm.etat_journee(j, tmp_path, MAINTENANT, 7, FINAL) == rm.REFUSEE


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


def _lancer(tmp_path, monkeypatch, capsys, paris, jours_pont="2", *extra):
    """La base, un dossier de pont vide, et une date de départ relative à
    AUJOURD'HUI — `main` lit l'horloge réelle."""
    scores = tmp_path / "scores"
    (scores / "soccer").mkdir(parents=True)
    monkeypatch.setattr(rm, "load_env_file", lambda *a, **k: 0)
    monkeypatch.setenv("SCORES_INGEST_DIR", str(scores))
    monkeypatch.setenv("SCORES_BRIDGE_DAYS", jours_pont)
    p = _base(tmp_path, paris)
    debut = (datetime.now(timezone.utc).date() - timedelta(days=10)).isoformat()
    rm.main(["--db", str(p), "--depuis", debut, *extra])
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
    assert "Récupérer mes résultats" in out


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
    rm.main(["--db", str(p)])
    assert "SCORES_BRIDGE_DAYS = 2 (absent de .env" in capsys.readouterr().out


def test_une_date_mal_ecrite_est_refusee(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, "load_env_file", lambda *a, **k: 0)
    with pytest.raises(SystemExit):
        rm.main(["--db", str(_base(tmp_path, [])), "--depuis", "26/09/2026"])


def test_elargir_passe_avant_cliquer(tmp_path, monkeypatch, capsys):
    """Un clic sur l'ancienne fenêtre ne reprendrait pas les journées sorties :
    on croirait le trou comblé. L'élargissement doit être proposé d'abord."""
    paris = [(_cle(5, 0), "soccer", _jour(5), "h2h", "home", None, None, True)]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris)
    assert out.index("SORTIES de la fenêtre") < out.index("Récupérer mes résultats")


def test_la_ligne_n_est_pas_ecrite_deux_fois(tmp_path, monkeypatch, capsys):
    paris = [(_cle(5, 0), "soccer", _jour(5), "totals", "over", 2.5, None, True),
             (_cle(5, 1), "soccer", _jour(5), "totals", "under 3.5", 3.5, None, True)]
    out, _ = _lancer(tmp_path, monkeypatch, capsys, paris, "2", "--lister")
    assert "over 2.5" in out and "under 3.5" in out and "3.5 3.5" not in out


# ── Pourquoi un match de football n'a pas été rapproché ─────────────

import json as _json  # noqa: E402


def _fixture(dom, ext, quand, statut="FT", ligue="L"):
    return {"fixture": {"date": quand, "status": {"short": statut}},
            "league": {"name": ligue},
            "teams": {"home": {"name": dom}, "away": {"name": ext}}}


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
    d = _verdict(tmp_path, _notre("Vancouver", "Inter Toronto", Q),
                 [_fixture("Vancouver FC", "Inter Toronto", Q, statut="PST")])
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
    rm.main(["--db", str(p), "--depuis", (jour - timedelta(days=1)).isoformat()])
    out = capsys.readouterr().out
    assert "CE QUE LA SOURCE AVAIT CE JOUR-LÀ" in out
    assert rm.APPARIABLE in out
    assert "results-update n'est pas repassé" in out


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
