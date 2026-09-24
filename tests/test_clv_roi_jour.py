"""L'axe « jour du match » et le bloc week-end contre semaine.

PIÈGES DU JOUR, TOUS SILENCIEUX
-------------------------------
1. LE JOUR EN UTC. Un match lancé à 00 h 30 à Bruxelles le dimanche est à
   22 h 30 UTC le samedi. Lu en UTC, il change de jour — et c'est justement la
   frontière du week-end qui bouge.

2. LE JOUR DE LA DÉTECTION. La question est « je gagne le week-end » : elle
   parle du jour où le résultat tombe, donc du MATCH.

3. UN PARI SANS LIGNE `events`. Il porte son heure dans sa clé ; sans repli il
   tomberait dans « sans horaire », et sans clé de dédup propre il fusionnerait
   avec tous les autres paris orphelins.

LE PIÈGE STATISTIQUE, PAYÉ UNE FOIS
-----------------------------------
La première version marquait d'un ✔ jusqu'à dix lignes au seuil simple, en
traitant comme indépendants les paris d'un même match. Simulée sans aucun
effet de jour, elle affichait un ✔ une fois sur trois. Un seul test porte
désormais la marque, et il doit tenir par match ET par journée.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from scripts.clv_roi_matrix import (JOURS, ORDRE_JOUR, SANS_HORAIRE, WEEK_END,
                                    _axe, _bande_jour, _cle_dedup,
                                    _seuil_student, main)
from src.clv import clv_pct


class _R(dict):
    """Une ligne qui répond à `keys()` comme un sqlite3.Row."""
    def keys(self):
        return dict.keys(self)


def _r(start=None, ek="20260101-invalide::a__vs__b"):
    return _R(start_time=start, event_key=ek)


# ── Le jour, à l'heure locale ────────────────────────────────────────

def test_un_match_de_minuit_trente_est_du_dimanche_pas_du_samedi():
    """Samedi 19/09 22 h 30 UTC = dimanche 20/09 00 h 30 à Bruxelles (UTC+2)."""
    assert _bande_jour(_r("2026-09-19T22:30:00+00:00")) == "dimanche"


def test_le_vendredi_soir_tard_bascule_dans_le_week_end():
    assert _bande_jour(_r("2026-09-18T23:30:00+00:00")) == "samedi"


def test_l_heure_d_hiver_est_suivie():
    """En janvier Bruxelles est à UTC+1 : un décalage fixe de +2 h se
    tromperait sur le premier des deux."""
    assert _bande_jour(_r("2026-01-10T22:30:00+00:00")) == "samedi"
    assert _bande_jour(_r("2026-01-10T23:30:00+00:00")) == "dimanche"


def test_sans_ligne_events_l_heure_est_lue_dans_la_cle():
    assert _bande_jour(_r(None, "202609201200::anderlecht__vs__genk")) == "dimanche"


def test_l_heure_de_la_cle_est_en_utc_puis_convertie():
    """La clé est en UTC : 22 h 30 UTC le samedi est dimanche à Bruxelles. La
    lire comme une heure locale laisserait ce match au samedi."""
    assert _bande_jour(_r(None, "202609192230::a__vs__b")) == "dimanche"


def test_sans_aucun_horaire_la_ligne_a_sa_propre_bande():
    assert _bande_jour(_r(None, "cle-illisible")) == SANS_HORAIRE
    assert _bande_jour(_R(start_time=None)) == SANS_HORAIRE


def test_le_fuseau_est_lu_a_l_appel_pas_a_l_import(monkeypatch):
    """`.env` est chargé APRÈS l'import : un fuseau figé à l'import ignorerait
    un TZ_RAPPORT posé dans `.env`."""
    monkeypatch.setenv("TZ_RAPPORT", "UTC")
    assert _bande_jour(_r("2026-09-19T22:30:00+00:00")) == "samedi"


def test_l_axe_jour_est_reconnu_et_ordonne_lundi_d_abord():
    titre, bande_de, ordre = _axe("jour")
    assert titre == "jour du match"
    assert ordre == ORDRE_JOUR
    assert ordre[0] == "lundi" and ordre[6] == "dimanche" and len(ordre) == 8
    assert bande_de is _bande_jour
    assert WEEK_END == {"samedi", "dimanche"} and set(WEEK_END) <= set(JOURS)


def test_le_seuil_de_student_n_est_jamais_plus_indulgent_que_1_96():
    assert _seuil_student(0) == float("inf")
    assert _seuil_student(9) == 2.26
    assert _seuil_student(11) == 2.23          # le degré tabulé EN DESSOUS
    assert _seuil_student(5000) == 1.96
    assert all(_seuil_student(d) >= 1.96 for d in range(1, 3000))


# ── La clé d'opportunité des paris orphelins ─────────────────────────

def test_deux_paris_orphelins_de_matchs_differents_restent_distincts():
    """Sans ligne `events`, home/away/start_time sont NULL : la clé ne doit pas
    tomber à ('', '', '', …) pour tous."""
    a = _R(home=None, away=None, start_time=None, market="h2h",
           outcome_label="home", line=None,
           event_key="202609151800::club1__vs__autre1")
    b = _R(a, event_key="202609191500::club2__vs__autre2")
    assert _cle_dedup(a) != _cle_dedup(b)
    assert _cle_dedup(a)[:3] == ("club1", "autre1", "2026-09-15")


# ── Le bloc, sur une base ────────────────────────────────────────────

def _base(tmp_path, lignes):
    """`lignes` : (id, sport, coup d'envoi ISO ou None, cote, clôture,
    gagnant, clé de match ou None). La clé de match permet de mettre
    plusieurs paris sur un même match."""
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
    for (i, sport, depart, cote, clot, gagnant, match) in lignes:
        m = match or i
        # Sans coup d'envoi, la clé elle-même est illisible : sinon le repli
        # sur la clé lui redonnerait un horaire.
        quand = (depart[:16].replace("-", "").replace("T", "").replace(":", "")
                 if depart else "illisible")
        ek = quand + f"::club{m}__vs__autre{m}"
        if depart is not None:
            c.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                      (ek, sport, "L1", f"Club{m}", f"Autre{m}", depart))
        # Un marché par pari : deux paris d'un même match sont deux
        # opportunités distinctes, pas une ligne dédupliquée.
        c.execute("INSERT INTO value_bets VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (i, ek, "unibet_be", "totals", "over", 0.5 + i, cote,
                   cote / 1.08, 8.0, "2026-08-01T10:00:00+00:00"))
        if clot is not None:
            c.execute("INSERT INTO clv_snapshots VALUES (?,?,?,?)", (i, i, 1, clot))
        if gagnant is not None:
            c.execute("INSERT OR IGNORE INTO results VALUES (?,?,?,?)",
                      (ek, None, 5, 5 if gagnant == "over" else 0))
    c.commit()
    c.close()
    return p


def _lancer(p, capsys, monkeypatch, *extra):
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--axe", "jour", *extra])
    main()
    return capsys.readouterr().out


def _bloc(out):
    return out.split("WEEK-END CONTRE SEMAINE", 1)[1]


_SAMEDI, _DIMANCHE = "2026-09-19T15:00:00+00:00", "2026-09-20T15:00:00+00:00"
_MARDI, _MERCREDI = "2026-09-15T18:00:00+00:00", "2026-09-16T18:00:00+00:00"


def _petit_jeu():
    """3 matchs par jour. Week-end : CLV haute, gagnés ; semaine : l'inverse."""
    lignes, i = [], 0
    for depart in (_SAMEDI, _DIMANCHE):
        for clot in (1.80, 1.85, 1.90):
            i += 1
            lignes.append((i, "soccer", depart, 2.10, clot, "over", None))
    for depart in (_MARDI, _MERCREDI):
        for clot in (2.05, 2.08, 2.12):
            i += 1
            lignes.append((i, "soccer", depart, 2.10, clot, "under", None))
    return lignes


