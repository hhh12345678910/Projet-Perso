"""La porte d'ENVOI rejouée — les paris qui ne sont jamais partis.

POURQUOI CE FICHIER EXISTE
--------------------------
`--premium` rejoue la porte du CANAL, et on en a conclu que le lot « alerté et
non joué » était bien le lot des paris alertés. C'est faux : `send_value_bet`
écarte des value bets AVANT tout routage, donc avant que la porte du canal ait
son mot à dire —

* les marchés de MI-TEMPS, pour aucun canal, jamais ;
* la FENÊTRE MORTE, un value bet prématch à moins de `min_minutes_to_kickoff`
  du coup d'envoi.

Ces paris sont dans `value_bets`, leur clôture est capturée, leur CLV est
mesurée — et aucun message n'est jamais parti. Ils ne peuvent donc PAS être
cliqués, et tombent tous du côté « non joué » de la comparaison. Comparer un
lot qui les contient à un lot qui, par construction, n'en contient aucun, ce
n'est pas comparer « joué » à « alerté » : c'est comparer deux populations
différentes en croyant mesurer un choix.

LES TROIS PIÈGES QUE CES TESTS TIENNENT
---------------------------------------
1. « ALERTABLE » EST UNE PROPRIÉTÉ DE L'OPPORTUNITÉ, PAS DE LA LIGNE — le même
   piège que `played` et que `notified_at`, tombés l'un après l'autre. La
   fenêtre morte se juge sur `detected_at`, qui diffère d'un book à l'autre.
2. LE SEUIL EST LU, JAMAIS RECOPIÉ (§17.7). Changer
   `TELEGRAM_MIN_MINUTES_TO_KICKOFF` doit changer ce que le rejeu écarte.
3. LE REJEU SE FALSIFIE LUI-MÊME. Un pari cliqué sur « Jouer » vient du bouton
   d'un message : le rejeu ne devrait pas pouvoir en tuer. Ce qu'il en tue est
   son taux d'erreur, et il s'imprime.
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from scripts.clv_roi_matrix import (_alertable, _fenetre_morte_defaut,
                                    _fenetre_morte_defaut as _defaut, main)


# ── Le prédicat, ligne à ligne ───────────────────────────────────────

def _ligne(detected, start, market="h2h"):
    return {"detected_at": detected, "start_time": start, "market": market}


J = "2026-09-01T18:00:00+00:00"


def test_prematch_loin_du_coup_d_envoi_est_alertable():
    assert _alertable(_ligne("2026-09-01T12:00:00+00:00", J), 15) is True


def test_prematch_dans_la_fenetre_morte_ne_l_est_pas():
    assert _alertable(_ligne("2026-09-01T17:50:00+00:00", J), 15) is False


def test_la_borne_est_inclusive_comme_en_production():
    """Production : `if mins_to_kickoff < cfg.min_minutes_to_kickoff: return`.
    À EXACTEMENT 15 minutes, le pari PART. Une borne stricte du mauvais côté
    écarterait des paris réellement alertés."""
    assert _alertable(_ligne("2026-09-01T17:45:00+00:00", J), 15) is True


def test_une_detection_live_echappe_au_garde():
    """Le garde de production est explicite : `if start is not None and not
    is_live`. Un pari dont le coup d'envoi est passé N'EST PAS écarté par la
    fenêtre morte — l'écarter ici jetterait des paris que la production
    envoie."""
    assert _alertable(_ligne("2026-09-01T19:30:00+00:00", J), 15) is True


def test_sans_horaire_de_coup_d_envoi_rien_n_est_ecarte():
    assert _alertable(_ligne("2026-09-01T12:00:00+00:00", None), 15) is True
    assert _alertable(_ligne(None, J), 15) is True


@pytest.mark.parametrize("marche", ["h2h_h1", "totals_h1"])
def test_la_mi_temps_n_est_jamais_alertable(marche):
    """Aucun canal, ni principal, ni premium, ni critique — et ce, quel que
    soit le délai."""
    assert _alertable(_ligne("2026-08-25T10:00:00+00:00", J, marche), 15) is False


@pytest.mark.parametrize("marche", ["h2h", "totals", "handicap", "btts"])
def test_le_match_plein_reste_alertable(marche):
    assert _alertable(_ligne("2026-08-25T10:00:00+00:00", J, marche), 15) is True


def test_un_marche_inconnu_ne_fait_pas_tomber_l_outil():
    """Un libellé que `MarketType` ne connaît pas doit passer, pas exploser :
    une table qui gagne un marché ne doit pas casser l'analyse de tous les
    autres."""
    assert _alertable(_ligne("2026-08-25T10:00:00+00:00", J, "corners"), 15) is True
    assert _alertable(_ligne("2026-08-25T10:00:00+00:00", J, None), 15) is True


# ── Le seuil est LU, jamais recopié ──────────────────────────────────

def test_le_seuil_vient_de_la_configuration_de_production(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1")
    monkeypatch.setenv("TELEGRAM_MIN_MINUTES_TO_KICKOFF", "42")
    minutes, source = _fenetre_morte_defaut()
    assert minutes == 42
    assert "TelegramConfig" in source


def test_sans_jeton_le_defaut_le_DIT(monkeypatch):
    """Une valeur par défaut prise faute d'environnement doit se voir : sinon
    le rejeu prétend reproduire un réglage qu'il n'a jamais lu (§11)."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    minutes, source = _defaut()
    assert minutes == 15
    assert "PAS été lue" in source


