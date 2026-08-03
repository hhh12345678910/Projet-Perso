"""Marchés prématch restés ouverts sur un match commencé.

Le cas vu en production : un match de football commencé depuis 19 minutes,
score 1-1, et Circus proposait toujours son marché « les deux équipes
marquent » en prématch. Le pari était déjà gagné au moment de le prendre.

Ce n'est pas un value bet, c'est une erreur d'exploitation du book. Tout
l'enjeu du détecteur est de ne pas confondre cette erreur avec les trois
situations qui lui ressemblent : une cote live légitime, un match reporté, et
un horaire simplement différent d'un book à l'autre.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.main import find_late_markets, remember_pinnacle_events
from src.matcher import event_key
from src.models import Book, MarketType, OddQuote, Outcome


NOW = datetime(2026, 9, 1, 21, 0, tzinfo=timezone.utc)
KICKOFF = NOW - timedelta(minutes=19)      # commencé il y a 19 minutes
EK = event_key("Union Saint-Gilloise", "Anderlecht", KICKOFF)


def _pin(ek=EK):
    return OddQuote(event_key=ek, book=Book.PINNACLE, market=MarketType.H2H,
                    outcome=Outcome(label="home"), decimal_odd=2.0,
                    fetched_at=NOW, source_event_id="1")


def _soft(book=Book.CIRCUS_BE, ek=EK, live=False, market=MarketType.H2H,
          label="home", line=None):
    return OddQuote(event_key=ek, book=book, market=market,
                    outcome=Outcome(label=label, line=line), decimal_odd=2.4,
                    fetched_at=NOW, source_event_id="2", from_live_feed=live)


def _recent(ek=EK):
    return {ek: 0.0}


def test_the_production_case_is_detected():
    """Pinnacle connaissait le match, ne le price plus (il est en direct), le
    coup d'envoi est dépassé de 19 min, et Circus propose encore du prématch."""
    late = find_late_markets([], [_soft()], "soccer", NOW, recent=_recent())
    assert (EK, Book.CIRCUS_BE) in late
    assert len(late[(EK, Book.CIRCUS_BE)]) == 1


def test_a_match_pinnacle_still_prices_is_not_late():
    """Si Pinnacle price encore l'événement en prématch, c'est qu'il n'a pas
    commencé — quelle que soit l'heure affichée. C'est la disparition qui fait
    foi, pas l'horloge."""
    late = find_late_markets([_pin()], [_soft()], "soccer", NOW, recent=_recent())
    assert late == {}


def test_a_live_quote_is_never_flagged():
    """Betano expose un flux live, fusionné avec son prématch dans la même
    liste. Sans ce filtre, tout match en cours qu'il price passerait pour une
    erreur du book."""
    late = find_late_markets([], [_soft(book=Book.BETANO_BE, live=True)],
                             "soccer", NOW, recent=_recent())
    assert late == {}


def test_an_event_pinnacle_never_priced_is_ignored():
    """Un match que Pinnacle n'a jamais pricé peut simplement avoir un horaire
    faux chez le book. Sans confirmation par la référence, on se tait."""
    late = find_late_markets([], [_soft()], "soccer", NOW, recent={})
    assert late == {}


def test_a_match_too_recent_is_ignored():
    """Sous le seuil, on signalerait surtout des coups d'envoi retardés de
    quelques minutes et des arrondis de programmation."""
    ko = NOW - timedelta(minutes=3)
    ek = event_key("Union Saint-Gilloise", "Anderlecht", ko)
    late = find_late_markets([], [_soft(ek=ek)], "soccer", NOW, recent=_recent(ek))
    assert late == {}


def test_a_match_long_finished_is_ignored():
    """Deux heures après le coup d'envoi, un marché encore ouvert ne relève
    plus de l'oubli mais d'un horaire faux — et le pari ne serait pas payé."""
    ko = NOW - timedelta(minutes=140)
    ek = event_key("Union Saint-Gilloise", "Anderlecht", ko)
    late = find_late_markets([], [_soft(ek=ek)], "soccer", NOW, recent=_recent(ek))
    assert late == {}


def test_the_book_own_kickoff_can_veto():
    """Au tennis le rapprochement tolère trois heures d'écart. Si le book
    annonce un coup d'envoi encore à venir, c'est peut-être lui qui a raison :
    on ne l'accuse pas sur la seule foi de l'heure de Pinnacle."""
    pin_ko = NOW - timedelta(minutes=40)
    book_ko = NOW + timedelta(minutes=30)          # le book dit : pas commencé
    ref = event_key("Alcaraz", "Sinner", pin_ko)
    cand = event_key("Alcaraz", "Sinner", book_ko)
    late = find_late_markets([], [_soft(ek=cand)], "tennis", NOW,
                             recent=_recent(ref))
    assert late == {}


def test_several_markets_of_one_book_are_grouped():
    quotes = [
        _soft(market=MarketType.H2H, label="home"),
        _soft(market=MarketType.H2H, label="away"),
        _soft(market=MarketType.TOTALS, label="over", line=2.5),
    ]
    late = find_late_markets([], quotes, "soccer", NOW, recent=_recent())
    assert len(late[(EK, Book.CIRCUS_BE)]) == 3


def test_two_books_are_reported_separately():
    late = find_late_markets(
        [], [_soft(book=Book.CIRCUS_BE), _soft(book=Book.UNIBET_BE)],
        "soccer", NOW, recent=_recent())
    assert {b for _, b in late} == {Book.CIRCUS_BE, Book.UNIBET_BE}


def test_memory_forgets_old_events():
    """Le dictionnaire ne doit pas grossir indéfiniment : au-delà de six
    heures un match est terminé et n'apprend plus rien."""
    import src.main as m
    m._PINNACLE_RECENT.clear()
    remember_pinnacle_events([_pin()], 0.0)
    assert EK in m._PINNACLE_RECENT
    remember_pinnacle_events([], 7 * 3600.0)
    assert EK not in m._PINNACLE_RECENT
    m._PINNACLE_RECENT.clear()
