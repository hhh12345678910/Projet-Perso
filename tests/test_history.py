from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.history.base import SettledBet, normalize_result
from src.history.kambi import parse_history, _odd, _money, _line, _dt
from src.history.registry import all_importers, importers_for
from src.models import Book
from src.storage import Storage


# Real Kambi coupon-history sample, trimmed from a fr.unibetsports.be HAR to six
# representative coupons: WON+line (totals), WON, LOST, VOID, OPEN, LOST combo.
FIXTURE = Path(__file__).parent / "fixtures" / "kambi_history_sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _by_odd(bets: list[SettledBet], odd: float) -> SettledBet:
    return next(b for b in bets if abs(b.odd - odd) < 1e-6)


# -- unit helpers -----------------------------------------------------------

def test_normalize_result_maps_vocab():
    assert normalize_result("WON") == "won"
    assert normalize_result("LOST") == "lost"
    assert normalize_result("VOID") == "void"
    assert normalize_result("OPEN") == "pending"
    assert normalize_result("something weird") == "pending"
    assert normalize_result(None) == "pending"


def test_scaling_helpers():
    assert _money(25000) == 25.0
    assert _odd(2550) == 2.55
    assert _odd(7056) == 7.056
    assert _line(2500) == 2.5
    assert _line(-1500) == -1.5
    assert _money(None) is None


def test_dt_parses_iso_and_epoch():
    assert _dt("2026-07-06T17:24:46.774Z").year == 2026
    assert _dt(1720000000000).year == 2024
    assert _dt(None) is None


def test_pnl_pending_is_zero_even_with_zero_payout():
    # A pending bet reports payout 0 — that must NOT read as a full loss.
    sb = SettledBet(book=Book.UNIBET_BE, bet_id="x", odd=2.75, stake=16.0,
                    result="pending", payout=0.0)
    assert sb.pnl == 0.0


def test_pnl_from_payout():
    win = SettledBet(book=Book.UNIBET_BE, bet_id="w", odd=2.55, stake=25.0,
                     result="won", payout=63.75)
    assert win.pnl == 38.75
    void = SettledBet(book=Book.UNIBET_BE, bet_id="v", odd=1.7, stake=30.0,
                      result="void", payout=30.0)
    assert void.pnl == 0.0


# -- real-payload parsing ---------------------------------------------------

def test_parse_history_counts_one_row_per_bet():
    bets = parse_history(_load(), Book.UNIBET_BE)
    assert len(bets) == 6
    assert all(b.book == Book.UNIBET_BE for b in bets)


def test_parse_history_won_single_enriched():
    bets = parse_history(_load(), Book.UNIBET_BE)
    won = _by_odd(bets, 2.55)
    assert won.result == "won"
    assert won.stake == 25.0
    assert won.payout == 63.75
    assert won.pnl == 38.75
    assert won.legs == 1
    assert won.selection            # outcome label present
    assert won.market               # betOffer criterion present
    assert won.event_label
    assert won.event_key is not None
    assert won.sport == "football"


def test_parse_history_lost_uses_played_odds_not_zero_betodds():
    bets = parse_history(_load(), Book.UNIBET_BE)
    lost = _by_odd(bets, 3.85)      # betOdds is 0 on a loser; playedOdds=3850
    assert lost.result == "lost"
    assert lost.pnl == -10.0


def test_parse_history_void_nets_zero():
    bets = parse_history(_load(), Book.UNIBET_BE)
    void = next(b for b in bets if b.result == "void")
    assert void.pnl == 0.0


def test_parse_history_open_is_pending_zero_pnl():
    bets = parse_history(_load(), Book.UNIBET_BE)
    pending = next(b for b in bets if b.result == "pending")
    assert pending.pnl == 0.0
    assert pending.payout is None


def test_parse_history_combo_is_coupon_level():
    bets = parse_history(_load(), Book.UNIBET_BE)
    combo = next(b for b in bets if b.legs > 1)
    assert combo.legs == 2
    assert combo.selection is None
    assert combo.event_key is None
    assert combo.bet_type == "combo"
    assert abs(combo.odd - 7.056) < 1e-6
    assert combo.event_label == "Combiné 2 sélections"


def test_parse_history_tolerates_empty():
    assert parse_history({}, Book.UNIBET_BE) == []
    assert parse_history({"historyCoupons": []}, Book.UNIBET_BE) == []


# -- registry ---------------------------------------------------------------

def test_registry_has_kambi_and_pending():
    imps = all_importers()
    books = {i.book for i in imps}
    assert Book.UNIBET_BE in books
    assert Book.LADBROKES_BE in books           # pending stub present
    assert all(not i.available() for i in imps)  # nothing configured in tests


def test_importers_for_filters():
    only = importers_for(["unibet_be"])
    assert len(only) == 1 and only[0].book == Book.UNIBET_BE


# -- storage ----------------------------------------------------------------

def test_storage_upsert_idempotent_and_pnl(tmp_path):
    storage = Storage(str(tmp_path / "t.db"))
    bets = parse_history(_load(), Book.UNIBET_BE)
    now = datetime.now(timezone.utc)
    rows = [b.to_row(now) for b in bets]

    inserted, seen = storage.upsert_settled_bets(rows)
    assert (inserted, seen) == (6, 6)
    assert storage.upsert_settled_bets(rows)[0] == 0   # re-import inserts nothing

    agg = {r["book"]: r for r in storage.pnl_by_book()}["unibet_be"]
    assert int(agg["n"]) == 6
    assert int(agg["pending"]) == 1
    assert round(float(agg["staked"]), 2) == 80.0      # 25+5+10+30+10, open excluded
    assert round(float(agg["pnl"]), 2) == 58.75        # 38.75 + 40 - 10 + 0 - 10
    assert int(agg["wins"]) == 2
