from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .matcher import parse_event_key
from .models import OddQuote


def clv_pct(odd_taken: float, closing_pinnacle_odd: float) -> float:
    """Beating the sharp closing line is the single most reliable indicator of
    long-run profitability — a CLV of +2 % means the price you took is 2 %
    longer than where the market settled."""
    if closing_pinnacle_odd <= 0:
        return 0.0
    return odd_taken / closing_pinnacle_odd - 1.0


def event_started(event_key: str, now: datetime | None = None) -> bool:
    """A bet's tracking lifecycle closes once its event has kicked off — the
    last Pinnacle snapshot strictly before that becomes the closing line."""
    parsed = parse_event_key(event_key)
    if parsed is None:
        return False
    start, _, _ = parsed
    now = now or datetime.now(timezone.utc)
    return start <= now


def index_quotes_by_market(quotes: Iterable[OddQuote]) -> dict:
    """Build a lookup table (event_key, market, outcome_label, line) -> quote
    so close-lines can find the matching Pinnacle price for each open bet in
    O(1) without re-walking the full quote list per bet."""
    out: dict = {}
    for q in quotes:
        key = (q.event_key, q.market.value, q.outcome.label, q.outcome.line)
        # Keep the freshest quote when several are present for the same key.
        prev = out.get(key)
        if prev is None or q.fetched_at > prev.fetched_at:
            out[key] = q
    return out


@dataclass
class ClvStats:
    n: int
    n_positive: int
    mean_clv_pct: float
    median_clv_pct: float

    @property
    def positive_rate(self) -> float:
        return self.n_positive / self.n if self.n else 0.0

    @property
    def expected_pnl_per_unit(self) -> float:
        """Mean CLV is roughly the long-run expected return per unit staked
        (above Pinnacle's own closing margin, ~2.5 %), so this number is the
        cleanest one-line answer to 'am I actually making money?'."""
        return self.mean_clv_pct / 100.0


def aggregate(rows: list[tuple[float, float]]) -> ClvStats:
    """Compute (mean, median, positive rate) over (odd_taken, closing_odd) pairs."""
    if not rows:
        return ClvStats(0, 0, 0.0, 0.0)
    clvs = sorted(clv_pct(taken, closing) * 100.0 for taken, closing in rows)
    n = len(clvs)
    mean = sum(clvs) / n
    median = clvs[n // 2] if n % 2 == 1 else (clvs[n // 2 - 1] + clvs[n // 2]) / 2
    n_pos = sum(1 for c in clvs if c > 0)
    return ClvStats(n=n, n_positive=n_pos, mean_clv_pct=mean, median_clv_pct=median)


def group_by(
    rows: Iterable[dict], key: str
) -> dict[str, list[tuple[float, float]]]:
    """Partition (odd_taken, closing_odd) pairs by a row attribute for
    per-book / per-market breakdowns."""
    out: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        out[r[key]].append((r["odd_taken"], r["closing_odd"]))
    return dict(out)
