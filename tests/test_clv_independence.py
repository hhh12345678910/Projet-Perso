"""La sonde d'indépendance de la CLV (§20.4).

Chaque test construit une population dont on connaît la réponse à l'avance :
la sonde doit rendre le bon verdict, pas seulement un tableau plausible.
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone

from scripts import clv_independence
from src.models import Book, MarketType, Outcome, ValueBet
from src.storage import Storage


def _base(tmp_path, paris):
    """paris : liste de (fair_détection, fair_clôture, cote_prise)."""
    db = str(tmp_path / "t.db")
    st = Storage(db)
    t0 = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    for n, (f, c, odd) in enumerate(paris, 1):
        ek = f"2026080112{n:05d}::a__vs__b"
        st.upsert_event(ek, "soccer", "L", "A", "B", t0 + timedelta(hours=3))
        vid = st.insert_value_bet(ValueBet(
            event_key=ek, book=Book.UNIBET_BE, market=MarketType.H2H,
            outcome=Outcome(label="home"), odd_taken=odd, fair_prob=1.0 / f,
            fair_odd=f, ev_pct=(odd / f - 1.0) * 100.0, kelly_stake_pct=1.0,
            detected_at=t0 + timedelta(minutes=n),
        ))
        st.insert_clv_snapshot(
            value_bet_id=vid, pinnacle_odd=c * 1.03, pinnacle_prob=1.0 / c,
            snapshot_at=t0 + timedelta(hours=2), closing=True,
            fair_odd=c, fair_prob=1.0 / c, overround=1.03,
        )
    return db


def _sortie(capsys, db, *args):
    argv = sys.argv
    sys.argv = ["clv_independence", "--db", db, *args]
    try:
        clv_independence.main()
    finally:
        sys.argv = argv
    return capsys.readouterr().out


def test_ligne_immobile_denonce_la_clv_qui_repete_l_ev(tmp_path, capsys):
    """Ligne juste identique à la clôture : CLV = EV, R² = 1, et la sonde doit
    dire que la CLV n'a jamais rien validé."""
    rnd = random.Random(1)
    paris = [(f, f, f * rnd.uniform(1.05, 1.20))
             for _ in range(300) for f in [rnd.uniform(1.8, 4.0)]]
    out = _sortie(capsys, _base(tmp_path, paris))
    assert "100.0 %" in out            # toutes immobiles
    assert "RIGOUREUSEMENT identique" in out
    assert "réénonce" in out


def test_marche_qui_corrige_est_le_cas_sain(tmp_path, capsys):
    """La clôture rejoint la cote prise : l'edge s'évapore, CLV ≈ 0 et
    décorrélée de l'EV. C'est le cas où la CLV mérite son statut de KPI."""
    rnd = random.Random(2)
    paris = []
    for _ in range(400):
        f = rnd.uniform(1.8, 4.0)
        odd = f * rnd.uniform(1.05, 1.20)
        paris.append((f, odd * rnd.uniform(0.99, 1.01), odd))   # clôture ≈ cote prise
    out = _sortie(capsys, _base(tmp_path, paris))
    assert "cas SAIN" in out
    assert "réénonce" not in out


def test_immobilite_partielle_est_chiffree_sans_alarme(tmp_path, capsys):
    """Un dixième de lignes immobiles se lit comme tel sans déclencher l'alarme.

    Une part d'immobilité est structurelle — détection sur la dernière cote
    avant le coup d'envoi. L'alarme est réservée aux proportions que cette
    explication ne couvre plus (> 20 %).
    """
    rnd = random.Random(4)
    paris = []
    for i in range(400):
        f = rnd.uniform(1.8, 4.0)
        odd = f * rnd.uniform(1.05, 1.20)
        paris.append((f, f if i % 10 == 0 else odd * rnd.uniform(.98, 1.02), odd))
    out = _sortie(capsys, _base(tmp_path, paris))
    assert "( 10.0 %)" in out
    assert "RIGOUREUSEMENT identique" not in out


def test_immobilite_massive_declenche_l_alarme(tmp_path, capsys):
    """Au-delà du seuil, l'alarme doit partir — c'est sa raison d'être."""
    rnd = random.Random(6)
    paris = []
    for i in range(400):
        f = rnd.uniform(1.8, 4.0)
        odd = f * rnd.uniform(1.05, 1.20)
        paris.append((f, f if i % 2 == 0 else odd * rnd.uniform(.98, 1.02), odd))
    out = _sortie(capsys, _base(tmp_path, paris))
    assert "RIGOUREUSEMENT identique" in out


def test_base_vide_ne_plante_pas(tmp_path, capsys):
    out = _sortie(capsys, _base(tmp_path, []))
    assert "Trop peu de paris" in out
