"""Les bornes de date, l'axe semaine, et la liste nommée.

TROIS PIÈGES, CHACUN DÉJÀ PAYÉ AILLEURS DANS CE PROJET
------------------------------------------------------
1. UNE BORNE HAUTE EXCLUSIVE. « --jusqu-a 2026-09-08 » doit contenir le
   8 septembre EN ENTIER. Couper à minuit jetterait une journée de détections
   sans rien dire — et personne ne compte les lignes qu'il ne voit pas.

2. UNE DATE MAL ÉCRITE ACCEPTÉE. `01/08/2026` doit lever une erreur qui donne
   le format, pas filtrer silencieusement tout ou rien. Une fenêtre vide qu'on
   croit pleine est le mode de panne dominant du projet (§11).

3. UNE SEMAINE ÉTIQUETÉE PAR SON NUMÉRO. « S28 » ne se trie pas d'une année
   sur l'autre et ne dit pas de quand il parle. L'étiquette commence par la
   date ISO du lundi précisément pour que le tri des libellés soit
   chronologique — c'est ce tri qui ordonne les lignes du tableau.
"""
from __future__ import annotations

import sqlite3
from argparse import Namespace
from datetime import datetime, timezone

import pytest

from scripts.clv_roi_matrix import (_appliquer_fenetre, _axe, _bande_semaine,
                                    _jour_utc, main)


def _args(**kw):
    base = dict(depuis=None, jusqu_a=None, jours=0)
    base.update(kw)
    return Namespace(**base)


def _r(detected, start=None):
    return {"detected_at": detected, "start_time": start or "2020-01-01T00:00:00+00:00"}


# ── Les dates ────────────────────────────────────────────────────────