# ── Sur la base, de bout en bout ─────────────────────────────────────

def _base(tmp_path, lignes, joues=()):
    """`lignes` : (id, book, home, away, marche, detected_at, cote)."""
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
    for (i, book, home, away, marche, detecte, cote) in lignes:
        ek = f"202609011800::{home.lower()}__vs__{away.lower()}"
        c.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                  (ek, "soccer", "L1", home, away, J))
        c.execute("INSERT INTO value_bets VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (i, ek, book, marche, "home", None, cote, cote / 1.05,
                   5.0, detecte))
        c.execute("INSERT INTO clv_snapshots VALUES (?,?,?,?)",
                  (i, i, 1, cote / 1.05))
        c.execute("INSERT OR IGNORE INTO results VALUES (?,?,?,?)",
                  (ek, "home", 1, 0))
    for i in joues:
        c.execute("INSERT INTO played_bets VALUES (?,?)", (f"k{i}", i))
    c.commit()
    c.close()
    return p


def _n(sortie: str) -> int:
    m = re.search(r"(\d+) opportunités dédupliquées", sortie)
    return int(m.group(1)) if m else -1


def _lancer(monkeypatch, capsys, *argv):
    monkeypatch.setattr("sys.argv", ["m", *argv])
    main()
    return capsys.readouterr().out


LOIN = "2026-09-01T10:00:00+00:00"      # 8 h avant
PRES = "2026-09-01T17:56:00+00:00"      # 4 min avant
TRES_LOIN = "2026-08-31T18:00:00+00:00"  # 24 h avant


def test_sans_le_drapeau_rien_ne_change(tmp_path, capsys, monkeypatch):
    """Le défaut doit rester EXACTEMENT celui de toutes les mesures
    précédentes. Activer un filtre en douce changerait la population sous des
    chiffres qui se ressemblent."""
    p = _base(tmp_path, [(1, "unibet_be", "A", "B", "h2h", PRES, 2.10),
                         (2, "unibet_be", "C", "D", "h2h_h1", LOIN, 2.20)])
    sortie = _lancer(monkeypatch, capsys, "--db", str(p))
    assert _n(sortie) == 2
    assert "Porte d'ENVOI" not in sortie


def test_le_drapeau_ecarte_la_fenetre_morte_et_la_mi_temps(tmp_path, capsys,
                                                           monkeypatch):
    p = _base(tmp_path, [(1, "unibet_be", "A", "B", "h2h", PRES, 2.10),
                         (2, "unibet_be", "C", "D", "h2h_h1", LOIN, 2.20),
                         (3, "unibet_be", "E", "F", "h2h", LOIN, 2.30)])
    sortie = _lancer(monkeypatch, capsys, "--db", str(p), "--porte-envoi")
    assert _n(sortie) == 1, "seul le prématch loin du coup d'envoi survit"
    assert "2 opportunités écartées sur 3" in sortie


def test_le_seuil_impose_en_ligne_de_commande_est_respecte(tmp_path, capsys,
                                                           monkeypatch):
    """Une détection à 8 h du coup d'envoi tombe si la fenêtre morte fait
    10 heures, et survit si elle en fait 15 minutes. Sans les DEUX sens, le
    drapeau pourrait ne rien lire du nombre et le test passerait quand même."""
    p = _base(tmp_path, [(1, "unibet_be", "A", "B", "h2h", LOIN, 2.10),
                         (2, "unibet_be", "C", "D", "h2h", TRES_LOIN, 2.20)])

    large = _lancer(monkeypatch, capsys, "--db", str(p), "--porte-envoi", "600")
    assert _n(large) == 1
    assert "1 opportunités écartées sur 2" in large

    etroit = _lancer(monkeypatch, capsys, "--db", str(p), "--porte-envoi", "15")
    assert _n(etroit) == 2
    assert "0 opportunités écartées sur 2" in etroit


def test_un_seuil_illisible_est_refuse(tmp_path, monkeypatch):
    p = _base(tmp_path, [(1, "unibet_be", "A", "B", "h2h", LOIN, 2.10)])
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p),
                                     "--porte-envoi", "quinze"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code != 0