def test_le_tableau_range_les_paris_par_jour_du_match(tmp_path, capsys, monkeypatch):
    """Tous détectés le 01/08, un samedi : l'axe doit lire le coup d'envoi."""
    out = _lancer(_base(tmp_path, _petit_jeu()), capsys, monkeypatch)
    lignes = out.splitlines()
    for jour in ("samedi", "dimanche", "mardi", "mercredi"):
        assert any(l.startswith("soccer") and f" {jour} " in l for l in lignes), jour


def test_les_chiffres_descriptifs_sont_exacts(tmp_path, capsys, monkeypatch):
    out = _lancer(_base(tmp_path, _petit_jeu()), capsys, monkeypatch)
    bloc = _bloc(out).splitlines()
    tous = next(l for l in bloc if l.startswith("TOUS"))
    sem = bloc[bloc.index(tous) + 1]
    clv_we = sum(clv_pct(2.10, c) for c in (1.80, 1.85, 1.90)) / 3 * 100
    clv_se = sum(clv_pct(2.10, c) for c in (2.05, 2.08, 2.12)) / 3 * 100
    assert f"{round(clv_we, 2):+.2f}%" in tous, tous
    assert "+110.00%" in tous and "-100.00%" in sem, (tous, sem)
    assert "+210.00 pt" in tous, tous
    assert f"{round(clv_we, 2) - round(clv_se, 2):+.2f} pt" in tous, tous


