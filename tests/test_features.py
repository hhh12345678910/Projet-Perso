"""Collecte des features permanentes.

Ces variables existent en mémoire à un instant précis du cycle et étaient
jetées ensuite. Aucune ne se reconstitue après coup : l'overround de la
référence et l'âge de sa ligne disparaissent avec la purge des cotes, la ligue
n'était stockée nulle part, et le score d'appariement n'était même pas
conservé. Une régression ici ne casse rien de visible — elle fait juste
collecter du vide, et on ne s'en aperçoit qu'en voulant analyser.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.main import build_bet_features, remap_to_reference
from src.matcher import event_key
from src.models import Book, FairLine, MarketType, OddQuote, Outcome, ValueBet
from src.storage import Storage


KO = datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc)
EK = event_key("Arsenal", "Chelsea", KO)
NOW = KO - timedelta(hours=6)


def _pin(label, odd, league="England - Premier League", fetched=None):
    return OddQuote(event_key=EK, book=Book.PINNACLE, market=MarketType.H2H,
                    outcome=Outcome(label=label), decimal_odd=odd,
                    fetched_at=fetched or NOW, source_event_id="1", league=league)


def _soft(book, label, odd, score=None, bek=None):
    return OddQuote(event_key=EK, book=book, market=MarketType.H2H,
                    outcome=Outcome(label=label), decimal_odd=odd,
                    fetched_at=NOW, source_event_id="2",
                    match_score=score, book_event_key=bek)


def _bet(book, odd, **kw):
    return ValueBet(event_key=EK, book=book, market=MarketType.H2H,
                    outcome=Outcome(label="home"), odd_taken=odd, fair_prob=0.5,
                    fair_odd=2.0, ev_pct=10.0, kelly_stake_pct=1.0,
                    detected_at=NOW, reference_book=Book.PINNACLE, **kw)


def _rows(bets, pin=None, soft=None, now=None):
    pin = pin if pin is not None else [_pin("home", 2.1), _pin("draw", 3.5), _pin("away", 3.8)]
    soft = soft if soft is not None else [_soft(Book.UNIBET_BE, "home", 2.3)]
    fair = {(EK, MarketType.H2H, None): FairLine(
        event_key=EK, market=MarketType.H2H,
        outcomes={"home": 0.47, "draw": 0.28, "away": 0.25})}
    return build_bet_features(bets, list(range(1, len(bets) + 1)), pin, soft,
                              fair, "soccer", now or NOW)


def test_league_and_category_come_from_the_reference():
    """Le book soft ne nomme pas toujours la compétition ; Pinnacle la nomme
    pour tous les événements de la référence. C'est donc de lui qu'elle vient."""
    r = _rows([_bet(Book.UNIBET_BE, 2.3)])[0]
    assert r[4] == "England - Premier League"
    assert r[5] == "top5"


def test_bet_league_wins_over_the_reference_when_present():
    r = _rows([_bet(Book.UNIBET_BE, 2.3, league="Sweden - Allsvenskan")])[0]
    assert r[4] == "Sweden - Allsvenskan"
    assert r[5] == "scandinavie"


def test_reference_overround_and_outcome_count():
    """Un overround élevé signale un marché sur lequel Pinnacle lui-même
    n'est pas sûr — une des causes soupçonnées de faux positifs."""
    r = _rows([_bet(Book.UNIBET_BE, 2.3)])[0]
    expected = 1 / 2.1 + 1 / 3.5 + 1 / 3.8 - 1
    assert abs(r[16] - expected) < 1e-9
    assert r[17] == 3


def test_reference_age_is_measured_from_the_quote(monkeypatch):
    """Une ligne de référence vieille de deux minutes a pu bouger : l'EV est
    alors calculée contre un prix périmé. Sans cet âge, impossible de le voir."""
    old = NOW - timedelta(seconds=90)
    pin = [_pin("home", 2.1, fetched=old), _pin("draw", 3.5, fetched=old),
           _pin("away", 3.8, fetched=old)]
    r = _rows([_bet(Book.UNIBET_BE, 2.3)], pin=pin)[0]
    assert abs(r[18] - 90.0) < 1.0


