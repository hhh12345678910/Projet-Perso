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
    GOLDEN_PALACE = "golden_palace"
    STARCASINO_SPORT = "starcasino_sport"
    SEVEN_ELEVEN_BE = "seven_eleven_be"  # 711.be — Kambi platform, same as Unibet
    BINGOAL_BE = "bingoal_be"            # Bingoal.be — Kambi platform, same as Unibet
    SCOOORE_BE = "scooore_be"            # Scooore.be (Loterie Nationale) — Kambi, same as Unibet
    MERIDIAN_BE = "meridian_be"          # Meridianbet.be — own platform, independent odds
    NAPOLEON_BE = "napoleon_be"          # Napoleonsports.be — Superbet platform, independent odds
    BETCENTER = "betcenter"              # Betcenter.be - Cashpoint/Merkur, cotes independantes
    SMARKETS = "smarkets"        # London exchange, used as a secondary sharp reference


BETANO_OPERATOR_ID = "22"
BETANO_LANGUAGE_ID_FR = "9"


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
    # Human-readable competition ("Suisse - Super League"), when the source
    # provides one. The event_key only carries normalised team names, so this
    # is the only way an alert can name the competition.
    league: Optional[str] = None
    # Clé d'origine, avant réalignement sur la référence. Le tennis tolère
    # jusqu'à 3 h d'écart entre l'heure annoncée par Pinnacle et celle du book,
    # parce qu'un match commence quand le précédent libère le court. Après
    # réalignement, la cote porte l'heure de Pinnacle : si celle-ci est la plus
    # tardive, le match paraît à venir alors qu'il est déjà en cours. Garder
    # l'heure du book permet de retenir la plus précoce des deux.
    book_event_key: Optional[str] = None
    # Score de similarité retenu par le rapprochement flou (0-100), ou 100 pour
    # une égalité exacte de clé. Il était calculé puis jeté : un appariement à
    # 86 et un appariement à 100 n'inspirent pourtant pas la même confiance, et
    # « mauvais matching » est l'une des causes soupçonnées de faux positifs.
    match_score: Optional[float] = None


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
    league: Optional[str] = None
    # Which sharp source produced the fair line this was measured against.
    # Pinnacle unless it didn't price the market and a fallback stood in — a
    # bet valued against a thinner reference deserves less confidence, and
    # without this the two are indistinguishable after the fact.
    reference_book: Optional[Book] = None
    # Voir OddQuote.book_event_key : l'heure annoncée par le book, quand elle
    # diffère de celle de la référence. Sert à ne pas alerter sur un match déjà
    # commencé selon le book mais pas encore selon Pinnacle.
    book_event_key: Optional[str] = None
    # Twin books offering the exact same price for this bet (e.g. Unibet & 711
    # share the Kambi feed). Listed alongside `book` in the alert so the same
    # opportunity fires once, naming every book where you can take it.
    also_books: tuple[Book, ...] = ()
    # Voir OddQuote.match_score.
    match_score: Optional[float] = None