def test_une_date_valide_donne_minuit_utc():
    assert _jour_utc("2026-08-01", "--depuis") == datetime(
        2026, 8, 1, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("brut", ["01/08/2026", "2026-13-01", "aout", "",
                                  "2026-08-32", "20260801"])
def test_une_date_invalide_dit_le_format(brut):
    with pytest.raises(SystemExit) as e:
        _jour_utc(brut, "--depuis")
    assert "AAAA-MM-JJ" in str(e.value)
    assert "2026-08-01" in str(e.value), "l'exemple manque"


# ── La fenêtre ───────────────────────────────────────────────────────

def test_la_borne_haute_contient_la_journee_entiere():
    """LE PIÈGE N°1. Une détection à 23 h 59 le 8 septembre doit être DEDANS."""
    rows = [_r("2026-09-08T23:59:00+00:00"), _r("2026-09-09T00:00:01+00:00")]
    gardees, _ = _appliquer_fenetre(rows, _args(depuis="2026-08-01",
                                                jusqu_a="2026-09-08"))
    assert len(gardees) == 1
    assert gardees[0]["detected_at"].startswith("2026-09-08")


def test_la_borne_basse_contient_minuit():
    rows = [_r("2026-08-01T00:00:00+00:00"), _r("2026-07-31T23:59:59+00:00")]
    gardees, _ = _appliquer_fenetre(rows, _args(depuis="2026-08-01"))
    assert len(gardees) == 1


def test_jours_et_dates_ensemble_sont_refuses():
    """Deux fenêtres dont l'une glisse avec l'heure : leur intersection n'a
    pas de bornes énonçables. Mieux vaut refuser que rendre un chiffre."""
    with pytest.raises(SystemExit) as e:
        _appliquer_fenetre([_r("2026-08-01T10:00:00+00:00")],
                           _args(depuis="2026-08-01", jours=7))
    assert "choisir l'un ou l'autre" in str(e.value)


def test_une_fenetre_a_l_envers_est_refusee():
    with pytest.raises(SystemExit) as e:
        _appliquer_fenetre([_r("2026-08-01T10:00:00+00:00")],
                           _args(depuis="2026-09-08", jusqu_a="2026-08-01"))
    assert "vide par construction" in str(e.value)


def test_sans_fenetre_rien_n_est_filtre():
    rows = [_r("2020-01-01T00:00:00+00:00")]
    gardees, desc = _appliquer_fenetre(rows, _args())
    assert gardees == rows and desc == ""


def test_les_lignes_sans_date_sont_comptees_pas_tues():
    """Une ligne sans `detected_at` ne peut appartenir à aucune fenêtre. La
    jeter en silence ferait chercher plus tard pourquoi les totaux ne se
    recollent pas."""
    rows = [_r("2026-08-05T10:00:00+00:00"), _r(None), _r("")]
    gardees, desc = _appliquer_fenetre(rows, _args(depuis="2026-08-01"))
    assert len(gardees) == 1
    assert "2 ligne(s) sans date" in desc


def test_une_fenetre_vide_le_dit_avec_le_total():
    with pytest.raises(SystemExit) as e:
        _appliquer_fenetre([_r("2026-01-01T10:00:00+00:00")],
                           _args(depuis="2026-08-01"))
    assert "Aucune détection sur la période" in str(e.value)
    assert "sur 1 au total" in str(e.value)


def test_les_matchs_a_venir_sont_annonces():
    """Ils n'ont ni CLV ni résultat : sans cet avertissement, les colonnes
    CLV et ROI décrivent une autre population que la colonne `opp`."""
    futur = "2099-01-01T00:00:00+00:00"
    rows = [_r("2026-08-05T10:00:00+00:00", start=futur)]
    _, desc = _appliquer_fenetre(rows, _args(depuis="2026-08-01"))
    assert "PAS ENCORE JOUÉS" in desc


# ── L'axe semaine ────────────────────────────────────────────────────

def test_la_semaine_est_etiquetee_par_son_lundi():
    # Le 8 septembre 2026 est un mardi ; son lundi est le 7.
    assert _bande_semaine(_r("2026-09-08T10:00:00+00:00")).startswith("2026-09-07")


def test_un_lundi_est_son_propre_lundi():
    assert _bande_semaine(_r("2026-09-07T00:00:01+00:00")).startswith("2026-09-07")


def test_un_dimanche_appartient_a_la_semaine_qui_s_acheve():
    # Dimanche 6 septembre → semaine du lundi 31 août.
    assert _bande_semaine(_r("2026-09-06T23:00:00+00:00")).startswith("2026-08-31")


def test_les_libelles_de_semaine_se_trient_chronologiquement():
    """LE PIÈGE N°3. C'est le tri des LIBELLÉS qui ordonne le tableau : s'il
    n'est pas chronologique, les semaines sortent dans le désordre."""
    jours = ["2026-12-28T10:00:00+00:00", "2027-01-04T10:00:00+00:00",
             "2026-07-06T10:00:00+00:00"]
    libelles = [_bande_semaine(_r(j)) for j in jours]
    assert sorted(libelles) == [libelles[2], libelles[0], libelles[1]]


def test_une_ligne_sans_date_a_sa_propre_bande():
    assert _bande_semaine(_r(None)) == "? (sans date)"


def test_l_axe_semaine_est_reconnu():
    titre, bande_de, ordre = _axe("semaine")
    assert titre == "semaine (lundi)"
    # Ordre canonique VIDE : les semaines présentes dépendent des données.
    assert ordre == []
    assert bande_de(_r("2026-09-08T10:00:00+00:00")).startswith("2026-09-07")


# ── La liste nommée ──────────────────────────────────────────────────

def _base(tmp_path, lignes):
    p = tmp_path / "v.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
        CREATE TABLE value_bets (id INTEGER PRIMARY KEY, event_key TEXT,
            book TEXT, market TEXT, outcome_label TEXT, line REAL,
            odd_taken REAL, fair_odd REAL, ev_pct REAL, detected_at TEXT);
        CREATE TABLE clv_snapshots (id INTEGER PRIMARY KEY, value_bet_id INT,
            closing INT, fair_odd REAL);
        CREATE TABLE events (event_key TEXT PRIMARY KEY, sport TEXT,
            league TEXT, home TEXT, away TEXT, start_time TEXT);
        CREATE TABLE results (event_key TEXT PRIMARY KEY, winner TEXT,
            home_score INT, away_score INT);
        CREATE TABLE played_bets (dedup_key TEXT PRIMARY KEY, value_bet_id INT);
        CREATE TABLE notified_value_bets (id INTEGER PRIMARY KEY,
            event_key TEXT, book TEXT, market TEXT, outcome_label TEXT,
            line REAL, ev_pct REAL, notified_at TEXT);
    """)
    for (i, home, away, jour, cote, ev, clot, gagnant) in lignes:
        ek = f"{jour.replace('-', '')}1800::{home.lower()}__vs__{away.lower()}{i}"
        c.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                  (ek, "soccer", "L1", home, away, f"{jour}T18:00:00+00:00"))
        c.execute("INSERT INTO value_bets VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (i, ek, "unibet_be", "h2h", "home", None, cote,
                   cote / (1 + ev / 100), ev, f"{jour}T10:00:00+00:00"))
        if clot is not None:
            c.execute("INSERT INTO clv_snapshots VALUES (?,?,?,?)",
                      (i, i, 1, clot))
        if gagnant is not None:
            c.execute("INSERT INTO results VALUES (?,?,?,?)", (ek, gagnant, 1, 0))
    c.commit()
    c.close()
    return p


def test_la_liste_nomme_les_matchs(tmp_path, capsys, monkeypatch):
    p = _base(tmp_path, [
        (1, "Anderlecht", "Genk", "2026-09-01", 2.10, 5.0, 2.00, "home"),
        (2, "Club Brugge", "Gent", "2026-09-02", 3.40, 8.0, 3.20, "away"),
    ])
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--lister"])
    main()
    out = capsys.readouterr().out
    assert "LES PARIS, UN PAR UN" in out
    assert "Anderlecht - Genk" in out
    assert "Club Brugge - Gent" in out
    assert "✅" in out and "❌" in out


def test_un_pari_non_regle_porte_un_sablier_pas_un_zero(tmp_path, capsys,
                                                        monkeypatch):
    """⚠️ « — » et « ⏳ », JAMAIS 0,00 €. Un pari sans résultat n'a pas
    rapporté zéro : on ne sait pas encore. Écrire zéro le ferait entrer dans
    les moyennes de l'œil du lecteur."""
    p = _base(tmp_path, [
        (1, "Anderlecht", "Genk", "2026-09-01", 2.10, 5.0, None, None)])
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--lister"])
    main()
    out = capsys.readouterr().out
    ligne = [l for l in out.splitlines() if "Anderlecht - Genk" in l][0]
    assert "⏳" in ligne, ligne
    assert "0.00 €" not in ligne, ligne
    # CLV inconnue elle aussi : un tiret, pas un zéro.
    assert "CLV     —" in ligne, ligne


def test_sans_lister_la_sortie_ne_contient_pas_la_liste(tmp_path, capsys,
                                                        monkeypatch):
    p = _base(tmp_path, [
        (1, "Anderlecht", "Genk", "2026-09-01", 2.10, 5.0, 2.00, "home")])
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p)])
    main()
    assert "LES PARIS, UN PAR UN" not in capsys.readouterr().out


