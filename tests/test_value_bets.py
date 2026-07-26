from __future__ import annotations

from datetime import datetime, timezone

from src.config import ScanConfig
from src.main import find_value_bets
from src.matcher import event_key
from src.models import Book, FairLine, MarketType, OddQuote, Outcome

NOW = datetime(2026, 6, 16, 20, 0, tzinfo=timezone.utc)
EK = event_key("Home FC", "Away FC", NOW)


def _q(book: Book, market: MarketType, label: str, odd: float,
       line: float | None = None) -> OddQuote:
    return OddQuote(
        event_key=EK,
        book=book,
        market=market,
        outcome=Outcome(label=label, line=line),
        decimal_odd=odd,
        fetched_at=NOW,
        source_event_id="x",
    )


def _fair(market: MarketType, outcomes: dict[str, float],
          line: float | None = None) -> dict:
    key = (EK, market, line)
    return {key: FairLine(event_key=EK, market=market, outcomes=outcomes)}


def test_h2h_value_bet_is_detected():
    # Soft book prices away at 2.50 while the fair away prob is 0.50 (fair odd
    # 2.00) -> ~25% EV, a genuine moneyline value bet.
    quotes = [
        _q(Book.BETFIRST, MarketType.H2H, "home", 1.60),
        _q(Book.BETFIRST, MarketType.H2H, "away", 2.50),
    ]
    fair = _fair(MarketType.H2H, {"home": 0.50, "away": 0.50})
    bets = find_value_bets(quotes, fair, ScanConfig())
    assert [b.outcome.label for b in bets] == ["away"]
    assert bets[0].market == MarketType.H2H


def test_handicap_value_bet_is_excluded():
    # Same shape but on a handicap line: even with a huge apparent edge, the
    # mismatched line semantics across books make this a phantom. It must not
    # surface as a value bet.
    quotes = [
        _q(Book.BETFIRST, MarketType.HANDICAP, "home", 1.60, line=-1.0),
        _q(Book.BETFIRST, MarketType.HANDICAP, "away", 3.75, line=-1.0),
    ]
    fair = _fair(MarketType.HANDICAP, {"home": 0.55, "away": 0.45}, line=-1.0)
    bets = find_value_bets(quotes, fair, ScanConfig())
    assert bets == []


# ---------------------------------------------------------------------------
# Secondary sharp source is a fallback, never a blend. Pinnacle is the
# reference because it's the most accurate source available; averaging it with
# anything less accurate can only move the estimate away from the truth.
# ---------------------------------------------------------------------------

def _sharp_quote(book, label, odd, key="202607261800::alpha__vs__beta"):
    from datetime import datetime, timezone
    from src.models import OddQuote, Outcome, MarketType

    return OddQuote(
        event_key=key, book=book, market=MarketType.H2H,
        outcome=Outcome(label=label), decimal_odd=odd,
        fetched_at=datetime.now(timezone.utc), source_event_id="x",
    )


def test_pinnacle_line_is_untouched_by_the_secondary():
    from src.main import build_fair_lines
    from src.models import Book, MarketType

    pin = [_sharp_quote(Book.PINNACLE, "home", 2.00),
           _sharp_quote(Book.PINNACLE, "away", 2.00)]
    # A secondary disagreeing sharply must not shift the Pinnacle-derived line.
    sec = [_sharp_quote(Book.SMARKETS, "home", 1.20),
           _sharp_quote(Book.SMARKETS, "away", 6.00)]

    alone = build_fair_lines(pin, "shin")
    withsec = build_fair_lines(pin, "shin", secondary_quotes=sec)
    key = ("202607261800::alpha__vs__beta", MarketType.H2H, None)
    assert withsec[key].outcomes == alone[key].outcomes
    assert withsec[key].reference_book is Book.PINNACLE


def test_secondary_fills_events_pinnacle_does_not_price():
    from src.main import build_fair_lines
    from src.models import Book, MarketType

    pin = [_sharp_quote(Book.PINNACLE, "home", 2.00),
           _sharp_quote(Book.PINNACLE, "away", 2.00)]
    other = "202607261900::gamma__vs__delta"
    sec = [_sharp_quote(Book.SMARKETS, "home", 1.80, other),
           _sharp_quote(Book.SMARKETS, "away", 2.20, other)]

    fair = build_fair_lines(pin, "shin", secondary_quotes=sec)
    sec_key = (other, MarketType.H2H, None)
    assert sec_key in fair
    # Tagged with its real source, so a bet valued against the fallback stays
    # distinguishable from one valued against Pinnacle.
    assert fair[sec_key].reference_book is Book.SMARKETS


def test_secondary_labels_do_not_leak_into_a_pinnacle_market():
    """Pinnacle listing a 2-way market while an exchange lists 3-way must not
    graft a 'draw' onto the Pinnacle fair line."""
    from src.main import build_fair_lines
    from src.models import MarketType, Book

    pin = [_sharp_quote(Book.PINNACLE, "home", 2.00),
           _sharp_quote(Book.PINNACLE, "away", 2.00)]
    sec = [_sharp_quote(Book.SMARKETS, "home", 2.60),
           _sharp_quote(Book.SMARKETS, "draw", 3.40),
           _sharp_quote(Book.SMARKETS, "away", 2.90)]

    fair = build_fair_lines(pin, "shin", secondary_quotes=sec)
    key = ("202607261800::alpha__vs__beta", MarketType.H2H, None)
    assert set(fair[key].outcomes) == {"home", "away"}


