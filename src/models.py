from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class MarketType(str, Enum):
    H2H = "h2h"               # 1X2 or moneyline (2-way or 3-way)
    TOTALS = "totals"         # over/under
    HANDICAP = "handicap"     # asian / european handicap
    BTTS = "btts"             # both teams to score


class Book(str, Enum):
    PINNACLE = "pinnacle"
    BETANO_BE = "betano_be"
    UNIBET_BE = "unibet_be"
    LADBROKES_BE = "ladbrokes_be"
    CIRCUS_BE = "circus_be"
    BETFIRST = "betfirst"
    BETCENTER = "betcenter"


@dataclass(frozen=True)
class Event:
    sport: str
    league: str
    home: str
    away: str
    start_time: datetime
    source_id: str
    book: Book


@dataclass(frozen=True)
class Outcome:
    label: str            # e.g. "home", "draw", "away", "over_2.5"
    line: Optional[float] = None


@dataclass(frozen=True)
class OddQuote:
    event_key: str        # normalized key from matcher
    book: Book
    market: MarketType
    outcome: Outcome
    decimal_odd: float
    fetched_at: datetime
    source_event_id: str


@dataclass
class FairLine:
    event_key: str
    market: MarketType
    outcomes: dict[str, float] = field(default_factory=dict)   # label -> fair prob
    method: str = "shin"
    reference_book: Book = Book.PINNACLE
    computed_at: Optional[datetime] = None


@dataclass
class ValueBet:
    event_key: str
    book: Book
    market: MarketType
    outcome: Outcome
    odd_taken: float
    fair_prob: float
    fair_odd: float
    ev_pct: float
    kelly_stake_pct: float
    detected_at: datetime
