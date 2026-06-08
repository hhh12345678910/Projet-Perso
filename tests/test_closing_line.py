from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.models import Book, MarketType, OddQuote, Outcome
from src.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "test.db")


def _q(book: Book, fetched_at: datetime, label: str = "home", line: float | None = None,
       market: MarketType = MarketType.H2H, odd: float = 2.0) -> OddQuote:
    return OddQuote(
        event_key="202606010000::a__vs__b",
        book=book,
        market=market,
        outcome=Outcome(label=label, line=line),
        decimal_odd=odd,
        fetched_at=fetched_at,
        source_event_id="x",
    )


def test_latest_pinnacle_quote_before_returns_most_recent(storage: Storage):
    # Three Pinnacle snapshots leading up to kickoff at 20:00.
    storage.insert_quote(_q(Book.PINNACLE, datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc), odd=2.10))
    storage.insert_quote(_q(Book.PINNACLE, datetime(2026, 6, 1, 19, 30, tzinfo=timezone.utc), odd=2.05))
    storage.insert_quote(_q(Book.PINNACLE, datetime(2026, 6, 1, 19, 55, tzinfo=timezone.utc), odd=2.00))

    kickoff = datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc)
    row = storage.latest_pinnacle_quote_before(
        event_key="202606010000::a__vs__b",
        market="h2h", outcome_label="home", line=None,
        before=kickoff,
    )
    assert row is not None
    # Most-recent-before-kickoff is the 19:55 snapshot.
    assert row["decimal_odd"] == 2.00


def test_latest_pinnacle_quote_before_ignores_post_kickoff_rows(storage: Storage):
    # Pinnacle snapshots both pre- and post-kickoff. The post-kickoff one
    # would only ever land in the table if scan ran while a live market
    # was still open; we still want the closing line, not the in-play one.
    storage.insert_quote(_q(Book.PINNACLE, datetime(2026, 6, 1, 19, 30, tzinfo=timezone.utc), odd=2.05))
    storage.insert_quote(_q(Book.PINNACLE, datetime(2026, 6, 1, 20, 30, tzinfo=timezone.utc), odd=1.80))

    kickoff = datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc)
    row = storage.latest_pinnacle_quote_before(
        event_key="202606010000::a__vs__b",
        market="h2h", outcome_label="home", line=None,
        before=kickoff,
    )
    assert row is not None
    assert row["decimal_odd"] == 2.05  # not the 1.80 in-play one


def test_latest_pinnacle_quote_before_ignores_other_books(storage: Storage):
    # A soft-book quote at the same (event, market, outcome) shouldn't be
    # picked up as a Pinnacle closing line.
    storage.insert_quote(_q(Book.UNIBET_BE, datetime(2026, 6, 1, 19, 55, tzinfo=timezone.utc), odd=2.30))

    kickoff = datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc)
    row = storage.latest_pinnacle_quote_before(
        event_key="202606010000::a__vs__b",
        market="h2h", outcome_label="home", line=None,
        before=kickoff,
    )
    assert row is None


def test_latest_pinnacle_quote_before_line_null_vs_value(storage: Storage):
    # H2H quotes have line IS NULL; totals quotes have line=value. The
    # lookup must distinguish the two so we don't mis-attribute prices.
    storage.insert_quote(_q(
        Book.PINNACLE, datetime(2026, 6, 1, 19, 30, tzinfo=timezone.utc),
        label="over", line=2.5, market=MarketType.TOTALS, odd=1.95,
    ))
    storage.insert_quote(_q(
        Book.PINNACLE, datetime(2026, 6, 1, 19, 30, tzinfo=timezone.utc),
        label="over", line=3.5, market=MarketType.TOTALS, odd=2.20,
    ))

    kickoff = datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc)
    row = storage.latest_pinnacle_quote_before(
        event_key="202606010000::a__vs__b",
        market="totals", outcome_label="over", line=2.5,
        before=kickoff,
    )
    assert row is not None and row["decimal_odd"] == 1.95
    row = storage.latest_pinnacle_quote_before(
        event_key="202606010000::a__vs__b",
        market="totals", outcome_label="over", line=3.5,
        before=kickoff,
    )
    assert row is not None and row["decimal_odd"] == 2.20


def test_latest_pinnacle_quote_before_no_match_returns_none(storage: Storage):
    kickoff = datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc)
    assert storage.latest_pinnacle_quote_before(
        event_key="nope", market="h2h", outcome_label="home", line=None,
        before=kickoff,
    ) is None
