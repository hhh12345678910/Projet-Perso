from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from .models import Book, FairLine, MarketType, OddQuote


def _is_half_line(line: float | None) -> bool:
    """Middles only make sense on half-integer total lines (2.5, 3.5, …): a
    whole-number line (3.0) can push, and quarter lines (2.25) are split bets —
    neither is modelled by the balanced-stake EV maths below."""
    if line is None:
        return False
    doubled = line * 2
    return abs(doubled - round(doubled)) < 1e-9 and round(doubled) % 2 == 1


@dataclass
class Middle:
    """A totals middle: back Over `low_line` at one book and Under `high_line`
    at another (high_line > low_line). The 'middle' hits when the final total
    lands strictly between the two lines → both legs win. Outside the gap one
    leg wins and the other loses (near break-even when stakes are balanced)."""

    event_key: str
    low_line: float                    # Over this line
    high_line: float                   # Under this line
    over_leg: tuple[float, Book]       # (odd, book) for Over low_line
    under_leg: tuple[float, Book]      # (odd, book) for Under high_line
    mid_prob: float                    # P(total strictly between) from Pinnacle
    ev_pct: float
    inverse_sum: float                 # 1/over_odd + 1/under_odd
    market: MarketType = MarketType.TOTALS

    @property
    def gap_values(self) -> list[int]:
        """Integer totals that land inside the middle (both legs win)."""
        lo = math.floor(self.low_line) + 1
        hi = math.ceil(self.high_line) - 1
        return list(range(lo, hi + 1))

    def stakes(self, bankroll: float) -> dict[str, float]:
        """Balanced stakes so the two 'miss' outcomes return the same amount;
        the middle gap is then pure upside on top. Proportional to the implied
        probabilities of each leg."""
        o_odd, _ = self.over_leg
        u_odd, _ = self.under_leg
        return {
            "over": bankroll * (1.0 / o_odd) / self.inverse_sum,
            "under": bankroll * (1.0 / u_odd) / self.inverse_sum,
        }


def find_middles(
    soft_quotes: Iterable[OddQuote],
    fair_lines: dict[tuple[str, MarketType, float | None], FairLine],
    *,
    min_ev_pct: float = 2.0,
    max_gap: float = 3.0,
) -> list[Middle]:
    """Detect +EV totals middles across soft books, priced against Pinnacle.

    For every event, take the best Over odd at each half-line and the best Under
    odd at each higher half-line (from a *different* book), then use Pinnacle's
    devigged totals ladder to get the fair probability the total lands in the
    gap:
        P_mid = P(over low) − P(over high)          [both read from fair_lines]
    With balanced stakes the EV per unit staked is:
        EV = (1 + P_mid) / inverse_sum − 1
    where inverse_sum = 1/over_odd + 1/under_odd. Emits middles clearing
    min_ev_pct, requiring two distinct books and both lines priced by Pinnacle
    (so the gap probability is a genuine sharp estimate, not a guess)."""
    over_best: dict[tuple[str, float], tuple[float, Book]] = {}
    under_best: dict[tuple[str, float], tuple[float, Book]] = {}
    events: set[str] = set()
    for q in soft_quotes:
        if q.market != MarketType.TOTALS:
            continue
        line = q.outcome.line
        if not _is_half_line(line):
            continue
        key = (q.event_key, float(line))
        if q.outcome.label == "over":
            cur = over_best.get(key)
            if cur is None or q.decimal_odd > cur[0]:
                over_best[key] = (q.decimal_odd, q.book)
                events.add(q.event_key)
        elif q.outcome.label == "under":
            cur = under_best.get(key)
            if cur is None or q.decimal_odd > cur[0]:
                under_best[key] = (q.decimal_odd, q.book)
                events.add(q.event_key)

    def _pin_over(event_key: str, line: float) -> float | None:
        fl = fair_lines.get((event_key, MarketType.TOTALS, line))
        return fl.outcomes.get("over") if fl is not None else None

    out: list[Middle] = []
    for event_key in events:
        over_lines = sorted(l for (ek, l) in over_best if ek == event_key)
        under_lines = sorted(l for (ek, l) in under_best if ek == event_key)
        for lo in over_lines:
            for hi in under_lines:
                if hi <= lo or (hi - lo) > max_gap:
                    continue
                o_odd, o_book = over_best[(event_key, lo)]
                u_odd, u_book = under_best[(event_key, hi)]
                if o_book == u_book:
                    continue  # a real middle needs two different books
                p_over_lo = _pin_over(event_key, lo)
                p_over_hi = _pin_over(event_key, hi)
                if p_over_lo is None or p_over_hi is None:
                    continue
                mid_prob = p_over_lo - p_over_hi
                if mid_prob <= 0:
                    continue
                inverse_sum = 1.0 / o_odd + 1.0 / u_odd
                ev_pct = ((1.0 + mid_prob) / inverse_sum - 1.0) * 100.0
                if ev_pct < min_ev_pct:
                    continue
                out.append(
                    Middle(
                        event_key=event_key,
                        low_line=lo,
                        high_line=hi,
                        over_leg=(o_odd, o_book),
                        under_leg=(u_odd, u_book),
                        mid_prob=mid_prob,
                        ev_pct=ev_pct,
                        inverse_sum=inverse_sum,
                    )
                )
    out.sort(key=lambda m: m.ev_pct, reverse=True)
    return out