# ── LE PIÈGE N°1 : la propriété est celle du GROUPE ──────────────────

def test_une_seule_ligne_hors_fenetre_suffit_a_garder_l_opportunite(
        tmp_path, capsys, monkeypatch):
    """⚠️ LE PIÈGE, TROISIÈME DE LA MÊME FAMILLE.

    Ladbrokes voit le pari à 8 h du coup d'envoi : l'alerte PART. Unibet le
    voit 4 minutes avant et se fait taire — mais l'opportunité, elle, a bien
    été alertée. La dédup garde le meilleur prix, ici Unibet à 2,30 : juger la
    fenêtre morte sur ce seul représentant écarterait une opportunité
    réellement partie, et le lot restant serait celui du hasard des horaires
    de détection, pas celui des alertes."""
    p = _base(tmp_path, [(1, "ladbrokes_be", "A", "B", "h2h", LOIN, 2.10),
                         (2, "unibet_be", "A", "B", "h2h", PRES, 2.30)])
    sortie = _lancer(monkeypatch, capsys, "--db", str(p), "--porte-envoi")
    assert _n(sortie) == 1, "l'opportunité alertée par l'autre book a disparu"
    assert "0 opportunités écartées" in sortie


def test_le_representant_reste_le_MEILLEUR_PRIX(tmp_path, capsys, monkeypatch):
    """Le rejeu décide QUELLES opportunités restent, jamais à quel prix. Le
    représentant est la meilleure cote, comme dans tous les autres régimes :
    faire varier la population ET le prix retenu ferait bouger deux choses à
    la fois, et l'écart mesuré ne serait attribuable à ni l'une ni l'autre."""
    p = _base(tmp_path, [(1, "ladbrokes_be", "A", "B", "h2h", LOIN, 2.10),
                         (2, "unibet_be", "A", "B", "h2h", PRES, 2.30)])
    sortie = _lancer(monkeypatch, capsys, "--db", str(p), "--porte-envoi",
                     "--lister")
    assert "2.30" in sortie or "2,30" in sortie


def test_toutes_les_lignes_dans_la_fenetre_ecartent_l_opportunite(
        tmp_path, capsys, monkeypatch):
    p = _base(tmp_path, [(1, "ladbrokes_be", "A", "B", "h2h", PRES, 2.10),
                         (2, "unibet_be", "A", "B", "h2h", PRES, 2.30),
                         (3, "unibet_be", "C", "D", "h2h", LOIN, 2.20)])
    sortie = _lancer(monkeypatch, capsys, "--db", str(p), "--porte-envoi")
    assert _n(sortie) == 1
    assert "1 opportunités écartées sur 2" in sortie


# ── LE PIÈGE N°3 : le rejeu se falsifie lui-même ─────────────────────

def test_le_controle_sur_les_paris_cliques_s_imprime(tmp_path, capsys,
                                                     monkeypatch):
    """Un pari CLIQUÉ vient du bouton d'un message Telegram : il a forcément
    été alerté. Si le rejeu en tue, c'est le rejeu qui a tort. Le nombre
    s'imprime QU'IL SOIT NUL OU NON — un contrôle qu'on ne montre que
    lorsqu'il passe n'est pas un contrôle."""
    p = _base(tmp_path, [(1, "unibet_be", "A", "B", "h2h", LOIN, 2.10),
                         (2, "unibet_be", "C", "D", "h2h", LOIN, 2.20)],
              joues=(1,))
    sortie = _lancer(monkeypatch, capsys, "--db", str(p), "--porte-envoi")
    assert "CONTRÔLE" in sortie
    assert "dont 0 déjà CLIQUÉS" in sortie


def test_un_rejeu_trop_large_est_marque(tmp_path, capsys, monkeypatch):
    """Le pari cliqué a été détecté 4 minutes avant le coup d'envoi : la
    production l'a pourtant envoyé (l'alerte est partie plus tôt, ou le
    réglage a changé depuis). Le rejeu doit dire qu'il vient de tuer un pari
    dont on SAIT qu'il est parti."""
    p = _base(tmp_path, [(1, "unibet_be", "A", "B", "h2h", PRES, 2.10),
                         (2, "unibet_be", "C", "D", "h2h", LOIN, 2.20)],
              joues=(1,))
    sortie = _lancer(monkeypatch, capsys, "--db", str(p), "--porte-envoi")
    assert "dont 1 déjà CLIQUÉS" in sortie
    assert "trop large" in sortie


