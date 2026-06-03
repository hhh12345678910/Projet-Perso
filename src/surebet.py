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


def _required_labels(market: MarketType, present: set[str]) -> tuple[str, ...] | None:
    """Decide which outcome labels constitute a complete arbitrage."""
    if market == MarketType.H2H:
        if {"home", "draw", "away"}.issubset(present):
            return ("home", "draw", "away")
        if {"home", "away"}.issubset(present) and "draw" not in present:
            return ("home", "away")
        return None
    if market == MarketType.TOTALS:
        if {"over", "under"}.issubset(present):
            return ("over", "under")
    # Handicap is intentionally skipped: line semantics vary across books
    # (some encode signed lines, some store both sides at the same |line|),
    # which produces phantom arbs we can't disambiguate without per-book
    # convention tracking.
    return None


def find_surebets(quotes: Iterable[OddQuote]) -> list[Surebet]:
    """Group quotes by (event_key, market, line) and report every group where
    the best-of-different-books per outcome sums to less than 1.

    Quotes are assumed to already share a canonical event_key — call
    `reconcile_event_keys` upstream when mixing books with different naming."""
    # (event_key, market, line) -> label -> [(odd, book)]
    grouped: dict[tuple[str, MarketType, float | None], dict[str, list[tuple[float, Book]]]] = (
        defaultdict(lambda: defaultdict(list))
    )
    for q in quotes:
        grouped[(q.event_key, q.market, q.outcome.line)][q.outcome.label].append(
            (q.decimal_odd, q.book)
        )

    out: list[Surebet] = []
    for (event_key, market, line), label_to_offers in grouped.items():
        labels = _required_labels(market, set(label_to_offers))
        if labels is None:
            continue
        # Best odd per outcome, plus which book offers it.
        legs: dict[str, tuple[float, Book]] = {
            label: max(label_to_offers[label]) for label in labels
        }
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
