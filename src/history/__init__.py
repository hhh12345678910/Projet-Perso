"""Bet-history importers.

Each bookmaker exposes (behind an authenticated session) an API listing the
user's own settled bets. These importers pull that history and normalise it
into `SettledBet` rows so the daemon can track REAL P&L per book — the
ground-truth counterpart to the value_bets table (what we detected).

Add a new book by writing an importer that yields `SettledBet` objects and
registering it in `registry.py`.
"""

from .base import SettledBet, normalize_result, RESULT_STATES

__all__ = ["SettledBet", "normalize_result", "RESULT_STATES"]