def test_la_liste_suit_l_axe_demande(tmp_path, capsys, monkeypatch):
    """Elle groupe par la bande de l'axe COURANT, pas par une bande à elle :
    deux groupages différents entre le tableau et la liste rendraient leurs
    totaux incomparables."""
    p = _base(tmp_path, [
        (1, "Anderlecht", "Genk", "2026-08-03", 2.10, 5.0, 2.00, "home"),
        (2, "Club Brugge", "Gent", "2026-08-11", 3.40, 8.0, 3.20, "away"),
    ])
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--axe", "semaine",
                                     "--lister"])
    main()
    out = capsys.readouterr().out
    assert "── 2026-08-03" in out
    assert "── 2026-08-10" in out


# ── « Joué » est une propriété de l'OPPORTUNITÉ, pas de la ligne ─────

def _base_jouee(tmp_path, lignes, joues=()):
    """`lignes` : (id, book, home, away, jour, cote, ev, clot, gagnant)."""
    p = tmp_path / "v.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
        CREATE TABLE value_bets (id INTEGER PRIMARY KEY, event_key TEXT,
            book TEXT, market TEXT, outcome_label TEXT, line REAL,
            odd_taken REAL, fair_odd REAL, ev_pct REAL, detected_at TEXT);
        CREATE TABLE clv_snapshots (id INTEGER PRIMARY KEY, value_bet_id INT,
            closing INT, fair_odd REAL);
        CREATE TABLE events (event_key TEXT PRIMARY KEY, sport TEXT,
            league TEXT, home TEXT, away TEXT, start_time TEXT);
        CREATE TABLE results (event_key TEXT PRIMARY KEY, winner TEXT,
            home_score INT, away_score INT);
        CREATE TABLE played_bets (dedup_key TEXT PRIMARY KEY, value_bet_id INT);
        CREATE TABLE notified_value_bets (id INTEGER PRIMARY KEY,
            event_key TEXT, book TEXT, market TEXT, outcome_label TEXT,
            line REAL, ev_pct REAL, notified_at TEXT);
    """)
    for (i, book, home, away, jour, cote, ev, clot, gagnant) in lignes:
        ek = f"{jour.replace('-', '')}1800::{home.lower()}__vs__{away.lower()}"
        c.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                  (ek, "soccer", "L1", home, away, f"{jour}T18:00:00+00:00"))
        c.execute("INSERT INTO value_bets VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (i, ek, book, "h2h", "home", None, cote,
                   cote / (1 + ev / 100), ev, f"{jour}T10:00:00+00:00"))
        if clot is not None:
            c.execute("INSERT INTO clv_snapshots VALUES (?,?,?,?)", (i, i, 1, clot))
        if gagnant is not None:
            c.execute("INSERT OR IGNORE INTO results VALUES (?,?,?,?)",
                      (ek, gagnant, 1, 0))
    for i in joues:
        c.execute("INSERT INTO played_bets VALUES (?,?)", (f"k{i}", i))
    c.commit()
    c.close()
    return p


def _opportunites(sortie: str) -> int:
    import re
    m = re.search(r"(\d+) opportunités dédupliquées", sortie)
    return int(m.group(1)) if m else -1


def test_joue_est_agrege_sur_l_opportunite_entiere(tmp_path, capsys,
                                                   monkeypatch):
    """⚠️ LE PIÈGE. Le pari est cliqué chez Unibet à 2,10 ; Ladbrokes proposait
    2,15, donc la dédup garde la ligne Ladbrokes — qui n'est PAS marquée jouée.

    Filtrer sur la ligne retenue classerait cette opportunité dans « non
    jouée ». Le lot « non joué » se remplirait alors exactement des paris les
    mieux tarifés, et la comparaison dirait le contraire de la vérité."""
    p = _base_jouee(
        tmp_path,
        [(1, "unibet_be", "Anderlecht", "Genk", "2026-09-01", 2.10, 5.0, 2.00, "home"),
         (2, "ladbrokes_be", "Anderlecht", "Genk", "2026-09-01", 2.15, 7.5, 2.00, "home")],
        joues=(1,))

    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--joues", "oui"])
    main()
    assert _opportunites(capsys.readouterr().out) == 1, "l'opportunité jouée a disparu"

    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--joues", "non"])
    with pytest.raises(SystemExit):
        main()          # plus rien : la seule opportunité a été jouée


def test_les_deux_lots_partitionnent_le_total(tmp_path, capsys, monkeypatch):
    """joués + non joués = tout. Un pari qui tombe dans les deux, ou dans
    aucun, fausserait toute comparaison entre les deux lots."""
    lignes = [(i, "unibet_be", f"A{i}", f"B{i}", "2026-09-01",
               2.00 + i / 100, 5.0, 1.95, "home") for i in range(1, 11)]
    p = _base_jouee(tmp_path, lignes, joues=(1, 2, 3, 4))

    def n(mode):
        monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--joues", mode])
        main()
        return _opportunites(capsys.readouterr().out)

    tous, oui, non = n("tous"), n("oui"), n("non")
    assert tous == 10
    assert oui == 4 and non == 6
    assert oui + non == tous


def test_la_population_est_annoncee_dans_l_entete(tmp_path, capsys, monkeypatch):
    """Deux sorties qui ne portent pas sur la même population doivent le dire,
    sinon on les compare sans le savoir."""
    p = _base_jouee(
        tmp_path,
        [(1, "unibet_be", "A", "B", "2026-09-01", 2.10, 5.0, 2.00, "home")],
        joues=(1,))
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--joues", "oui"])
    main()
    out = capsys.readouterr().out
    assert "UNIQUEMENT les paris cliqués" in out

    monkeypatch.setattr("sys.argv", ["m", "--db", str(p)])
    main()
    assert "Population :" not in capsys.readouterr().out


# ── L'axe « heure d'envoi » ──────────────────────────────────────────

class _R(dict):
    """Une ligne qui répond à `keys()` comme un sqlite3.Row."""
    def keys(self):
        return dict.keys(self)


def test_l_heure_est_locale_pas_utc():
    """⚠️ LE PIÈGE DE CET AXE. `notified_at` est en UTC. Afficher ces heures-là
    dirait « creux à 1 h du matin » pour un creux qui est à 3 h chez le
    lecteur — et c'est sur ce moment-là qu'il agirait. Un axe horaire décalé de
    deux heures est pire qu'absent."""
    from scripts.clv_roi_matrix import _bande_heure
    assert _bande_heure(_R(notified_at="2026-07-15T01:30:00+00:00")) == "03 h"