# ---------------------------------------------------------------------------
# Smarkets needs ~1 request per event at 0.5s spacing (~100s for one sport),
# so running it inline every cycle would make it the bottleneck for every
# other book. It's cached — harmless here, since it's only consulted for
# markets Pinnacle doesn't price, which drift slowly.
# ---------------------------------------------------------------------------

def _drain_smarkets_threads():
    import threading
    for t in threading.enumerate():
        if t.name.startswith("smarkets-"):
            t.join(timeout=5)


def test_smarkets_never_blocks_the_cycle(monkeypatch):
    """A live install saw a 26-minute refresh run inside the scan cycle, which
    silenced that sport for the duration. The call must return at once."""
    import time as _t
    import src.main as main

    main._SMARKETS_CACHE.clear()
    main._SMARKETS_REFRESHING.clear()

    def slow(sm, sport, **kw):
        _t.sleep(1.0)
        return iter([])

    monkeypatch.setattr(main, "smarkets_iter_all_quotes", slow)
    monkeypatch.setattr(main, "SmarketsScraper", lambda *a, **k: _NullScraper())

    t0 = _t.monotonic()
    assert main.fetch_smarkets_quotes("soccer") == []
    assert _t.monotonic() - t0 < 0.3
    _drain_smarkets_threads()


def test_smarkets_refreshes_only_once_at_a_time(monkeypatch):
    import time as _t
    import src.main as main

    main._SMARKETS_CACHE.clear()
    main._SMARKETS_REFRESHING.clear()
    calls = {"n": 0}

    def fake(sm, sport, **kw):
        calls["n"] += 1
        _t.sleep(0.5)
        return iter([])

    monkeypatch.setattr(main, "smarkets_iter_all_quotes", fake)
    monkeypatch.setattr(main, "SmarketsScraper", lambda *a, **k: _NullScraper())

    for _ in range(5):
        main.fetch_smarkets_quotes("soccer")
    _drain_smarkets_threads()
    assert calls["n"] == 1


def test_smarkets_serves_the_snapshot_once_refreshed(monkeypatch):
    import src.main as main
    from src.models import Book

    main._SMARKETS_CACHE.clear()
    main._SMARKETS_REFRESHING.clear()
    good = [_sharp_quote(Book.SMARKETS, "home", 2.0)]

    monkeypatch.setattr(main, "smarkets_iter_all_quotes", lambda sm, sport, **kw: iter(good))
    monkeypatch.setattr(main, "SmarketsScraper", lambda *a, **k: _NullScraper())
    monkeypatch.setenv("SMARKETS_TTL_S", "900")

    assert main.fetch_smarkets_quotes("soccer") == []      # nothing cached yet
    _drain_smarkets_threads()
    assert len(main.fetch_smarkets_quotes("soccer")) == 1   # snapshot now served


def test_smarkets_failure_keeps_the_previous_snapshot(monkeypatch):
    """One failed scrape shouldn't drop the fallback — the events it covers
    have no other sharp reference at all."""
    import httpx
    import src.main as main
    from src.models import Book

    main._SMARKETS_CACHE.clear()
    main._SMARKETS_REFRESHING.clear()
    good = [_sharp_quote(Book.SMARKETS, "home", 2.0)]
    state = {"first": True}

    def fake(sm, sport, **kw):
        if state["first"]:
            state["first"] = False
            return iter(good)
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(main, "smarkets_iter_all_quotes", fake)
    monkeypatch.setattr(main, "SmarketsScraper", lambda *a, **k: _NullScraper())
    monkeypatch.setenv("SMARKETS_TTL_S", "0")   # every call sees a stale cache

    main.fetch_smarkets_quotes("soccer")
    _drain_smarkets_threads()
    assert len(main.fetch_smarkets_quotes("soccer")) == 1
    _drain_smarkets_threads()
    assert len(main.fetch_smarkets_quotes("soccer")) == 1


class _NullScraper:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_value_bet_records_which_reference_valued_it():
    """A bet measured against a fallback deserves less confidence than one
    measured against Pinnacle. Without this they're indistinguishable later."""
    from src.main import build_fair_lines, find_value_bets
    from src.config import ScanConfig
    from src.models import Book, MarketType, OddQuote, Outcome
    from datetime import datetime, timezone

    other = "202607261900::gamma__vs__delta"
    pin = [_sharp_quote(Book.PINNACLE, "home", 2.00),
           _sharp_quote(Book.PINNACLE, "away", 2.00)]
    sec = [_sharp_quote(Book.SMARKETS, "home", 2.00, other),
           _sharp_quote(Book.SMARKETS, "away", 2.00, other)]
    fair = build_fair_lines(pin, "shin", secondary_quotes=sec)

    def soft(key):
        return [
            OddQuote(event_key=key, book=Book.BETANO_BE, market=MarketType.H2H,
                     outcome=Outcome(label=lbl), decimal_odd=2.6,
                     fetched_at=datetime.now(timezone.utc), source_event_id="x")
            for lbl in ("home", "away")
        ]

    cfg = ScanConfig(sport="soccer", min_ev_pct=1.0)
    by_ref = {
        b.event_key: b.reference_book
        for b in find_value_bets(soft("202607261800::alpha__vs__beta") + soft(other), fair, cfg)
    }
    assert by_ref["202607261800::alpha__vs__beta"] is Book.PINNACLE
    assert by_ref[other] is Book.SMARKETS
