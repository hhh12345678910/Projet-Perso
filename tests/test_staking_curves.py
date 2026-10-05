"""Les courbes de mise : fixe contre Kelly, et ce qu'il faut encaisser."""
from __future__ import annotations

import csv
import sqlite3

import pytest

from scripts import staking_curves as sc
from src.ev import kelly_stake
from src.storage import Storage


# ── Les mesures, sans base ────────────────────────────────────────────

def test_le_creux_est_mesure_de_pic_a_creux():
    dd, sommet, depuis = sc._drawdown([10, 5, 15, 0, 20])
    assert (dd, sommet, depuis) == (15, 20, 15)


def test_le_ratio_PnL_par_creux_ne_depend_pas_du_niveau_de_mise():
    """Doubler toutes les mises double le P&L ET le creux : le ratio et le
    creux exprimé en mises ne bougent pas — c'est ce qui compare deux schémas."""
    dates = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    gains = [25.0, -25.0, -25.0, 60.0]
    courbe = [25.0, 0.0, -25.0, 35.0]
    a = sc.mesures(courbe, dates, [25.0] * 4, gains, 1000)
    b = sc.mesures([2 * v for v in courbe], dates, [50.0] * 4, [2 * g for g in gains], 1000)
    assert a["dd"] == 50 and b["dd"] == 100
    assert a["pnl_par_creux"] == b["pnl_par_creux"] == pytest.approx(35 / 50)
    assert a["dd_mises"] == b["dd_mises"] == 2.0


def test_la_plus_longue_periode_sous_le_sommet():
    dates = ["2026-09-01", "2026-09-03", "2026-09-10", "2026-09-12", "2026-09-20"]
    # Sommet 30 le 03/09, retour à 30 le 12/09 : 9 jours ; puis rechute en cours.
    assert sc._sous_le_sommet([10, 30, 5, 30, 20], dates) == (9, "2026-09-03", "2026-09-12")
    assert sc._sous_le_sommet([10, 30, 5, 10, 20], dates) == (17, "2026-09-03", "en cours")
    assert sc._sous_le_sommet([10, 20, 30], dates[:3]) == (0, "", "")


@pytest.mark.parametrize("brut, attendu", [("0.25", [0.25]), ("1/4,1/2", [0.25, 0.5]),
                                           ("0.25, 1", [0.25, 1.0])])
def test_les_fractions(brut, attendu):
    assert sc._fractions(brut) == attendu


@pytest.mark.parametrize("brut", ["0", "1.5", "-0.25", ""])
def test_une_fraction_hors_bornes_est_refusee(brut):
    with pytest.raises(SystemExit):
        sc._fractions(brut)


# ── De bout en bout, sur tes paris joués ──────────────────────────────

def _base(tmp_path):
    db = tmp_path / "t.db"
    Storage(db)
    c = sqlite3.connect(db)
    # (cote, cote juste, gagnant, joué ?) — cinq matchs, trois joués.
    paris = [(2.0, 1.80, "home", True), (3.0, 2.60, "away", True),
             (2.5, 2.20, "home", False), (1.8, 1.65, "away", True),
             (4.0, 3.30, "home", False)]
    for i, (cote, juste, gagnant, joue) in enumerate(paris, start=1):
        ek = f"2026090{i}1800::h{i}__vs__a{i}"
        c.execute("INSERT INTO events VALUES (?, 'soccer', 'L', ?, ?, ?)",
                  (ek, f"h{i}", f"a{i}", f"2026-09-0{i}T18:00:00+00:00"))
        c.execute("INSERT INTO value_bets(event_key,book,market,outcome_label,line,odd_taken,"
                  "fair_prob,fair_odd,ev_pct,kelly_pct,detected_at) VALUES "
                  "(?, 'unibet_be', 'h2h', 'home', NULL, ?, ?, ?, ?, 1.0, ?)",
                  (ek, cote, 1 / juste, juste, 100 * (cote / juste - 1), f"2026-09-0{i}T10:00:00"))
        vb = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO results(event_key, winner, home_score, away_score, source, settled_at)"
                  " VALUES (?, ?, 1, 0, 't', 'x')", (ek, gagnant))
        if joue:
            c.execute("INSERT INTO played_bets(dedup_key, played_at, value_bet_id, odd_taken, stake)"
                      " VALUES (?, 'x', ?, ?, 35)", (f"{ek}|h2h|home|None", vb, cote))
    c.commit()
    return db


