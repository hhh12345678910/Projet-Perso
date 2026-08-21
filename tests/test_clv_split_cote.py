"""L'axe « cote » de `clv_split` — pour lire la CLV en face du P&L.

Le §21.17 a trouvé que les deux se contredisent sur les grosses cotes : CLV
plate, P&L à −20,34 % sur la tranche 4,0-6,0. Le §9 et le §17.3 concluaient
« la cote n'est pas un critère », mais sur la CLV SEULE — aucune analyse
fondée sur elle ne pouvait voir le motif.

⚠️ Le test qui compte est `test_les_bornes_sont_celles_de_pnl_detections` :
deux découpages différents rendraient la contradiction illisible, et c'est
très exactement ce qu'on cherche à mesurer.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from scripts import clv_split
from src.models import Book, MarketType, Outcome, ValueBet
from src.storage import Storage

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _base(tmp_path, paris):
    """`paris` = [(cote, clv)]. clv_pct = odd / closing_fair - 1."""
    db = str(tmp_path / "t.db")
    st = Storage(db)
    for i, (cote, clv) in enumerate(paris, start=1):
        ek = f"2026080112{i:04d}::a__vs__b"
        st.upsert_event(ek, "soccer", "L", "A", "B", T0 + timedelta(hours=3))
        vid = st.insert_value_bet(ValueBet(
            event_key=ek, book=Book.UNIBET_BE, market=MarketType.H2H,
            outcome=Outcome(label="home"), odd_taken=cote, fair_prob=0.5,
            fair_odd=1.9, ev_pct=10.0, kelly_stake_pct=1.0,
            detected_at=T0 + timedelta(minutes=i)))
        st.insert_clv_snapshot(
            value_bet_id=vid, pinnacle_odd=cote, pinnacle_prob=0.5,
            snapshot_at=T0 + timedelta(hours=2), closing=True,
            fair_odd=cote / (1.0 + clv), fair_prob=0.5, overround=1.02)
    return db


def _sortie(capsys, db, *args):
    argv = sys.argv
    sys.argv = ["clv_split", "--db", db, *args]
    try:
        clv_split.main()
    finally:
        sys.argv = argv
    return capsys.readouterr().out


def test_les_bornes_sont_celles_de_pnl_detections():
    """Les deux tables doivent se superposer, sinon la comparaison est fausse."""
    from scripts.pnl_detections import main  # noqa: F401  (import = le module existe)
    src = open("scripts/pnl_detections.py", encoding="utf-8").read()
    assert '("1.0-1.8", 1.0, 1.8)' in src, "les bornes de pnl_detections ont bougé"
    assert clv_split._bande_cote(1.5) == "1.0-1.8"
    assert clv_split._bande_cote(1.8) == "1.8-2.3"    # borne basse incluse
    assert clv_split._bande_cote(5.99) == "4.0-6.0"
    assert clv_split._bande_cote(6.0) == "> 6.0"      # borne haute exclue


def test_une_cote_absente_ne_fait_pas_tomber_la_sonde():
    assert clv_split._bande_cote(None) == "?"
    assert clv_split._bande_cote(0.0) == "?"


def test_la_clv_est_decoupee_par_tranche(tmp_path, capsys):
    """Deux populations de CLV connue, dans deux tranches distinctes."""
    paris = [(1.5, 0.10)] * 6 + [(5.0, -0.04)] * 6
    out = _sortie(capsys, _base(tmp_path, paris), "--by", "cote", "--min", "5")

    assert "1.0-1.8" in out and "4.0-6.0" in out
    lignes = [l for l in out.splitlines() if l.startswith(("1.0-1.8", "4.0-6.0"))]
    assert len(lignes) == 2
    assert "+10.00 %" in lignes[0]
    assert "-4.00 %" in lignes[1]


def test_la_table_se_lit_dans_l_ordre_des_cotes(tmp_path, capsys):
    """Une table de cotes triée par effectif est illisible : on veut 1,0 puis
    1,8 puis 2,3, quel que soit le nombre de paris dans chacune."""
    paris = [(5.0, 0.02)] * 20 + [(1.5, 0.02)] * 6
    out = _sortie(capsys, _base(tmp_path, paris), "--by", "cote", "--min", "5")

    lignes = [l.split()[0] for l in out.splitlines() if l.startswith(("1.0-", "4.0-"))]
    assert lignes == ["1.0-1.8", "4.0-6.0"], lignes