def test_le_passage_a_l_heure_d_hiver_est_suivi():
    """Un décalage fixe de +2 h se tromperait d'une heure de novembre à mars,
    sur toute fenêtre qui traverse octobre."""
    from scripts.clv_roi_matrix import _bande_heure
    assert _bande_heure(_R(notified_at="2026-01-15T01:30:00+00:00")) == "02 h"


def test_un_pari_jamais_alerte_a_sa_propre_bande():
    """Il n'est PAS un déchet : sa CLV est parfaitement mesurable. Le jeter
    ferait lire l'axe sur la seule sous-population rapprochée."""
    from scripts.clv_roi_matrix import SANS_ENVOI, _bande_heure
    assert _bande_heure(_R(notified_at=None)) == SANS_ENVOI
    assert _bande_heure(_R()) == SANS_ENVOI


def test_les_libelles_d_heure_se_trient_chronologiquement():
    from scripts.clv_roi_matrix import ORDRE_HEURE
    heures = [h for h in ORDRE_HEURE if h.endswith(" h")]
    assert heures == sorted(heures), "le tri des libellés n'est pas horaire"
    assert heures[0] == "00 h" and heures[-1] == "23 h" and len(heures) == 24


def test_l_axe_heure_est_reconnu():
    from scripts.clv_roi_matrix import _axe
    titre, bande_de, ordre = _axe("heure")
    assert titre == "heure d'envoi"
    assert len(ordre) == 25          # 24 heures + « non notifié »