def test_aucune_ligne_descriptive_ne_porte_de_marque(tmp_path, capsys, monkeypatch):
    """Le ✔ appartient au seul test décisif — jamais aux lignes par sport."""
    out = _lancer(_base(tmp_path, _petit_jeu()), capsys, monkeypatch)
    descriptif = _bloc(out).split("2. Le test qui décide", 1)[0]
    assert "✔" not in descriptif


def test_sous_trente_matchs_il_n_y_a_pas_de_verdict(tmp_path, capsys, monkeypatch):
    out = _lancer(_base(tmp_path, _petit_jeu()), capsys, monkeypatch)
    assert "PAS DE VERDICT" in _bloc(out)
    assert "ÉCART ÉTABLI" not in _bloc(out).replace("PAS D'ÉCART ÉTABLI", "")


def _jeu_long(clv_we, clv_se, par_jour=3, semaines=8, bruit=(0.0, 0.03, -0.03)):
    """`semaines` week-ends et semaines, `par_jour` matchs par jour, CLV par
    côté donnée en fraction via la clôture : clôture = 2.10 / (1 + clv)."""
    lignes, i = [], 0
    lundi = date(2026, 7, 6)
    for s in range(semaines):
        for j in range(7):
            d = lundi + timedelta(days=7 * s + j)
            cible = clv_we if j >= 5 else clv_se
            for k in range(par_jour):
                i += 1
                v = cible + bruit[(i + s + j) % len(bruit)]
                lignes.append((i, "soccer", f"{d.isoformat()}T15:00:00+00:00",
                               2.10, 2.10 / (1 + v), None, None))
    return lignes


def test_un_vrai_ecart_est_etabli(tmp_path, capsys, monkeypatch):
    out = _lancer(_base(tmp_path, _jeu_long(0.15, 0.02)), capsys, monkeypatch)
    assert "✔ ÉCART ÉTABLI : l'edge est plus fort le WEEK-END" in _bloc(out)


def test_sans_ecart_rien_n_est_etabli(tmp_path, capsys, monkeypatch):
    out = _lancer(_base(tmp_path, _jeu_long(0.08, 0.08)), capsys, monkeypatch)
    assert "PAS D'ÉCART ÉTABLI" in _bloc(out)
    assert "✔ ÉCART ÉTABLI" not in _bloc(out)


