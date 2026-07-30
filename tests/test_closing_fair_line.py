"""The closing line must be devigged before it can be compared to a taken odd.

Every test here exists because the previous behaviour — comparing against
Pinnacle's displayed price — added the book's whole commission to each bet's
CLV, which on a 6-7% portfolio is larger than the edge being measured.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.clv import clv_pct, pnl, settle
from src.models import Book, MarketType, OddQuote, Outcome
from src.storage import Storage


KICKOFF = datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc)
EVENT = "202606012000::a__vs__b"


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "test.db")


def _q(label: str, odd: float, fetched_at: datetime,
       market: MarketType = MarketType.H2H, line: float | None = None) -> OddQuote:
    return OddQuote(
        event_key=EVENT, book=Book.PINNACLE, market=market,
        outcome=Outcome(label=label, line=line), decimal_odd=odd,
        fetched_at=fetched_at, source_event_id="x",
    )


# ------------------------------------------------------- closing group ------
def test_closing_group_returns_every_outcome_of_one_capture(storage: Storage):
    early = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)
    late = datetime(2026, 6, 1, 19, 55, tzinfo=timezone.utc)
    for label, odd in (("home", 3.60), ("draw", 3.60), ("away", 2.10)):
        storage.insert_quote(_q(label, odd, early))
    for label, odd in (("home", 3.46), ("draw", 3.47), ("away", 2.05)):
        storage.insert_quote(_q(label, odd, late))

    group = storage.pinnacle_closing_group(EVENT, "h2h", None, KICKOFF)

    assert len(group) == 3
    # All three come from the same capture — a devig over a mix of capture
    # times would leave a phantom margin behind.
    assert {r["fetched_at"] for r in group} == {late.isoformat()}
    assert sorted(round(r["decimal_odd"], 2) for r in group) == [2.05, 3.46, 3.47]


def test_closing_group_ignores_quotes_after_kickoff(storage: Storage):
    for label, odd in (("home", 3.46), ("draw", 3.47), ("away", 2.05)):
        storage.insert_quote(_q(label, odd, datetime(2026, 6, 1, 19, 55, tzinfo=timezone.utc)))
    # In-play prices carry a far wider margin; they must never become "closing".
    for label, odd in (("home", 8.00), ("draw", 4.00), ("away", 1.30)):
        storage.insert_quote(_q(label, odd, datetime(2026, 6, 1, 20, 30, tzinfo=timezone.utc)))

    group = storage.pinnacle_closing_group(EVENT, "h2h", None, KICKOFF)

    assert sorted(round(r["decimal_odd"], 2) for r in group) == [2.05, 3.46, 3.47]


def test_closing_group_separates_lines_on_totals(storage: Storage):
    at = datetime(2026, 6, 1, 19, 55, tzinfo=timezone.utc)
    for label, odd, line in (("over", 1.95, 2.5), ("under", 1.95, 2.5),
                             ("over", 3.10, 3.5), ("under", 1.38, 3.5)):
        storage.insert_quote(_q(label, odd, at, market=MarketType.TOTALS, line=line))

    group = storage.pinnacle_closing_group(EVENT, "totals", 2.5, KICKOFF)

    assert len(group) == 2
    assert all(r["line"] == 2.5 for r in group)


def test_closing_group_empty_when_nothing_captured(storage: Storage):
    assert storage.pinnacle_closing_group(EVENT, "h2h", None, KICKOFF) == []


# ------------------------------------------------------------- snapshot -----
def test_snapshot_keeps_both_raw_and_devigged_prices(storage: Storage):
    storage.insert_clv_snapshot(
        value_bet_id=1, pinnacle_odd=3.46, pinnacle_prob=1 / 3.46,
        snapshot_at=KICKOFF, closing=True,
        fair_odd=3.68, fair_prob=1 / 3.68, overround=1.065,
    )
    row = storage.closing_snapshot(1)

    assert row["pinnacle_odd"] == 3.46
    assert row["fair_odd"] == 3.68
    assert math.isclose(row["overround"], 1.065)


# ------------------------------------------------------------- clv_pct ------
def test_clv_is_zero_on_a_bet_that_is_worth_nothing():
    # Taking exactly the fair closing price is a bet with no edge, and must
    # score zero — against the raw price it used to score the whole margin.
    assert math.isclose(clv_pct(3.68, 3.68), 0.0, abs_tol=1e-9)


def test_clv_goes_negative_when_the_market_moves_against_you():
    # Bet 4.00 on a line that was 3.65 and drifted out to 3.90: still a
    # positive-EV bet at the time, but the market disagreed.
    assert clv_pct(4.00, 3.90) < clv_pct(4.00, 3.65)
    assert clv_pct(3.80, 3.90) < 0


# -------------------------------------------------------------- settle ------
def test_settle_h2h_by_winner():
    assert settle("h2h", "home", None, "home", None, None) == "won"
    assert settle("h2h", "home", None, "away", None, None) == "lost"
    assert settle("h2h", "draw", None, "draw", None, None) == "won"


def test_settle_h2h_unknown_without_a_winner():
    assert settle("h2h", "home", None, None, None, None) is None


def test_settle_totals_uses_the_combined_score():
    assert settle("totals", "over 2.5", 2.5, None, 2, 1) == "won"
    assert settle("totals", "under 2.5", 2.5, None, 2, 1) == "lost"
    assert settle("totals", "over 3.5", 3.5, None, 1, 1) == "lost"


def test_settle_totals_exact_line_is_a_push_not_a_loss():
    # Grading a push as a loss would quietly bias measured P&L downwards.
    assert settle("totals", "over 3.0", 3.0, None, 2, 1) == "void"
    assert settle("totals", "under 3.0", 3.0, None, 2, 1) == "void"


def test_settle_unknown_market_is_undecidable():
    assert settle("handicap", "home -1.0", -1.0, "home", 2, 0) is None


# ----------------------------------------------------------------- pnl ------
def test_pnl_pays_profit_only_on_a_win():
    assert math.isclose(pnl("won", 3.0, 25.0), 50.0)
    assert math.isclose(pnl("lost", 3.0, 25.0), -25.0)
    assert math.isclose(pnl("void", 3.0, 25.0), 0.0)


def test_pnl_is_none_while_the_result_is_unknown():
    # None, never 0.0 — an unsettled bet must not read as a break-even one.
    assert pnl(None, 3.0, 25.0) is None


def test_bets_older_than_retention_leave_the_closing_queue(storage: Storage):
    """146 paris échouaient à chaque passage horaire, dont 105 vieux de plus
    d'une semaine : leurs cotes étaient purgées depuis longtemps. Le bruit
    permanent rendait une vraie panne indiscernable — c'est ce que ce retrait
    corrige."""
    from src.models import ValueBet

    def _vb(event_key: str) -> int:
        return storage.insert_value_bet(ValueBet(
            event_key=event_key, book=Book.UNIBET_BE, market=MarketType.H2H,
            outcome=Outcome(label="home"), odd_taken=2.10, fair_prob=0.5,
            fair_odd=2.0, ev_pct=5.0, kelly_stake_pct=1.0,
            detected_at=KICKOFF,
        ))

    old_id = _vb("202605200000::vieux__vs__match")
    keep_id = _vb(EVENT)

    assert {r["id"] for r in storage.open_value_bets()} == {old_id, keep_id}

    assert storage.mark_closing_lost([old_id]) == 1
    assert [r["id"] for r in storage.open_value_bets()] == [keep_id]

    # Idempotent, et un appel vide ne fait rien.
    storage.mark_closing_lost([old_id])
    assert [r["id"] for r in storage.open_value_bets()] == [keep_id]
    assert storage.mark_closing_lost([]) == 0
