from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .models import Book, MarketType, OddQuote


# Surebets that look "too good" are almost always parsing/matching bugs in
# practice (event mis-match, home/away swap, line-semantics mismatch). Cap the
# margin we report so the noise doesn't drown out the realistic 0.5-3% range.
SUSPICIOUS_MARGIN_THRESHOLD = 0.15


@dataclass
class Surebet:
    event_key: str
    market: MarketType
    line: float | None
    # Per outcome label: (best decimal odd, book offering it).
    legs: dict[str, tuple[float, Book]] = field(default_factory=dict)
    margin: float = 0.0   # 1 - Σ(1/odd_i); > 0 means arbitrage exists
    suspicious: bool = False

    @property
    def roi(self) -> float:
        """Return as a fraction of the total stake."""
        denom = 1.0 - self.margin
        return self.margin / denom if denom > 0 else 0.0

    def stakes(self, bankroll: float) -> dict[str, float]:
        """Optimal per-leg stake to lock in `bankroll * (1 + roi)` on any
        outcome (proportional to the implied probabilities)."""
        denom = sum(1.0 / odd for odd, _ in self.legs.values())
        return {
            label: bankroll * (1.0 / odd) / denom
            for label, (odd, _) in self.legs.items()
        }


def _best_per_label(
    book_offers: dict[Book, dict[str, float]],
    eligible_books: set[Book],
    labels: tuple[str, ...],
) -> dict[str, tuple[float, Book]] | None:
    """For each label, the best (odd, book) among `eligible_books` that offer it.
    Returns None if any label has no offer among the eligible books."""
    legs: dict[str, tuple[float, Book]] = {}
    for label in labels:
        best: tuple[float, Book] | None = None
        for book in eligible_books:
            odd = book_offers[book].get(label)
            if odd is None:
                continue
            if best is None or odd > best[0]:
                best = (odd, book)
        if best is None:
            return None
        legs[label] = best
    return legs


def find_surebets(quotes: Iterable[OddQuote]) -> list[Surebet]:
    """Group quotes by (event_key, market, line) and report every group where
    the best-of-different-books per outcome sums to less than 1.

    For H2H, books are split into 3-way and 2-way markets before computing an
    arb. This prevents the classic hockey/soccer phantom where a moneyline
    book's home/away (winner incl. overtime) is mixed with a regulation-time
    3-way draw from another book — that mix fabricates a non-existent arbitrage.
    A book is treated as a *moneyline 2-way* book only when it offers both home
    and away but no draw; a book that offers a draw (or only a partial set) stays
    eligible for the 3-way arb so genuine 3-way arbs across books still surface.

    Quotes are assumed to already share a canonical event_key — call
    `reconcile_event_keys` upstream when mixing books with different naming."""
    # (event_key, market, line) -> book -> {label: best odd from that book}
    grouped: dict[tuple[str, MarketType, float | None], dict[Book, dict[str, float]]] = (
        defaultdict(lambda: defaultdict(dict))
    )
    for q in quotes:
        per_book = grouped[(q.event_key, q.market, q.outcome.line)][q.book]
        prev = per_book.get(q.outcome.label)
        if prev is None or q.decimal_odd > prev:
            per_book[q.outcome.label] = q.decimal_odd

    out: list[Surebet] = []
    for (event_key, market, line), book_offers in grouped.items():
        # Build the candidate leg-sets to test for this group. For H2H we may
        # produce both a 3-way and a 2-way candidate from disjoint book pools.
        candidate_legs: list[dict[str, tuple[float, Book]]] = []

        if market == MarketType.H2H:
            # A book is a confirmed moneyline (2-way) book only if it prices
            # both sides but not a draw. Everything else (offers a draw, or only
            # a partial set) is eligible for the 3-way arb.
            moneyline_books = {
                b for b, o in book_offers.items()
                if "home" in o and "away" in o and "draw" not in o
            }
            three_way_eligible = set(book_offers) - moneyline_books

            legs_3way = _best_per_label(book_offers, three_way_eligible, ("home", "draw", "away"))
            if legs_3way is not None:
                candidate_legs.append(legs_3way)

            if moneyline_books:
                legs_2way = _best_per_label(book_offers, moneyline_books, ("home", "away"))
                if legs_2way is not None:
                    candidate_legs.append(legs_2way)

        elif market == MarketType.TOTALS:
            legs_ou = _best_per_label(book_offers, set(book_offers), ("over", "under"))
            if legs_ou is not None:
                candidate_legs.append(legs_ou)
        # Handicap intentionally skipped: line semantics vary across books
        # (signed lines vs both sides at the same |line|), which produces
        # phantom arbs we can't disambiguate without per-book convention tracking.

        for legs in candidate_legs:
            # A real arbitrage needs a different book per leg — same-book "arbs"
            # are just two markets a single book lets you bet on, not arbitrage.
            if len({book for _, book in legs.values()}) < len(legs):
                continue
            inverse_sum = sum(1.0 / odd for odd, _ in legs.values())
            if inverse_sum >= 1.0:
                continue
            margin = 1.0 - inverse_sum
            out.append(
                Surebet(
                    event_key=event_key,
                    market=market,
                    line=line,
                    legs=legs,
                    margin=margin,
                    suspicious=margin > SUSPICIOUS_MARGIN_THRESHOLD,
                )
            )
    out.sort(key=lambda s: s.margin, reverse=True)
    return out
