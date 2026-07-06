from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..models import Book


# Canonical settlement states. Every book's own vocabulary
# (WON/WIN/LOST/LOSE/PUSH/REFUND/CASHED_OUT/…) is mapped onto one of these so
# the P&L ledger and reports speak one language.
RESULT_STATES = frozenset(
    {"won", "lost", "void", "half_won", "half_lost", "cashout", "pending"}
)

# Book vocabulary -> canonical state. Lower-cased before lookup, so add keys in
# lower case. Unknown strings fall back to "pending" (never silently counted as
# a win or loss — an unrecognised result must not corrupt the P&L total).
_RESULT_ALIASES = {
    "won": "won", "win": "won", "winner": "won", "settled_won": "won",
    "lost": "lost", "lose": "lost", "loss": "lost", "loser": "lost",
    "settled_lost": "lost",
    "void": "void", "voided": "void", "push": "void", "refund": "void",
    "refunded": "void", "cancelled": "void", "canceled": "void",
    "returned": "void", "draw_no_bet_void": "void",
    "half_won": "half_won", "halfwon": "half_won", "half_win": "half_won",
    "won_half": "half_won",
    "half_lost": "half_lost", "halflost": "half_lost", "half_loss": "half_lost",
    "lost_half": "half_lost",
    "cashout": "cashout", "cashedout": "cashout", "cashed_out": "cashout",
    "cash_out": "cashout", "cashed-out": "cashout",
    "pending": "pending", "open": "pending", "unsettled": "pending",
    "running": "pending", "not_settled": "pending", "placed": "pending",
}


def normalize_result(raw: Any) -> str:
    """Map a book's raw result string onto a canonical RESULT_STATES value.
    Unknown or empty values return 'pending' so they never distort P&L."""
    if raw is None:
        return "pending"
    key = str(raw).strip().lower().replace(" ", "_")
    return _RESULT_ALIASES.get(key, "pending")


@dataclass
class SettledBet:
    """One settled (or still-open) bet as reported by a bookmaker, normalised
    to a book-agnostic shape. Maps 1:1 onto the settled_bets table.

    One instance = one coupon. For singles, event/market/selection/line are
    filled from the single leg. For combos/systems they stay None and `legs`
    carries the number of selections; odd/stake/pnl are coupon-level.
    """

    book: Book
    bet_id: str                       # bookmaker's coupon/bet id (dedup key)
    odd: float                        # coupon total decimal odd
    stake: float
    result: str                       # one of RESULT_STATES
    placed_at: Optional[datetime] = None
    settled_at: Optional[datetime] = None
    event_start: Optional[datetime] = None
    sport: Optional[str] = None
    event_label: Optional[str] = None  # "Home - Away" as the book shows it
    event_key: Optional[str] = None    # normalised key (best-effort join to value_bets)
    market: Optional[str] = None
    selection: Optional[str] = None
    line: Optional[float] = None
    payout: Optional[float] = None     # amount returned (0 for a lost bet)
    bet_type: Optional[str] = None     # single / combo / system
    legs: int = 1
    currency: Optional[str] = None
    raw: Optional[str] = None          # original coupon JSON (audit/debug)

    def __post_init__(self) -> None:
        if self.result not in RESULT_STATES:
            self.result = normalize_result(self.result)

    @property
    def pnl(self) -> float:
        """Profit/loss for this coupon.

        Prefer an explicit payout when the book gives one (pnl = payout - stake).
        Otherwise derive it from the result: a win returns stake*odd, a loss
        costs the stake, void/cashout without a payout net to zero, pending is
        zero (not yet resolved)."""
        # Not yet resolved: no P&L, whatever a placeholder payout (often 0) says.
        if self.result == "pending":
            return 0.0
        if self.payout is not None:
            return round(self.payout - self.stake, 2)
        if self.result == "won":
            return round(self.stake * self.odd - self.stake, 2)
        if self.result == "lost":
            return round(-self.stake, 2)
        # void / half_* / cashout / pending without an explicit payout: we can't
        # infer the amount reliably, so treat as break-even until a payout is
        # known. Importers should set payout for these whenever the API gives it.
        return 0.0

    def to_row(self, imported_at: datetime) -> dict:
        """Flatten to the dict shape Storage.upsert_settled_bets expects."""
        def _iso(d: Optional[datetime]) -> Optional[str]:
            return d.isoformat() if d is not None else None

        return {
            "book": self.book.value,
            "bet_id": str(self.bet_id),
            "placed_at": _iso(self.placed_at),
            "settled_at": _iso(self.settled_at),
            "event_start": _iso(self.event_start),
            "sport": self.sport,
            "event_label": self.event_label,
            "event_key": self.event_key,
            "market": self.market,
            "selection": self.selection,
            "line": self.line,
            "odd": self.odd,
            "stake": self.stake,
            "payout": self.payout,
            "pnl": self.pnl,
            "result": self.result,
            "bet_type": self.bet_type,
            "legs": self.legs,
            "currency": self.currency,
            "raw": self.raw,
            "imported_at": imported_at.isoformat(),
        }


class HistoryImportError(RuntimeError):
    """Raised when an importer can't reach or authenticate against the history
    API (expired cookie, missing config). The CLI catches it and reports which
    book failed without aborting the other books' imports."""


class HistoryImporter(ABC):
    """Base class for a per-book bet-history importer.

    Concrete importers own their auth (cookie/token from env) and their API
    quirks, and yield normalised `SettledBet` objects. `available()` lets the
    CLI skip a book that isn't configured (no cookie) instead of erroring.
    """

    book: Book

    @abstractmethod
    def available(self) -> bool:
        """True when the importer has the config it needs to run (e.g. a cookie
        is present in the environment). Unconfigured books are skipped."""

    @abstractmethod
    def fetch(self, *, since: Optional[datetime] = None) -> Iterable[SettledBet]:
        """Yield settled bets, most recent first. `since` is an optional lower
        bound (from the last import) so importers can stop paging early."""