def test_un_seul_samedi_hors_norme_ne_suffit_pas(tmp_path, capsys, monkeypatch):
    """Tous les matchs du week-end le MÊME jour : par match l'écart paraît
    énorme, mais une journée n'est qu'une observation. La contre-vérification
    par journée doit refuser le ✔."""
    lignes, i = [], 0
    for k in range(40):
        i += 1
        lignes.append((i, "soccer", _SAMEDI, 2.10, 2.10 / (1.15 + 0.01 * (k % 3)),
                       None, None))
    lundi = date(2026, 7, 6)
    for s in range(8):
        for j in range(5):
            d = lundi + timedelta(days=7 * s + j)
            i += 1
            lignes.append((i, "soccer", f"{d.isoformat()}T15:00:00+00:00", 2.10,
                           2.10 / (1.02 + 0.01 * (i % 3)), None, None))
    out = _lancer(_base(tmp_path, lignes), capsys, monkeypatch)
    assert "PAS D'ÉCART ÉTABLI" in _bloc(out)


def test_les_paris_d_un_meme_match_ne_comptent_qu_une_fois(tmp_path, capsys,
                                                          monkeypatch):
    """Cinq paris sur un même match : une seule observation dans le test."""
    lignes = [(i, "soccer", _SAMEDI, 2.10, 1.90, None, "unique")
              for i in range(1, 6)]
    lignes += [(10 + i, "soccer", _MARDI, 2.10, 2.00, None, None)
               for i in range(1, 4)]
    out = _lancer(_base(tmp_path, lignes), capsys, monkeypatch)
    tous = next(l for l in _bloc(out).splitlines() if l.startswith("TOUS"))
    assert tous.split()[2:4] == ["1", "5"], tous    # 1 match, 5 CLV


def test_un_sport_d_un_seul_cote_est_annonce_pas_tu(tmp_path, capsys, monkeypatch):
    lignes = _petit_jeu() + [(100 + i, "tennis", _DIMANCHE, 2.10, 1.5, None, None)
                             for i in range(4)]
    out = _lancer(_base(tmp_path, lignes), capsys, monkeypatch)
    assert "tennis     uniquement le week-end (4 opportunité(s))" in _bloc(out)


def test_rien_que_du_week_end_le_dit(tmp_path, capsys, monkeypatch):
    lignes = [(i, "soccer", _SAMEDI, 2.10, 1.9, None, None) for i in range(1, 4)]
    out = _lancer(_base(tmp_path, lignes), capsys, monkeypatch)
    assert "Aucune comparaison possible" in _bloc(out)


def test_un_pari_sans_horaire_est_compte(tmp_path, capsys, monkeypatch):
    lignes = _petit_jeu() + [(99, "soccer", None, 2.10, 1.9, None, None)]
    out = _lancer(_base(tmp_path, lignes), capsys, monkeypatch)
    assert "1 opportunité(s) sans horaire de match" in _bloc(out)


def test_le_bloc_n_apparait_que_sur_l_axe_jour(tmp_path, capsys, monkeypatch):
    p = _base(tmp_path, _petit_jeu())
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--axe", "semaine"])
    main()
    assert "WEEK-END CONTRE SEMAINE" not in capsys.readouterr().out


def test_la_liste_suit_l_ordre_des_jours_et_la_date_locale(tmp_path, capsys,
                                                         monkeypatch):
    lignes = [(1, "soccer", "2026-09-19T22:30:00+00:00", 2.10, 1.9, None, None),
              (2, "soccer", _MARDI, 2.10, 1.9, None, None)]
    out = _lancer(_base(tmp_path, lignes), capsys, monkeypatch, "--lister")
    liste = out.split("LES PARIS, UN PAR UN", 1)[1]
    assert liste.index("── mardi") < liste.index("── dimanche")
    dimanche = liste.split("── dimanche", 1)[1]
    assert "2026-09-20" in dimanche and "2026-09-19" not in dimanche


