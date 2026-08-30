"""La porte PREMIUM de `clv_split` — mesurer le flux qu'on envoie vraiment.

`clv-report` et `clv_split` mesurent TOUTES les détections. Or le canal
premium n'en reçoit qu'une fraction : deux bandes (EV, cote) dont un pari doit
franchir au moins une. Juger « faut-il garder ce book ? » sur la population
entière répond à une autre question que celle posée.

⚠️ Le test qui compte est `test_les_seuils_viennent_de_la_CONFIGURATION` :
recopier les nombres dans la sonde les ferait dériver au premier
`TELEGRAM_*` changé dans `.env`, et la sonde mesurerait alors autre chose que
la production — exactement le défaut que le §17.7 interdit.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest

from scripts import clv_split
from src.models import Book, MarketType, Outcome, ValueBet
from src.storage import Storage

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

#: (cote, EV, passe la porte avec les seuils PAR DÉFAUT)
#: standard : EV ≥ 8 et 1.5 ≤ cote ≤ 4     longue : EV ≥ 20 et 4 ≤ cote ≤ 6
CAS = [
    (2.00,  9.0, True),    # bande standard
    (2.50, 12.0, True),
    (1.30, 15.0, False),   # cote sous la bande
    (5.00, 12.0, False),   # bonne cote, EV trop faible pour la bande longue
    (5.00, 25.0, True),    # bande longue
    (2.00,  6.0, False),   # EV sous la bande standard
    (7.00, 50.0, False),   # au-delà de la bande longue : le premium n'en veut pas
]


def _base(tmp_path):
    db = str(tmp_path / "t.db")
    st = Storage(db)
    for i, (cote, ev, _) in enumerate(CAS, start=1):
        ek = f"2026080112{i:04d}::a__vs__b"
        st.upsert_event(ek, "soccer", "L", "A", "B", T0 + timedelta(hours=3))
        bid = st.insert_value_bet(ValueBet(
            event_key=ek, book=Book.ELITESPORTS, market=MarketType.H2H,
            outcome=Outcome("home"), odd_taken=cote, fair_prob=0.5,
            fair_odd=2.0, ev_pct=ev, kelly_stake_pct=1.0, detected_at=T0))
        st.insert_clv_snapshot(bid, pinnacle_odd=cote * 0.95, pinnacle_prob=0.5,
                               snapshot_at=T0 + timedelta(hours=3),
                               closing=True, fair_odd=cote * 0.95,
                               fair_prob=0.5, overround=1.05)
    return db


def _lancer(db, monkeypatch, capsys, extra=()):
    monkeypatch.setattr(sys, "argv",
                        ["clv_split", "--by", "book", "--db", db,
                         "--min", "1", *extra])
    clv_split.main()
    return capsys.readouterr().out


def _n(sortie) -> int:
    """L'effectif de la ligne elitesports."""
    for ligne in sortie.splitlines():
        if ligne.startswith("elitesports"):
            return int(ligne.split()[1])
    return 0


def test_sans_la_porte_TOUS_les_paris_comptent(tmp_path, monkeypatch, capsys):
    """Contre-épreuve : sans elle, une porte qui ne filtre rien passerait le
    test suivant."""
    assert _n(_lancer(_base(tmp_path), monkeypatch, capsys)) == len(CAS)


def test_la_porte_ne_garde_QUE_ce_que_le_premium_recevrait(
        tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    sortie = _lancer(_base(tmp_path), monkeypatch, capsys, ("--premium",))
    assert _n(sortie) == sum(1 for _, _, passe in CAS if passe)
    # Et la sortie DIT quels seuils elle a appliqués : une porte muette
    # laisserait croire à un filtre qu'on n'a pas vérifié.
    assert "PORTE PREMIUM" in sortie
    assert "bande standard" in sortie and "bande longue" in sortie


def test_les_seuils_viennent_de_la_CONFIGURATION(tmp_path, monkeypatch, capsys):
    """LE test de cette sonde. Les seuils sont LUS, pas recopiés.

    En portant `TELEGRAM_MIN_PREMIUM_EV` à 13, la bande standard n'accepte
    plus ni l'EV à 9 ni celle à 12 : seule la bande longue (cote 5,00 / EV 25)
    survit. Si la sonde portait ses propres constantes, le compte ne bougerait
    pas — et elle mesurerait un canal qui n'existe plus.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    monkeypatch.setenv("TELEGRAM_MIN_PREMIUM_EV", "13")
    sortie = _lancer(_base(tmp_path), monkeypatch, capsys, ("--premium",))
    assert _n(sortie) == 1
    assert "EV ≥ 13 %" in sortie
    assert "lus dans votre .env" in sortie


def test_une_bande_de_COTE_resserree_est_suivie_aussi(
        tmp_path, monkeypatch, capsys):
    """Le seuil d'EV n'est pas le seul lu : la bande de cote aussi."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    monkeypatch.setenv("TELEGRAM_PREMIUM_MAX_ODD", "2.2")
    sortie = _lancer(_base(tmp_path), monkeypatch, capsys, ("--premium",))
    # 2.50 sort de la bande standard ; restent 2.00/9.0 et 5.00/25.0.
    assert _n(sortie) == 2
    assert "cote 1.5–2.2" in sortie


def test_sans_env_telegram_la_sonde_le_DIT(tmp_path, monkeypatch, capsys):
    """Sans `.env` chargé, les seuils sont ceux du code — pas les vôtres. Le
    taire ferait lire un résultat pour un autre."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    sortie = _lancer(_base(tmp_path), monkeypatch, capsys, ("--premium",))
    assert "PAR DEFAUT" in sortie and ".env non chargé" in sortie
