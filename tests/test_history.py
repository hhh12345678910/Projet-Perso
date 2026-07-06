from __future__ import annotations

from src.history.base import SettledBet, normalize_result
from src.history.kambi import parse_history, _odd, _money, _dt
from src.history.registry import all_importers, importers_for
from src.models import Book
from src.storage import Storage


# A synthetic Kambi-style coupon-history payload. Field names follow the schema
# the parser targets; `parse_history` is defensive via _first(), so this doubles
# as the shape we expect a real capture to (roughly) match.
SAMPLE = {
    "coupons": [
        {
            "coupon": {
                "couponId": 1001,
                "couponType": "SINGLE",
                "createdDate": 1720000000000,
                "settledDate": 1720003600000,
                "stake": 20000,          # ×1000 -> 20.00
                "payout": 47000,         # ×1000 -> 47.00
                "totalOdds": 2350,       # ×1000 -> 2.35
                "result": "WON",
                "currency": "EUR",
                "outcomes": [
                    {
                        "eventName": "Anderlecht - Club Brugge",
                        "eventStartDate": 1719999000000,
                        "criterionName": "Full Time",
                        "label": "Anderlecht",
                        "odds": 2350,
                        "sport": "FOOTBALL",
                        "outcomeResult": "WON",
                    }
                ],
            }
        },
        {
            "coupon": {
                "couponId": 1002,
                "couponType": "COMBINATION",
                "createdDate": 1720100000000,
                "settledDate": 1720103600000,
                "stake": 10000,          # 10.00
                "payout": 0,
                "totalOdds": 6500,       # 6.50
                "result": "LOST",
                "outcomes": [
                    {"eventName": "A - B", "odds": 2000, "outcomeResult": "WON"},
                    {"eventName": "C - D", "odds": 3250, "outcomeResult": "LOST"},
                ],
            }
        },
    ]
}


def test_normalize_result_maps_vocab():
    assert normalize_result("WON") == "won"
    assert normalize_result("Lose") == "lost"
    assert normalize_result("PUSH") == "void"
    assert normalize_result("Cashed Out") == "cashout"
    assert normalize_result("half_won") == "half_won"
    assert normalize_result("something weird") == "pending"
    assert normalize_result(None) == "pending"


def test_money_and_odds_scaling():
    assert _money(20000) == 20.0
    assert _odd(2350) == 2.35
    assert _odd(2.35) == 2.35          # already-decimal tolerated
    assert _money(None) is None


def test_dt_handles_epoch_ms_and_iso():
    assert _dt(1720000000000).year == 2024
    assert _dt("2026-05-17T18:30:00Z").year == 2026
    assert _dt(None) is None


def test_pnl_from_payout_win():
    sb = SettledBet(book=Book.UNIBET_BE, bet_id="x", odd=2.35, stake=20.0,
                    result="won", payout=47.0)
    assert sb.pnl == 27.0


def test_pnl_lost_without_payout():
    sb = SettledBet(book=Book.UNIBET_BE, bet_id="y", odd=2.0, stake=15.0, result="lost")
    assert sb.pnl == -15.0


def test_parse_history_single_enriched():
    bets = parse_history(SAMPLE, Book.UNIBET_BE)
    single = next(b for b in bets if b.bet_id == "1001")
    assert single.legs == 1
    assert single.odd == 2.35
    assert single.stake == 20.0
    assert single.result == "won"
    assert single.pnl == 27.0
    assert single.selection == "Anderlecht"
    assert single.market == "Full Time"
    assert single.event_label == "Anderlecht - Club Brugge"
    assert single.event_key is not None     # derived from "Home - Away"
    assert single.book == Book.UNIBET_BE


def test_parse_history_combo_is_coupon_level():
    bets = parse_history(SAMPLE, Book.UNIBET_BE)
    combo = next(b for b in bets if b.bet_id == "1002")
    assert combo.legs == 2
    assert combo.selection is None          # not attributable per-leg
    assert combo.event_key is None
    assert combo.odd == 6.5
    assert combo.result == "lost"
    assert combo.pnl == -10.0               # payout 0 - stake 10


def test_parse_history_tolerates_empty():
    assert parse_history({}, Book.UNIBET_BE) == []
    assert parse_history({"coupons": []}, Book.UNIBET_BE) == []


def test_registry_has_kambi_and_pending():
    imps = all_importers()
    books = {i.book for i in imps}
    assert Book.UNIBET_BE in books
    assert Book.LADBROKES_BE in books       # pending stub present
    # Kambi importers are unavailable without env config -> skipped cleanly.
    assert all(not i.available() for i in imps)


def test_importers_for_filters():
    only = importers_for(["unibet_be"])
    assert len(only) == 1
    assert only[0].book == Book.UNIBET_BE


def test_storage_upsert_settled_is_idempotent(tmp_path):
    db = tmp_path / "t.db"
    storage = Storage(str(db))
    bets = parse_history(SAMPLE, Book.UNIBET_BE)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    rows = [b.to_row(now) for b in bets]

    inserted, seen = storage.upsert_settled_bets(rows)
    assert (inserted, seen) == (2, 2)

    # Re-import the same rows -> nothing new (UNIQUE(book, bet_id)).
    inserted2, seen2 = storage.upsert_settled_bets(rows)
    assert inserted2 == 0

    by_book = {r["book"]: r for r in storage.pnl_by_book()}
    ub = by_book["unibet_be"]
    assert int(ub["n"]) == 2
    assert round(float(ub["pnl"]), 2) == 17.0   # +27 (win) - 10 (lost combo)