def test_seuls_tes_paris_joues_et_la_formule_kelly_de_production(tmp_path, capsys):
    from src.alerter import _MAX_STAKE_PCT, _round_stake
    db = _base(tmp_path)
    out = tmp_path / "c.csv"
    assert sc.main(["--db", str(db), "--joues", "--fractions", "1/4", "--bankroll", "10000",
                    "--out", str(out)]) == 0
    texte = capsys.readouterr().out
    assert "3 paris réglés" in texte and "tes paris JOUÉS" in texte
    lignes = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [l["date"] for l in lignes] == ["2026-09-01", "2026-09-02", "2026-09-04"]
    # Mise Kelly = formule de production, plafond et arrondi de production.
    brute = min(kelly_stake(2.0, 1 / 1.80, 10000, 0.25), 10000 * _MAX_STAKE_PCT / 100)
    assert float(lignes[0]["mise_kelly_0.25"]) == _round_stake(brute)
    # La ligne « à mise moyenne égale » engage en moyenne exactement la mise fixe.
    egal = [float(l["mise_kelly_0.25_egal"]) for l in lignes]
    assert sum(egal) / len(egal) == pytest.approx(35.0, abs=0.02)
    assert "Kelly 1/4 à 35 € moy." in texte and "P&L/creux" in texte


def test_sans_joues_toute_la_population_est_lue(tmp_path, capsys):
    db = _base(tmp_path)
    assert sc.main(["--db", str(db), "--fractions", "0.25,0.5", "--bankroll", "5000"]) == 0
    texte = capsys.readouterr().out
    assert "5 paris réglés" in texte
    assert "Kelly 1/4" in texte and "Kelly 1/2" in texte and "fixe 35 €" in texte


# ── Les paliers d'EV : la mise ronde qui ne dépend que de l'EV ────────

def test_lire_et_appliquer_des_paliers():
    from src.alerter import lire_paliers, mise_palier
    p = lire_paliers("15:50, 0:25,8:35")
    assert p == [(0.0, 25.0), (8.0, 35.0), (15.0, 50.0)]
    assert [mise_palier(e, p) for e in (None, 3, 7.99, 8, 14.9, 15, 80)] == \
        [25, 25, 25, 35, 35, 50, 50]
    assert lire_paliers("") == [] and mise_palier(10, []) is None
    for faux in ("8:35,8:40", "8:0", "x:35", "8"):
        with pytest.raises(ValueError):
            lire_paliers(faux)


def test_les_alertes_suivent_les_paliers_seulement_s_ils_sont_definis(monkeypatch):
    import src.alerter as al
    monkeypatch.setattr(al, "_STAKE_MODE", "flat")
    monkeypatch.setattr(al, "_STAKE_PCT", 0.0)
    monkeypatch.setattr(al, "_STAKE_BASE_EUR", 35.0)
    monkeypatch.setattr(al, "_STAKE_EV_PALIERS", [])
    assert al._advised_stake_eur(9.0, None, 1000) == 35          # règle historique
    monkeypatch.setattr(al, "_STAKE_EV_PALIERS", [(0, 25.0), (8, 35.0), (15, 50.0)])
    assert al._advised_stake_eur(6.0, None, 1000) == 25
    assert al._advised_stake_eur(20.0, None, 1000) == 50
    assert al._advised_stake_line(20.0, None, 1000) == "Mise conseillée : 50€  ⚡"
    assert al._advised_stake_line(6.0, None, 1000) == "Mise conseillée : 25€"


def test_les_paliers_auto_ne_lisent_que_l_EV():
    """Proportionnels à l'EV médiane de chaque bande, bornés à ½–2× la mise,
    moyenne ≈ la mise, croissants — et ronds."""
    from src.alerter import _round_stake
    evs = [5.5] * 40 + [10.0] * 40 + [20.0] * 15 + [60.0] * 5
    p = sc.paliers_auto(evs, 35.0, _round_stake)
    assert [s for s, _m in p] == [5.0, 8.0, 15.0, 35.0]
    mises = [m for _s, m in p]
    assert mises == sorted(mises) and all(m % 5 == 0 for m in mises)
    assert all(17.5 <= m <= 70 for m in mises)
    from src.alerter import mise_palier
    moy = sum(mise_palier(e, p) for e in evs) / len(evs)
    assert moy == pytest.approx(35.0, abs=3.0)


def test_la_comparaison_montre_les_paliers_et_la_ligne_env(tmp_path, capsys):
    db = _base(tmp_path)
    assert sc.main(["--db", str(db), "--fractions", "1/4", "--bankroll", "2100",
                    "--paliers", "0:25,8:35,15:50"]) == 0
    texte = capsys.readouterr().out
    assert "paliers EV auto" in texte and "paliers EV testés" in texte
    assert "STAKE_EV_PALIERS=0:25,8:35,15:50" in texte
    assert "montants" in texte


def test_des_paliers_mal_ecrits_sont_refuses(tmp_path):
    with pytest.raises(SystemExit):
        sc.main(["--db", str(_base(tmp_path)), "--paliers", "8:35,8:40"])
