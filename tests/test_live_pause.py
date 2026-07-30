"""Mettre le live en pause ne doit toucher QUE les value bets.

Surebets et middles comparent les books soft entre eux : ils n'ont besoin
d'aucune référence sharp et restent donc valides une fois le match commencé.
Le stockage des cotes continue lui aussi, sans quoi on perdrait les lignes de
clôture — donc le CLV.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import ScanConfig
from src.main import build_fair_lines, find_value_bets
from src.models import Book, MarketType, OddQuote, Outcome
from src.surebet import find_surebets


def _key(start: datetime) -> str:
    return f"{start.strftime('%Y%m%d%H%M')}::a__vs__b"


def _quotes(event_key: str, soft_home: float = 4.00) -> list[OddQuote]:
    now = datetime.now(timezone.utc)
    out = []
    for label, odd in (("home", 3.46), ("draw", 3.47), ("away", 2.05)):
        out.append(OddQuote(
            event_key=event_key, book=Book.PINNACLE, market=MarketType.H2H,
            outcome=Outcome(label=label, line=None), decimal_odd=odd,
            fetched_at=now, source_event_id="x",
        ))
    for label, odd in (("home", soft_home), ("draw", 3.60), ("away", 2.10)):
        out.append(OddQuote(
            event_key=event_key, book=Book.UNIBET_BE, market=MarketType.H2H,
            outcome=Outcome(label=label, line=None), decimal_odd=odd,
            fetched_at=now, source_event_id="y",
        ))
    return out


def _bets(event_key: str, *, live_enabled: bool):
    quotes = _quotes(event_key)
    cfg = ScanConfig(min_ev_pct=2.0)
    cfg.scan_live_value_bets = live_enabled
    fair = build_fair_lines([q for q in quotes if q.book == Book.PINNACLE], cfg.devig_method)
    soft = [q for q in quotes if q.book != Book.PINNACLE]
    return find_value_bets(soft, fair, cfg)


def test_prematch_is_untouched_by_the_pause():
    ek = _key(datetime.now(timezone.utc) + timedelta(hours=6))
    assert _bets(ek, live_enabled=False)


def test_live_is_dropped_when_paused():
    ek = _key(datetime.now(timezone.utc) - timedelta(minutes=30))
    assert _bets(ek, live_enabled=False) == []


def test_live_comes_back_when_re_enabled():
    """La pause doit être réversible sans toucher au code."""
    ek = _key(datetime.now(timezone.utc) - timedelta(minutes=30))
    assert _bets(ek, live_enabled=True)


def test_unparseable_event_key_is_kept():
    # Un défaut de format ne doit pas faire disparaître un pari en silence.
    quotes = _quotes("pas-une-cle-valide")
    cfg = ScanConfig(min_ev_pct=2.0)
    cfg.scan_live_value_bets = False
    fair = build_fair_lines([q for q in quotes if q.book == Book.PINNACLE], cfg.devig_method)
    soft = [q for q in quotes if q.book != Book.PINNACLE]
    assert find_value_bets(soft, fair, cfg)


def test_surebets_still_found_on_a_live_event():
    """Le cœur de la demande : la pause ne doit pas coûter un seul surebet."""
    started = _key(datetime.now(timezone.utc) - timedelta(minutes=30))
    now = datetime.now(timezone.utc)
    # Deux books soft dont les prix se croisent : arbitrage réel, sans Pinnacle.
    quotes = []
    for book, home, away in ((Book.UNIBET_BE, 2.40, 1.60), (Book.NAPOLEON_BE, 1.60, 2.40)):
        for label, odd in (("home", home), ("away", away)):
            quotes.append(OddQuote(
                event_key=started, book=book, market=MarketType.H2H,
                outcome=Outcome(label=label, line=None), decimal_odd=odd,
                fetched_at=now, source_event_id=book.value,
            ))

    assert find_surebets(quotes), "les surebets live ne doivent pas être affectés"


def test_the_flag_reads_the_environment(monkeypatch):
    monkeypatch.setenv("VALUEBET_SCAN_LIVE", "1")
    assert ScanConfig().scan_live_value_bets is True
    monkeypatch.setenv("VALUEBET_SCAN_LIVE", "0")
    assert ScanConfig().scan_live_value_bets is False
    monkeypatch.delenv("VALUEBET_SCAN_LIVE", raising=False)
    # Par défaut le live est en pause.
    assert ScanConfig().scan_live_value_bets is False
