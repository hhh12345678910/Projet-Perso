"""La sonde de couverture des ligues (§21.9 pt 1).

Ce qui est vérifié ici n'est pas le formatage mais le VERDICT : sur une
population dont on connaît d'avance la part de grands championnats, la sonde
doit annoncer la bonne. Se tromper là enverrait le projet payer un pont
navigateur pour rien — ou, pire, choisir une API à catalogue qui laisserait la
moitié des détections sans résultat, donc le §20.4 sans réponse.

Le second point testé est le silence : une détection sans ligne `events` ne
doit JAMAIS disparaître du tableau sans être annoncée. C'est le trou du §19.7,
et il ressemble en tout point à « cette ligue n'existe pas ».
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from scripts import scores_coverage
from src.models import Book, MarketType, Outcome, ValueBet
from src.storage import Storage

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _base(tmp_path, lots, *, sans_event=0):
    """Construit une base de détections. `lots` = [(ligue, sport, n, ev), …].

    `sans_event` ajoute des value_bets dont l'événement n'est jamais inséré :
    c'est le trou du §19.7, et la sonde doit le compter à part.
    """
    db = str(tmp_path / "t.db")
    st = Storage(db)
    n = 0
    for ligue, sport, combien, ev in lots:
        for _ in range(combien):
            n += 1
            ek = f"2026080112{n:04d}::a__vs__b"
            st.upsert_event(ek, sport, ligue, "A", "B", T0 + timedelta(hours=3))
            st.insert_value_bet(ValueBet(
                event_key=ek, book=Book.UNIBET_BE, market=MarketType.H2H,
                outcome=Outcome(label="home"), odd_taken=2.0,
                fair_prob=0.5, fair_odd=1.9, ev_pct=ev, kelly_stake_pct=1.0,
                detected_at=T0 + timedelta(minutes=n),
            ))
    for _ in range(sans_event):
        n += 1
        # Aucun upsert_event : la détection existe, son événement non.
        st.insert_value_bet(ValueBet(
            event_key=f"2026080112{n:04d}::orphelin", book=Book.UNIBET_BE,
            market=MarketType.H2H, outcome=Outcome(label="home"), odd_taken=2.0,
            fair_prob=0.5, fair_odd=1.9, ev_pct=10.0, kelly_stake_pct=1.0,
            detected_at=T0 + timedelta(minutes=n),
        ))
    return db


def _sortie(capsys, db, *args):
    argv = sys.argv
    sys.argv = ["scores_coverage", "--db", db, *args]
    try:
        scores_coverage.main()
    finally:
        sys.argv = argv
    return capsys.readouterr().out


def test_part_du_top5_est_le_verdict(tmp_path, capsys):
    """30 détections en top 5 sur 100 doivent s'annoncer 30,0 %, pas « la
    plupart ». C'est ce chiffre qui décide entre une API à catalogue et un
    cinquième pont navigateur."""
    db = _base(tmp_path, [
        ("England - Premier League", "soccer", 30, 10.0),
        ("Estonia - Esiliiga B", "soccer", 40, 10.0),
        ("Peru - Liga 2", "soccer", 30, 10.0),
    ])
    out = _sortie(capsys, db)

    assert "Détections football : 100 sur 3 ligues distinctes" in out
    assert "30.0 %" in out
    assert "70 autres" in out


def test_le_tennis_ne_compte_pas(tmp_path, capsys):
    """La source cherchée est une source de FOOTBALL. Compter le tennis
    gonflerait la queue longue avec des tournois qu'aucune API de football
    n'a vocation à couvrir."""
    db = _base(tmp_path, [
        ("England - Premier League", "soccer", 10, 10.0),
        ("ATP Cincinnati", "tennis", 90, 10.0),
    ])
    out = _sortie(capsys, db)

    assert "Détections football : 10 sur 1 ligues distinctes" in out
    assert "ATP Cincinnati" not in out


def test_les_detections_orphelines_sont_annoncees(tmp_path, capsys):
    """Le piège §19.7 : sans ligne `events`, une détection n'a pas de ligue.
    L'ignorer en silence donnerait un tableau propre et faux."""
    db = _base(tmp_path, [("England - Premier League", "soccer", 10, 10.0)],
               sans_event=7)
    out = _sortie(capsys, db)

    assert "7 détections sans ligne" in out
    # Et elles ne sont pas comptées comme du football : le total reste 10.
    assert "Détections football : 10 " in out