def test_les_fantomes_sortent_du_lot_NON_JOUE(tmp_path, capsys, monkeypatch):
    """Le résultat utile : le lot « alerté et non joué » perd les paris qui ne
    sont jamais partis. C'est exactement la population que la comparaison
    joué / non-joué prétendait mesurer."""
    lignes = [(1, "unibet_be", "A", "B", "h2h", LOIN, 2.10),
              (2, "unibet_be", "C", "D", "h2h", LOIN, 2.20),
              (3, "unibet_be", "E", "F", "h2h", PRES, 2.30),
              (4, "unibet_be", "G", "H", "h2h_h1", LOIN, 2.40)]
    p = _base(tmp_path, lignes, joues=(1,))

    avant = _lancer(monkeypatch, capsys, "--db", str(p), "--joues", "non")
    apres = _lancer(monkeypatch, capsys, "--db", str(p), "--joues", "non",
                    "--porte-envoi")
    assert _n(avant) == 3
    assert _n(apres) == 1, "les deux fantômes restent dans le lot non joué"

    # Le lot JOUÉ, lui, ne bouge pas : par construction il ne contient aucun
    # fantôme. Si ce nombre changeait, le rejeu tuerait des paris alertés.
    joue = _lancer(monkeypatch, capsys, "--db", str(p), "--joues", "oui",
                   "--porte-envoi")
    assert _n(joue) == 1


def test_une_population_vidée_par_le_rejeu_le_dit(tmp_path, capsys, monkeypatch):
    """« Aucun pari ne correspond » ne doit jamais pouvoir se lire « aucun pari
    n'est rentable » : le garde de population vide doit nommer le rejeu parmi
    les critères."""
    p = _base(tmp_path, [(1, "unibet_be", "A", "B", "h2h", PRES, 2.10)])
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--porte-envoi"])
    with pytest.raises(SystemExit) as e:
        main()
    assert "porte d'envoi rejouée" in str(e.value)


# ── La strate écartée doit se MESURER, pas se déduire ────────────────

def test_la_strate_ecartee_est_mesuree_dans_la_meme_invocation(
        tmp_path, capsys, monkeypatch):
    """⚠️ POURQUOI ELLE S'IMPRIME. La question à laquelle ce rejeu sert à
    répondre est « ces paris-là avaient-ils une CLV haute et un ROI mauvais ? ».
    On pourrait croire la déduire en soustrayant un run avec drapeau d'un run
    sans — c'est faux : entre deux invocations la base gagne des clôtures et
    des résultats, et la soustraction met cette dérive sur le dos du filtre.
    Mesuré le 12/09 : le lot JOUÉ, dont le rejeu ne retire rien, passait de
    951 à 955 clôtures d'un run à l'autre."""
    p = _base(tmp_path, [(1, "unibet_be", "A", "B", "h2h", LOIN, 2.10),
                         (2, "unibet_be", "C", "D", "h2h", PRES, 2.20),
                         (3, "unibet_be", "E", "F", "h2h_h1", LOIN, 2.30)])
    sortie = _lancer(monkeypatch, capsys, "--db", str(p), "--porte-envoi")
    assert "LA STRATE ÉCARTÉE" in sortie
    assert "TOTAL écarté" in sortie
    # Les deux raisons sont ventilées SÉPARÉMENT : un lot écarté à 90 % de
    # mi-temps et un lot écarté à 90 % de fenêtre morte appellent des
    # conclusions opposées.
    assert "mi-temps" in sortie
    assert "fenêtre morte" in sortie


def test_la_capture_de_cloture_des_deux_lots_est_comparee(tmp_path, capsys,
                                                          monkeypatch):
    """C'est ce qui distingue les deux mécanismes soupçonnés : une strate SANS
    clôture ne peut pas avoir fait dégénérer la CLV — elle n'en a pas."""
    p = _base(tmp_path, [(1, "unibet_be", "A", "B", "h2h", LOIN, 2.10),
                         (2, "unibet_be", "C", "D", "h2h", PRES, 2.20)])
    sortie = _lancer(monkeypatch, capsys, "--db", str(p), "--porte-envoi")
    assert "dans le lot GARDÉ" in sortie


def test_une_raison_par_groupe_meme_quand_les_lignes_different(
        tmp_path, capsys, monkeypatch):
    """Un groupe dont toutes les lignes sont tues peut l'être pour DEUX
    raisons. L'étiquette combinée existe pour que le cas se voie au lieu
    d'être attribué arbitrairement à l'une des deux."""
    from scripts.clv_roi_matrix import _raison_non_alertable
    assert _raison_non_alertable(_ligne(LOIN, J, "h2h_h1"), 15) == "mi-temps"
    assert _raison_non_alertable(_ligne(PRES, J, "h2h"), 15) == "fenêtre morte"
    assert _raison_non_alertable(_ligne(LOIN, J, "h2h"), 15) is None
    # La mi-temps l'emporte quand les deux s'appliquent : c'est l'ordre du
    # code de production, où le garde mi-temps est testé en premier.
    assert _raison_non_alertable(_ligne(PRES, J, "h2h_h1"), 15) == "mi-temps"