def test_number_of_books_pricing_the_market():
    """Un marché que personne d'autre ne price est le premier suspect quand
    une cote paraît trop belle."""
    soft = [_soft(Book.UNIBET_BE, "home", 2.3), _soft(Book.LADBROKES_BE, "home", 2.2),
            _soft(Book.NAPOLEON_BE, "away", 3.9)]
    r = _rows([_bet(Book.UNIBET_BE, 2.3)], soft=soft)[0]
    assert r[19] == 3


def test_delay_and_match_score_are_carried():
    r = _rows([_bet(Book.UNIBET_BE, 2.3, match_score=91.5)])[0]
    assert abs(r[14] - 6.0) < 1e-6      # 6 h avant le coup d'envoi
    assert r[20] == 91.5


def test_time_shift_between_the_two_sources():
    """Au tennis les deux sources divergent de plusieurs heures. Un écart
    important veut peut-être dire qu'elles ne parlent pas du même match."""
    bek = event_key("Arsenal", "Chelsea", KO - timedelta(minutes=45))
    r = _rows([_bet(Book.UNIBET_BE, 2.3, book_event_key=bek)])[0]
    assert abs(r[21] - 45.0) < 1e-6


def test_match_score_survives_the_remapping():
    """Le score était calculé par le matcher puis jeté. Ce test vérifie qu'il
    traverse bien le réalignement, y compris quand la clé change."""
    ref = event_key("Bayern Munich", "Manchester United", KO)
    cand = event_key("Bayern Munchen", "Manchester Utd", KO)
    q = OddQuote(event_key=cand, book=Book.UNIBET_BE, market=MarketType.H2H,
                 outcome=Outcome(label="home"), decimal_odd=2.0,
                 fetched_at=NOW, source_event_id="9")
    out = remap_to_reference([q], [ref], "soccer")
    assert out, "le rapprochement devait aboutir"
    assert out[0].match_score is not None
    assert 85.0 <= out[0].match_score <= 100.0


def test_exact_key_match_scores_100():
    q = OddQuote(event_key=EK, book=Book.UNIBET_BE, market=MarketType.H2H,
                 outcome=Outcome(label="home"), decimal_odd=2.0,
                 fetched_at=NOW, source_event_id="9")
    out = remap_to_reference([q], [EK], "soccer")
    assert out[0].match_score == 100.0


def test_features_round_trip_through_storage(tmp_path):
    db = str(tmp_path / "v.db")
    s = Storage(db)
    rows = _rows([_bet(Book.UNIBET_BE, 2.3)])
    assert s.insert_bet_features(rows) == 1
    # Rejouer le même cycle ne duplique pas.
    assert s.insert_bet_features(rows) == 1
    import sqlite3
    c = sqlite3.connect(db)
    got = c.execute("SELECT COUNT(*), league_category FROM bet_features").fetchone()
    assert got[0] == 1 and got[1] == "top5"


def test_upsert_events_fills_a_league_left_empty(tmp_path):
    """Un INSERT OR IGNORE laissait vide à jamais un événement vu une première
    fois sans ligue. Une ligue jamais remplie ne se rattrape pas."""
    import sqlite3
    db = str(tmp_path / "v.db")
    s = Storage(db)
    s.upsert_events([(EK, "soccer", "", "arsenal", "chelsea", KO.isoformat())])
    s.upsert_events([(EK, "soccer", "England - Premier League", "arsenal",
                      "chelsea", KO.isoformat())])
    c = sqlite3.connect(db)
    assert c.execute("SELECT league FROM events").fetchone()[0] == "England - Premier League"


def test_upsert_events_never_overwrites_a_known_league(tmp_path):
    import sqlite3
    db = str(tmp_path / "v.db")
    s = Storage(db)
    s.upsert_events([(EK, "soccer", "England - Premier League", "a", "b", KO.isoformat())])
    s.upsert_events([(EK, "soccer", "Autre Chose", "a", "b", KO.isoformat())])
    c = sqlite3.connect(db)
    assert c.execute("SELECT league FROM events").fetchone()[0] == "England - Premier League"
