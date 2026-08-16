"""Smarkets n'est plus une référence de repli — décision prise sur mesure.

Sur 24 paris valorisés contre le repli Smarkets et jugés au consensus dévigé
des softbooks — règle indépendante de Smarkets, c'est tout son intérêt — la CLV
ressort à −20,6 % de moyenne et **0 % de positifs**. Zéro sur vingt-quatre.

Le mécanisme se lit sur un cas : radualbot — viktorfrydrych, juste Smarkets à
3,71 quand six books s'accordent sur 10,22. Une offre orpheline dans un carnet
vide — et le repli va la chercher précisément là où l'exchange est le plus
creux, puisqu'il ne se déclenche que sur ce que Pinnacle ignore.
"""
from __future__ import annotations

from datetime import datetime, timezone

import src.main as m
from src.models import Book, MarketType, OddQuote, Outcome

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def q(book, label, odd):
    return OddQuote(event_key="202608161500::a__vs__b", book=book,
                    market=MarketType.H2H, outcome=Outcome(label=label),
                    decimal_odd=odd, fetched_at=NOW, source_event_id="1")


def test_the_fallback_is_off_by_default():
    """Il faut poser SMARKETS_AS_REFERENCE=1 pour le rallumer, jamais l'inverse.

    Un réglage dangereux doit demander un geste explicite ; l'oubli doit
    retomber du côté sûr."""
    assert m._SMARKETS_AS_REFERENCE is False


def test_without_a_secondary_no_fallback_line_exists():
    """Sans source secondaire, aucune juste n'est fabriquée sur un marché que
    Pinnacle ne price pas — donc aucun value bet ne peut en naître."""
    fair = m.build_fair_lines([], "shin", secondary_quotes=None)
    assert fair == {}


def test_smarkets_is_still_collected_as_an_observer():
    """Rien n'est perdu : la collecte reste, seule la fabrication de lignes
    justes lui est retirée. C'est ce qui permettra de refaire la mesure si sa
    liquidité s'améliore."""
    assert m._SMARKETS_ENABLED is True
    assert Book.SMARKETS in m.SHARP_BOOKS      # jamais un book où l'on parie