def test_min_ev_restreint_la_population(tmp_path, capsys):
    """Le flux réellement détecté commence à 5 % d'EV (§21.12). Juger la
    couverture sur des détections que le daemon n'émet pas décrirait un flux
    que personne ne reçoit."""
    db = _base(tmp_path, [
        ("England - Premier League", "soccer", 20, 3.0),
        ("Estonia - Esiliiga B", "soccer", 10, 8.0),
    ])
    out = _sortie(capsys, db, "--min-ev", "5")

    assert "Détections football : 10 sur 1 ligues distinctes" in out
    # La Premier League est sous le seuil : le top 5 tombe donc à zéro.
    assert "au mieux 0.0 %" in out


def test_concentration_par_paliers(tmp_path, capsys):
    """Une ligue qui porte 90 % du volume et neuf qui se partagent le reste
    appellent une autre décision que dix ligues égales. C'est la FORME de la
    queue qu'on lit, pas le nombre de ligues."""
    lots = [("England - Premier League", "soccer", 90, 10.0)]
    lots += [(f"Nulle Part - Division {i}", "soccer", 1, 10.0) for i in range(10)]
    db = _base(tmp_path, lots)
    out = _sortie(capsys, db)

    assert "Détections football : 100 sur 11 ligues distinctes" in out
    # Une seule ligue suffit pour 50 % ET pour 80 % : elle en porte 90.
    assert "50 % des détections  →     1 ligues" in out
    assert "80 % des détections  →     1 ligues" in out
    # Les dix ligues à une détection sont de la queue longue.
    assert "10 ligues n'apparaissent qu'une ou deux fois" in out


def _base_ligue_vide(tmp_path, jours_avant):
    """Des détections football dont `events.league` est vide, datées.

    Les dates sont RELATIVES à aujourd'hui : figer 2026-08 ferait passer ces
    tests du vert au rouge avec le calendrier, sans qu'aucun code ne change.
    """
    db = str(tmp_path / "t.db")
    st = Storage(db)
    detected = datetime.now(timezone.utc) - timedelta(days=jours_avant)
    st.upsert_event("ok::a__vs__b", "soccer", "England - Premier League",
                    "A", "B", detected + timedelta(hours=3))
    st.insert_value_bet(ValueBet(
        event_key="ok::a__vs__b", book=Book.UNIBET_BE, market=MarketType.H2H,
        outcome=Outcome(label="home"), odd_taken=2.0, fair_prob=0.5,
        fair_odd=1.9, ev_pct=10.0, kelly_stake_pct=1.0, detected_at=detected))
    for i in range(5):
        ek = f"vide{i}::a__vs__b"
        st.upsert_event(ek, "soccer", "", "A", "B", detected + timedelta(hours=3))
        st.insert_value_bet(ValueBet(
            event_key=ek, book=Book.UNIBET_BE, market=MarketType.H2H,
            outcome=Outcome(label="home"), odd_taken=2.0, fair_prob=0.5,
            fair_odd=1.9, ev_pct=10.0, kelly_stake_pct=1.0,
            detected_at=detected + timedelta(minutes=i)))
    return db


def test_un_trou_de_ligue_ancien_est_declare_historique(tmp_path, capsys):
    """Le cas réel du 21/08 : 12 156 détections sans ligue, toutes arrêtées au
    01/08 — des lignes d'avant la capture de la ligue. La mesure reste valable
    et la sonde doit le DIRE, sinon on suspecte le tableau sans raison."""
    out = _sortie(capsys, _base_ligue_vide(tmp_path, jours_avant=45))

    assert "5 dont la ligue est vide" in out
    assert "HISTORIQUE" in out
    assert "ACTIF" not in out


def test_un_trou_de_ligue_actuel_est_declare_actif(tmp_path, capsys):
    """Le cas opposé, qui invalide la mesure : si la ligue cesse d'être
    capturée aujourd'hui, le tableau ne décrit qu'une partie du flux. Les deux
    cas affichaient le même avertissement, et il fallait une requête SQL à la
    main pour les séparer."""
    out = _sortie(capsys, _base_ligue_vide(tmp_path, jours_avant=0))

    assert "5 dont la ligue est vide" in out
    assert "ACTIF" in out
    assert "HISTORIQUE" not in out


def test_base_sans_football(tmp_path, capsys):
    """Rien à dire doit se dire. Un tableau vide et un tableau absent se
    ressemblent trop pour qu'on laisse le doute."""
    db = _base(tmp_path, [("ATP Cincinnati", "tennis", 5, 10.0)])
    out = _sortie(capsys, db)

    assert "Aucune détection football" in out