def test_un_effet_de_composition_n_est_pas_un_effet_de_jour(tmp_path, capsys,
                                                           monkeypatch):
    """Le football (CLV +15 %) joue surtout le week-end, le tennis (+2 %)
    surtout en semaine — mais DANS chaque sport, aucun écart de jour. Les lots
    bruts diffèrent énormément ; le test à sport constant ne doit rien
    établir."""
    lignes, i = [], 0
    lundi = date(2026, 7, 6)
    for s in range(8):
        for j in range(7):
            d = lundi + timedelta(days=7 * s + j)
            we = j >= 5
            for sport, clv, n in (("soccer", 0.15, 6 if we else 1),
                                  ("tennis", 0.02, 1 if we else 6)):
                for k in range(n):
                    i += 1
                    v = clv + (0.0, 0.03, -0.03)[(i + s) % 3]
                    lignes.append((i, sport, f"{d.isoformat()}T15:00:00+00:00",
                                   2.10, 2.10 / (1 + v), None, None))
    out = _lancer(_base(tmp_path, lignes), capsys, monkeypatch)
    tous = next(l for l in _bloc(out).splitlines() if l.startswith("TOUS"))
    ecart_brut = float(tous.split("pt")[0].split()[-1])
    assert ecart_brut > 5, tous           # le lot brut, lui, « voit » un écart
    assert "PAS D'ÉCART ÉTABLI" in _bloc(out)


def test_le_test_compte_des_matchs_pas_des_paris(tmp_path, capsys, monkeypatch):
    """Deux matchs du week-end à cinq paris chacun : le test annonce 2 matchs,
    pas 10 observations."""
    lignes = [(i, "soccer", _SAMEDI, 2.10, 1.90 + 0.01 * i, None, "a")
              for i in range(1, 6)]
    lignes += [(i, "soccer", _DIMANCHE, 2.10, 1.85 + 0.01 * i, None, "b")
               for i in range(6, 11)]
    lignes += [(20 + i, "soccer", _MARDI, 2.10, 2.00 + 0.01 * i, None, None)
               for i in range(1, 4)]
    out = _lancer(_base(tmp_path, lignes), capsys, monkeypatch)
    assert "matchs : 2 le week-end, 3 en semaine" in _bloc(out)


def test_la_composition_est_neutralisee_meme_journee_par_journee(tmp_path, capsys,
                                                                monkeypatch):
    """Chaque journée-sport est homogène : football le week-end et le lundi
    (CLV +15 %), tennis du mardi au vendredi et un peu le samedi (+2 %). Le test par journée ne
    peut donc pas rattraper un mélange des sports — seule la stratification
    le peut. À football constant (week-end contre lundi), aucun écart."""
    lignes, i = [], 0
    lundi = date(2026, 7, 6)
    for s in range(8):
        for j in range(7):
            d = lundi + timedelta(days=7 * s + j)
            journee = [("soccer", 0.15, 5)] if j in (0, 5, 6) else [("tennis", 0.02, 5)]
            if j == 5:                  # un peu de tennis le samedi aussi
                journee.append(("tennis", 0.02, 2))
            for sport, clv, n in journee:
                for k in range(n):      # 40 lundis de football : au-dessus du plancher
                    i += 1
                    v = clv + (0.0, 0.03, -0.03)[(i + s) % 3]
                    lignes.append((i, sport, f"{d.isoformat()}T15:00:00+00:00",
                                   2.10, 2.10 / (1 + v), None, None))
    out = _lancer(_base(tmp_path, lignes), capsys, monkeypatch)
    tous = next(l for l in _bloc(out).splitlines() if l.startswith("TOUS"))
    assert float(tous.split("pt")[0].split()[-1]) > 5, tous   # le brut « voit » un écart
    assert "PAS D'ÉCART ÉTABLI" in _bloc(out)
    assert "✔ ÉCART ÉTABLI" not in _bloc(out)
