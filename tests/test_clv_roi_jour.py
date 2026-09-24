"""L'axe « jour du match » et le bloc week-end contre semaine.

TROIS PIÈGES, TOUS SILENCIEUX
-----------------------------
1. LE JOUR EN UTC. Un match lancé à 00 h 30 à Bruxelles le dimanche est à
   22 h 30 UTC le samedi. Lu en UTC, il change de jour — et c'est justement la
   frontière du week-end qui bouge. Le tableau serait faux sans aucune erreur.

2. LE JOUR DE LA DÉTECTION. La question est « je gagne le week-end » : elle
   parle du jour où le résultat tombe, donc du MATCH. Un pari détecté le
   mercredi pour un match du samedi est un pari du samedi.

3. UN PARI SANS LIGNE `events`. Il porte son heure dans sa clé ; le perdre
   dans « sans horaire » retirerait des paris de la comparaison sans le dire.
"""
from __future__ import annotations

import sqlite3

from scripts.clv_roi_matrix import (JOURS, ORDRE_JOUR, SANS_HORAIRE, _axe,
                                    _bande_jour, main)


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
    """Vendredi 18/09 23 h 30 UTC = samedi 01 h 30 local : c'est un match du
    week-end, et le lire en UTC le rangerait en semaine."""
    assert _bande_jour(_r("2026-09-18T23:30:00+00:00")) == "samedi"


def test_l_heure_d_hiver_est_suivie():
    """En janvier, Bruxelles est à UTC+1 : samedi 10/01 22 h 30 UTC est encore
    samedi (23 h 30 local), 23 h 30 UTC est dimanche (00 h 30 local). Un
    décalage fixe de +2 h se tromperait sur le premier."""
    assert _bande_jour(_r("2026-01-10T22:30:00+00:00")) == "samedi"
    assert _bande_jour(_r("2026-01-10T23:30:00+00:00")) == "dimanche"


def test_sans_ligne_events_l_heure_est_lue_dans_la_cle():
    """Clé `AAAAMMJJHHMM` en UTC : 20/09 à 12 h 00 UTC, un dimanche."""
    assert _bande_jour(_r(None, "202609201200::anderlecht__vs__genk")) == "dimanche"


def test_sans_aucun_horaire_la_ligne_a_sa_propre_bande():
    assert _bande_jour(_r(None, "cle-illisible")) == SANS_HORAIRE
    assert _bande_jour(_R(start_time=None)) == SANS_HORAIRE


def test_l_axe_jour_est_reconnu_et_ordonne_lundi_d_abord():
    titre, bande_de, ordre = _axe("jour")
    assert titre == "jour du match"
    assert ordre == ORDRE_JOUR
    assert ordre[0] == "lundi" and ordre[6] == "dimanche" and len(ordre) == 8
    assert bande_de is _bande_jour


# ── Le tableau et le bloc week-end ───────────────────────────────────

def _base(tmp_path, lignes, sport="soccer"):
    """`lignes` : (id, coup d'envoi ISO, détection ISO, cote, clôture, gagnant)."""
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
    for (i, depart, detecte, cote, clot, gagnant) in lignes:
        ek = (depart[:16].replace("-", "").replace("T", "").replace(":", "")
              + f"::club{i}__vs__autre{i}")
        c.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                  (ek, sport, "L1", f"Club{i}", f"Autre{i}", depart))
        c.execute("INSERT INTO value_bets VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (i, ek, "unibet_be", "h2h", "home", None, cote,
                   cote / 1.08, 8.0, detecte))
        if clot is not None:
            c.execute("INSERT INTO clv_snapshots VALUES (?,?,?,?)",
                      (i, i, 1, clot))
        if gagnant is not None:
            c.execute("INSERT INTO results VALUES (?,?,?,?)",
                      (ek, gagnant, 1, 0))
    c.commit()
    c.close()
    return p


# Samedi 19/09 et dimanche 20/09 (week-end), mardi 15/09 et mercredi 16/09.
_WE = ["2026-09-19T15:00:00+00:00", "2026-09-20T15:00:00+00:00"]
_SEM = ["2026-09-15T18:00:00+00:00", "2026-09-16T18:00:00+00:00"]


def _jeu():
    """Week-end : CLV haute, gagnés. Semaine : CLV basse, perdus."""
    lignes, i = [], 0
    for depart in _WE:
        for clot in (1.80, 1.85, 1.90):
            i += 1
            lignes.append((i, depart, "2026-09-14T10:00:00+00:00", 2.10, clot, "home"))
    for depart in _SEM:
        for clot in (2.05, 2.08, 2.12):
            i += 1
            lignes.append((i, depart, "2026-09-14T10:00:00+00:00", 2.10, clot, "away"))
    return lignes


def test_le_tableau_range_les_paris_par_jour_du_match(tmp_path, capsys, monkeypatch):
    """Tous détectés un lundi : si l'axe lisait la détection, tout tomberait
    en « lundi ». Il doit lire le coup d'envoi."""
    p = _base(tmp_path, _jeu())
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--axe", "jour"])
    main()
    out = capsys.readouterr().out
    lignes = out.splitlines()
    for jour in ("samedi", "dimanche", "mardi", "mercredi"):
        assert any(l.startswith("soccer") and f" {jour} " in l for l in lignes), jour
    assert not any(l.startswith("soccer") and " lundi " in l for l in lignes)


def test_le_bloc_week_end_compare_les_deux_lots(tmp_path, capsys, monkeypatch):
    p = _base(tmp_path, _jeu())
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--axe", "jour"])
    main()
    out = capsys.readouterr().out
    assert "WEEK-END CONTRE SEMAINE" in out
    bloc = out.split("WEEK-END CONTRE SEMAINE", 1)[1].splitlines()
    tous = [l for l in bloc if l.startswith("TOUS")][0]
    # 6 paris de chaque côté, et un écart de CLV POSITIF (week-end meilleur).
    assert "week-end" in tous and "     6 " in tous, tous
    assert "+" in tous.split("pt")[0].split()[-1], tous
    assert any(l.strip().startswith("semaine") for l in bloc)
    # La même comparaison sport par sport, pour séparer jour et composition.
    assert any(l.startswith("soccer") for l in bloc)


def test_le_bloc_n_apparait_que_sur_l_axe_jour(tmp_path, capsys, monkeypatch):
    p = _base(tmp_path, _jeu())
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--axe", "semaine"])
    main()
    assert "WEEK-END CONTRE SEMAINE" not in capsys.readouterr().out


def test_un_seul_cote_present_ne_fait_pas_planter(tmp_path, capsys, monkeypatch):
    """Rien que du week-end : pas de comparaison possible, pas d'exception."""
    lignes = [(i, _WE[0], "2026-09-14T10:00:00+00:00", 2.10, 1.9, "home")
              for i in range(1, 4)]
    p = _base(tmp_path, lignes)
    monkeypatch.setattr("sys.argv", ["m", "--db", str(p), "--axe", "jour"])
    main()
    out = capsys.readouterr().out
    assert "WEEK-END CONTRE SEMAINE" in out
    bloc = out.split("WEEK-END CONTRE SEMAINE", 1)[1]
    assert "TOUS" not in bloc.split("⚠️")[0]


def test_les_jours_du_week_end_sont_bien_samedi_et_dimanche():
    from scripts.clv_roi_matrix import WEEK_END
    assert WEEK_END == {"samedi", "dimanche"}
    assert set(WEEK_END) <= set(JOURS)