def _base_heure(tmp_path, envois):
    """`envois` : (id, notified_at ou None). Une opportunité par id."""
    p = tmp_path / "v.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
        CREATE TABLE value_bets (id INTEGER PRIMARY KEY, event_key TEXT,
            book TEXT, market TEXT, outcome_label TEXT, line REAL,
            odd_taken REAL, fair_odd REAL, ev_pct REAL, detected_at TEXT);
        CREATE TABLE clv_snapshots (id INTEGER PRIMARY KEY, value_bet_id INT,
            closing INT, fair_odd REAL);
        CREATE TABLE events (event_key TEXT PRIMARY KEY, sport TEXT,
            league TEXT, home TEXT, away TEXT, start_time TEXT);
        CREATE TABLE results (event_key TEXT PRIMARY KEY, winner TEXT,
            home_score INT, away_score INT);
        CREATE TABLE played_bets (dedup_key TEXT PRIMARY KEY, value_bet_id INT);
        CREATE TABLE notified_value_bets (id INTEGER PRIMARY KEY,
            event_key TEXT, book TEXT, market TEXT, outcome_label TEXT,
            line REAL, ev_pct REAL, notified_at TEXT);
    """)
    for i, quand in envois:
        ek = f"202607151800::a{i}__vs__b{i}"
        c.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                  (ek, "soccer", "L1", f"A{i}", f"B{i}",
                   "2026-07-15T18:00:00+00:00"))
        c.execute("INSERT INTO value_bets VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (i, ek, "unibet_be", "h2h", "home", None, 2.50, 2.30, 8.7,
                   "2026-07-15T10:00:00+00:00"))
        c.execute("INSERT INTO clv_snapshots VALUES (?,?,?,?)", (i, i, 1, 2.30))
        if quand:
            c.execute("INSERT INTO notified_value_bets VALUES (?,?,?,?,?,?,?,?)",
                      (i, ek, "unibet_be", "h2h", "home", None, 8.7, quand))
    c.commit()
    c.close()
    return p


def test_le_taux_de_rapprochement_est_annonce(tmp_path, capsys, monkeypatch):
    """⚠️ `notified_value_bets` n'a pas de `value_bet_id` : le rapprochement se
    fait sur cinq colonnes et reste imparfait. Taire ce taux ferait lire la
    bande « non notifié » comme « jamais alerté »."""
    envois = [(i, "2026-07-15T08:00:00+00:00") for i in range(1, 8)]
    envois += [(i, None) for i in range(8, 11)]
    p = _base_heure(tmp_path, envois)
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--axe", "heure"])
    main()
    out = capsys.readouterr().out
    assert "Heure d'envoi retrouvée pour 7 opportunités sur 10 (70 %)" in out
    assert "Europe/Brussels" in out
    assert "indiscernables" in out


def test_les_paris_non_notifies_restent_dans_le_tableau(tmp_path, capsys,
                                                        monkeypatch):
    p = _base_heure(tmp_path, [(1, "2026-07-15T08:00:00+00:00"), (2, None)])
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--axe", "heure"])
    main()
    out = capsys.readouterr().out
    assert "10 h" in out            # 08 h UTC = 10 h locale en été
    assert "non notifié" in out


def test_le_taux_n_est_annonce_que_sur_l_axe_heure(tmp_path, capsys,
                                                   monkeypatch):
    p = _base_heure(tmp_path, [(1, "2026-07-15T08:00:00+00:00")])
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p)])
    main()
    assert "Heure d'envoi retrouvée" not in capsys.readouterr().out


def test_l_heure_d_alerte_est_agregee_sur_l_opportunite(tmp_path, capsys,
                                                        monkeypatch):
    """⚠️ MÊME PIÈGE QUE `--joues`, manqué la première fois.

    L'alerte part sur UN book ; la dédup garde le book à la MEILLEURE COTE.
    Ici l'alerte est partie sur Unibet (2,10) alors que Ladbrokes proposait
    2,15 — c'est donc la ligne Ladbrokes qui représente l'opportunité, et elle
    n'a aucune notification. Sans agrégation sur le groupe, l'opportunité tombe
    en « non notifié » alors qu'elle a bien été alertée à 10 h."""
    p = tmp_path / "v.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
        CREATE TABLE value_bets (id INTEGER PRIMARY KEY, event_key TEXT,
            book TEXT, market TEXT, outcome_label TEXT, line REAL,
            odd_taken REAL, fair_odd REAL, ev_pct REAL, detected_at TEXT);
        CREATE TABLE clv_snapshots (id INTEGER PRIMARY KEY, value_bet_id INT,
            closing INT, fair_odd REAL);
        CREATE TABLE events (event_key TEXT PRIMARY KEY, sport TEXT,
            league TEXT, home TEXT, away TEXT, start_time TEXT);
        CREATE TABLE results (event_key TEXT PRIMARY KEY, winner TEXT,
            home_score INT, away_score INT);
        CREATE TABLE played_bets (dedup_key TEXT PRIMARY KEY, value_bet_id INT);
        CREATE TABLE notified_value_bets (id INTEGER PRIMARY KEY,
            event_key TEXT, book TEXT, market TEXT, outcome_label TEXT,
            line REAL, ev_pct REAL, notified_at TEXT);
    """)
    ek = "202607151800::a__vs__b"
    c.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
              (ek, "soccer", "L1", "A", "B", "2026-07-15T18:00:00+00:00"))
    for i, (book, cote) in enumerate(
            [("unibet_be", 2.10), ("ladbrokes_be", 2.15)], start=1):
        c.execute("INSERT INTO value_bets VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (i, ek, book, "h2h", "home", None, cote, 2.00, 5.0,
                   "2026-07-15T07:00:00+00:00"))
        c.execute("INSERT INTO clv_snapshots VALUES (?,?,?,?)", (i, i, 1, 2.00))
    # L'alerte n'est partie QUE sur Unibet — le book qui n'a PAS la meilleure cote.
    c.execute("INSERT INTO notified_value_bets VALUES (?,?,?,?,?,?,?,?)",
              (1, ek, "unibet_be", "h2h", "home", None, 5.0,
               "2026-07-15T08:00:00+00:00"))
    c.commit()
    c.close()

    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--axe", "heure"])
    main()
    out = capsys.readouterr().out
    assert "10 h" in out, out          # 08 h UTC = 10 h locale en été
    assert "retrouvée pour 1 opportunités sur 1 (100 %)" in out, out
