from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import main as m
from src.models import Book, MarketType, OddQuote, Outcome

T = datetime(2026, 8, 16, 20, 0, tzinfo=timezone.utc)


def _q(book: Book) -> OddQuote:
    return OddQuote(
        event_key="202608162000::a__vs__b", book=book,
        market=MarketType.H2H, outcome=Outcome(label="home"),
        decimal_odd=2.0, fetched_at=T, source_event_id="x",
    )


@pytest.fixture(autouse=True)
def _clean_state():
    for store in (m._BOOK_SEEN, m._BOOK_ALERTED):
        store.clear()
    m._BOOK_FAILS.clear()
    m._BOOK_DOWN_SINCE.clear()
    yield


class _Spy:
    """Capture les alertes au lieu de les envoyer."""

    def __init__(self) -> None:
        self.sent: list[str] = []


@pytest.fixture
def spy(monkeypatch) -> _Spy:
    s = _Spy()
    monkeypatch.setattr(m, "send_system_alert",
                        lambda cfg, text, **kw: s.sent.append(text))
    return s


def test_a_book_that_never_produced_never_alerts(spy):
    """Un book coupé par BOOKS_DISABLED, ou qui ne couvre pas ce sport, rend
    zéro cote exactement comme un book en panne. Sans cette distinction le
    canal critique se remplirait de books qui n'ont jamais existé ici."""
    for _ in range(50):
        m._book_health("soccer", [], None, now=0.0)
    assert spy.sent == []


def test_alert_needs_both_the_delay_and_the_cycles(spy):
    """La durée seule se déclencherait sur UN cycle anormalement long — pendant
    une purge ils montent à 9-24 minutes (§18.4)."""
    m._book_health("soccer", [_q(Book.UNIBET_BE)], None, now=0.0)

    # Une heure d'absence, mais un seul cycle : pas d'alerte.
    m._book_health("soccer", [], None, now=3600.0)
    assert spy.sent == []


def test_alert_needs_the_delay_not_only_the_cycles(spy):
    """Et le nombre de cycles seul ne veut rien dire non plus : cinq cycles
    valent deux minutes ou deux heures selon le moment."""
    m._book_health("soccer", [_q(Book.UNIBET_BE)], None, now=0.0)
    for i in range(1, 9):
        m._book_health("soccer", [], None, now=float(i))     # 8 cycles, 8 s
    assert spy.sent == []


def test_a_lasting_silence_alerts_once(spy):
    m._book_health("soccer", [_q(Book.UNIBET_BE)], None, now=0.0)
    for i in range(1, 8):
        m._book_health("soccer", [], None, now=i * 300.0)    # 5 min par cycle

    assert len(spy.sent) == 1
    assert "unibet_be" in spy.sent[0]
    assert "muet" in spy.sent[0]

    # Le silence continue : toujours une seule alerte.
    for i in range(8, 20):
        m._book_health("soccer", [], None, now=i * 300.0)
    assert len(spy.sent) == 1


def test_recovery_is_announced_with_the_real_duration(spy):
    m._book_health("soccer", [_q(Book.BETANO_BE)], None, now=0.0)
    for i in range(1, 8):
        m._book_health("soccer", [], None, now=i * 300.0)
    m._book_health("soccer", [_q(Book.BETANO_BE)], None, now=2400.0)

    assert len(spy.sent) == 2
    assert "de retour" in spy.sent[1]
    # L'absence court depuis le PREMIER cycle vide (t=300), pas depuis le
    # dernier cycle sain : 2400 - 300 = 35 min. Compter depuis le cycle sain
    # gonflerait chaque panne d'un cycle.
    assert "35 min" in spy.sent[1]


def test_bridged_books_get_the_tab_hint(spy):
    """La cause de loin la plus fréquente, et la seule corrigible en dix
    secondes. Le message le dit plutôt que de laisser chercher."""
    m._book_health("soccer", [_q(Book.BETANO_BE)], None, now=0.0)
    for i in range(1, 8):
        m._book_health("soccer", [], None, now=i * 300.0)
    assert "onglet" in spy.sent[0]


def test_api_books_get_no_tab_hint(spy):
    m._book_health("soccer", [_q(Book.UNIBET_BE)], None, now=0.0)
    for i in range(1, 8):
        m._book_health("soccer", [], None, now=i * 300.0)
    assert "onglet" not in spy.sent[0]


def test_sports_are_watched_independently(spy):
    """MagicBetting couvre le football et le tennis avec des identifiants
    différents : perdre un sport sans l'autre est un cas réel."""
    m._book_health("soccer", [_q(Book.MAGICBETTING)], None, now=0.0)
    m._book_health("tennis", [_q(Book.MAGICBETTING)], None, now=0.0)

    for i in range(1, 8):
        m._book_health("soccer", [], None, now=i * 300.0)
        m._book_health("tennis", [_q(Book.MAGICBETTING)], None, now=i * 300.0)

    assert len(spy.sent) == 1
    assert "soccer" in spy.sent[0]


def test_a_blip_resets_the_counter(spy):
    """Deux absences courtes séparées par un cycle sain ne doivent pas
    s'additionner en une panne imaginaire."""
    m._book_health("soccer", [_q(Book.UNIBET_BE)], None, now=0.0)
    for i in range(1, 4):
        m._book_health("soccer", [], None, now=i * 300.0)
    m._book_health("soccer", [_q(Book.UNIBET_BE)], None, now=1200.0)
    for i in range(5, 8):
        m._book_health("soccer", [], None, now=i * 300.0)
    assert spy.sent == []
