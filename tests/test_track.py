"""Tracking a tap on Jouer, and recovering closings taken before the devig fix."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import track
from src.config import ScanConfig
from src.main import _closing_prices, _track_rows
from src.models import Book, MarketType, OddQuote, Outcome, ValueBet
from src.storage import Storage


EVENT = "202606012000::a__vs__b"
KICKOFF = datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc)
CAPTURE = datetime(2026, 6, 1, 19, 55, tzinfo=timezone.utc)


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    st = Storage(tmp_path / "test.db")
    st.upsert_event(EVENT, "soccer", "L", "a", "b", KICKOFF)
    for label, odd in (("home", 3.46), ("draw", 3.47), ("away", 2.05)):
        st.insert_quote(OddQuote(
            event_key=EVENT, book=Book.PINNACLE, market=MarketType.H2H,
            outcome=Outcome(label=label, line=None), decimal_odd=odd,
            fetched_at=CAPTURE, source_event_id="x",
        ))
    return st


def _bet(storage: Storage, odd: float = 4.00) -> int:
    return storage.insert_value_bet(ValueBet(
        event_key=EVENT, book=Book.STARCASINO_SPORT, market=MarketType.H2H,
        outcome=Outcome(label="home", line=None), odd_taken=odd,
        fair_prob=1 / 3.64, fair_odd=3.64, ev_pct=9.89,
        kelly_stake_pct=1.2, detected_at=CAPTURE,
    ))


# ------------------------------------------------------ closing prices ------
def test_closing_prices_strips_the_commission(storage: Storage):
    _bet(storage)
    row = storage.open_value_bets()[0]

    raw, fair, prob, over = _closing_prices(storage, ScanConfig(), row)

    assert raw == 3.46
    # The devigged price is longer than the displayed one — that gap IS the
    # commission, and counting it as CLV was the whole bug.
    assert fair > raw
    assert over == pytest.approx(1.065, abs=1e-3)
    assert prob == pytest.approx(1 / fair)


def test_closing_prices_refuses_a_market_it_cannot_devig(tmp_path: Path):
    st = Storage(tmp_path / "thin.db")
    st.upsert_event(EVENT, "soccer", "L", "a", "b", KICKOFF)
    st.insert_quote(OddQuote(
        event_key=EVENT, book=Book.PINNACLE, market=MarketType.H2H,
        outcome=Outcome(label="home", line=None), decimal_odd=3.46,
        fetched_at=CAPTURE, source_event_id="x",
    ))
    _bet(st)

    raw, fair, _, _ = _closing_prices(st, ScanConfig(), st.open_value_bets()[0])

    assert raw == 3.46
    assert fair is None  # one quote alone carries no visible margin


# ----------------------------------------------------------- backfill -------
def test_backfill_recomputes_a_snapshot_taken_before_the_fix(storage: Storage):
    vb_id = _bet(storage)
    # A snapshot as the old code wrote it: raw price only.
    storage.insert_clv_snapshot(
        value_bet_id=vb_id, pinnacle_odd=3.46, pinnacle_prob=1 / 3.46,
        snapshot_at=KICKOFF, closing=True,
    )
    pending = storage.closing_snapshots_missing_fair()
    assert len(pending) == 1

    raw, fair, prob, over = _closing_prices(storage, ScanConfig(), pending[0])
    storage.update_snapshot_fair(int(pending[0]["snapshot_id"]), fair, prob, over)

    assert storage.closing_snapshots_missing_fair() == []
    assert storage.closing_snapshot(vb_id)["fair_odd"] == pytest.approx(fair)


# -------------------------------------------------------------- played ------
def test_played_bet_inherits_the_detection(storage: Storage):
    vb_id = _bet(storage)
    key = f"{EVENT}|h2h|home|None"
    vb = storage.latest_value_bet_for(EVENT, "h2h", "home", None)

    storage.record_played_bet(key, KICKOFF, track.STAKE_EUR, vb, "soccer", "StarCasino")
    row = storage.played_bet(key)

    assert row["value_bet_id"] == vb_id
    assert row["fair_odd"] == pytest.approx(3.64)
    assert row["ev_pct"] == pytest.approx(9.89)
    assert row["stake"] == track.STAKE_EUR


def test_replaying_a_selection_does_not_create_a_second_bet(storage: Storage):
    _bet(storage)
    key = f"{EVENT}|h2h|home|None"
    vb = storage.latest_value_bet_for(EVENT, "h2h", "home", None)

    storage.record_played_bet(key, KICKOFF, 25.0, vb, "soccer", "StarCasino")
    storage.record_played_bet(key, KICKOFF, 25.0, vb, "soccer", "StarCasino")

    assert len(storage.played_bets_with_clv()) == 1


def test_track_rows_carry_clv_and_pnl_once_settled(storage: Storage):
    vb_id = _bet(storage)
    raw, fair, prob, over = _closing_prices(storage, ScanConfig(), storage.open_value_bets()[0])
    storage.insert_clv_snapshot(
        value_bet_id=vb_id, pinnacle_odd=raw, pinnacle_prob=1 / raw,
        snapshot_at=KICKOFF, closing=True, fair_odd=fair, fair_prob=prob, overround=over,
    )
    storage.record_played_bet(
        f"{EVENT}|h2h|home|None", KICKOFF, track.STAKE_EUR,
        storage.latest_value_bet_for(EVENT, "h2h", "home", None), "soccer", "StarCasino",
    )
    storage.record_result(EVENT, "home", KICKOFF, home_score=2, away_score=1)

    row = _track_rows(storage)[0]
    cell = dict(zip(track.HEADERS, row))

    assert cell["Résultat"] == "Gagné"
    # 25 € at 4.00 returns 75 € of profit.
    assert cell["P&L"] == "+75.00"
    assert cell["Cumul P&L"] == "+75.00"
    assert float(cell["CLV %"]) == pytest.approx((4.00 / fair - 1) * 100, abs=1e-2)
    assert cell["Score dom."] == "2"


def test_track_rows_leave_pnl_blank_while_unsettled(storage: Storage):
    _bet(storage)
    storage.record_played_bet(
        f"{EVENT}|h2h|home|None", KICKOFF, track.STAKE_EUR,
        storage.latest_value_bet_for(EVENT, "h2h", "home", None), "soccer", "StarCasino",
    )

    cell = dict(zip(track.HEADERS, _track_rows(storage)[0]))

    assert cell["Résultat"] == ""
    assert cell["P&L"] == ""      # never 0.00 — that would read as break-even
    assert cell["CLV %"] == ""    # no closing line captured yet


# ---------------------------------------------------------------- file ------
def test_append_writes_the_header_once(tmp_path: Path):
    path = tmp_path / "t.csv"
    track.append(path, ["a"] * len(track.HEADERS))
    track.append(path, ["b"] * len(track.HEADERS))

    rows = list(csv.reader(path.open(encoding="utf-8")))
    assert rows[0] == track.HEADERS
    assert len(rows) == 3
