"""Une ligne de référence à la marge aberrante ne doit pas devenir « juste ».

Le cas mesuré le 16/08, Hodd — Strommen : Smarkets cotait over 6,5 à 1,98 et
under 6,5 à 1,07 — 144 % de marge — alors que sa propre ligne 5,5 donnait over
à 10,66. Une cote « over » qui redescend en montant la ligne n'est pas un prix
mais une offre isolée que personne n'a prise, ce qu'un carnet d'ordres affiche
quand même. Pinnacle s'arrêtant à 4,5, le repli secondaire s'est déclenché
précisément là où l'exchange est illiquide.

Résultat : une « juste » à 3,53 sur un événement à 2 %, et +260 % d'EV sur
cinq matchs. Le book, lui, avait raison — son échelle était monotone.

Le contrôle DOIT précéder le devig : celui-ci normalise à 100 % quoi qu'on lui
donne, donc nourri d'une ligne à 144 % il rend une probabilité d'apparence
normale et l'aberration devient indétectable en aval (§11).
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.main import _devig_group, _overround_ok, build_fair_lines
from src.models import Book, MarketType, OddQuote, Outcome

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def q(book: Book, label: str, line: float, odd: float) -> OddQuote:
    return OddQuote(
        event_key="202608161500::hodd__vs__strommen", book=book,
        market=MarketType.TOTALS, outcome=Outcome(label=label, line=line),
        decimal_odd=odd, fetched_at=NOW, source_event_id="1",
    )


def test_the_smarkets_line_that_caused_it_is_rejected():
    groupe = [q(Book.SMARKETS, "over", 6.5, 1.98),
              q(Book.SMARKETS, "under", 6.5, 1.07)]
    assert _overround_ok(groupe) is False
    assert _devig_group(groupe, "shin") is None


def test_a_normal_sharp_line_passes():
    """Smarkets sur sa ligne 5,5 du même match : cohérente, donc gardée."""
    groupe = [q(Book.SMARKETS, "over", 5.5, 10.66),
              q(Book.SMARKETS, "under", 5.5, 1.11)]
    assert _overround_ok(groupe) is True
    assert _devig_group(groupe, "shin") is not None


def test_pinnacle_1x2_margin_passes():
    """5 à 7 % sur un 1X2 est le régime normal : ne rien couper de sain."""
    groupe = [q(Book.PINNACLE, "home", None, 2.10),
              q(Book.PINNACLE, "draw", None, 3.40),
              q(Book.PINNACLE, "away", None, 3.90)]
    assert _overround_ok(groupe) is True


def test_a_sub_hundred_book_is_left_alone():
    """Sous 100 %, c'est un surebet interne à la référence.

    Anormal aussi, mais c'est le signal que cherche find_surebets : le couper
    ici le ferait disparaître ailleurs."""
    groupe = [q(Book.SMARKETS, "over", 2.5, 2.10),
              q(Book.SMARKETS, "under", 2.5, 2.10)]
    assert _overround_ok(groupe) is True


def test_no_fair_line_is_built_from_the_broken_ladder():
    """Bout en bout : aucune juste ne doit sortir de la ligne 6,5.

    C'est ce qui empêche la détection à +260 % — pas un plafond d'EV, qui
    aurait masqué le défaut au lieu de le corriger."""
    fair = build_fair_lines([], "shin", secondary_quotes=[
        q(Book.SMARKETS, "over", 6.5, 1.98),
        q(Book.SMARKETS, "under", 6.5, 1.07),
        q(Book.SMARKETS, "over", 2.5, 1.64),
        q(Book.SMARKETS, "under", 2.5, 2.51),
    ])
    lignes = {k[2] for k in fair}
    assert 6.5 not in lignes        # écartée
    assert 2.5 in lignes            # la ligne saine reste
