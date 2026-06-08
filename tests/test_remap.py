from __future__ import annotations

from datetime import datetime, timezone

from src.main import _flip_outcome_for_swap, remap_to_reference
from src.matcher import event_key
from src.models import Book, MarketType, OddQuote, Outcome


NOW = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)


def _q(book: Book, market: MarketType, label: str, odd: float,
       home: str, away: str, line: float | None = None) -> OddQuote:
    return OddQuote(
        event_key=event_key(home, away, NOW),
        book=book,
        market=market,
        outcome=Outcome(label=label, line=line),
        decimal_odd=odd,
        fetched_at=NOW,
        source_event_id="x",
    )


def test_flip_swaps_home_and_away_for_h2h():
    flipped = _flip_outcome_for_swap(Outcome(label="home"), MarketType.H2H)
    assert flipped.label == "away"
    flipped = _flip_outcome_for_swap(Outcome(label="away"), MarketType.H2H)
    assert flipped.label == "home"


def test_flip_leaves_draw_alone():
    assert _flip_outcome_for_swap(Outcome(label="draw"), MarketType.H2H).label == "draw"


def test_flip_no_op_for_totals():
    # Over/under labels don't belong to either team — swap shouldn't touch them.
    over = Outcome(label="over", line=2.5)
    assert _flip_outcome_for_swap(over, MarketType.TOTALS) == over
    under = Outcome(label="under", line=2.5)
    assert _flip_outcome_for_swap(under, MarketType.TOTALS) == under


def test_flip_handles_unknown_label_gracefully():
    # Anything that isn't home/away/draw stays as-is; the alternative would
    # silently drop legitimate exotic markets we forgot to plan for.
    out = _flip_outcome_for_swap(Outcome(label="foo"), MarketType.H2H)
    assert out.label == "foo"


def test_remap_flips_outcome_when_teams_were_swapped():
    # Pinnacle reference: Nigeria home, Senegal away
    ref = event_key("Nigeria", "Senegal", NOW)
    # Soft book has the teams in the opposite order — same match, just listed
    # backwards. Its "home" label is actually pointing at Senegal.
    soft_quotes = [
        _q(Book.UNIBET_BE, MarketType.H2H, "home", 3.50, "Senegal", "Nigeria"),
        _q(Book.UNIBET_BE, MarketType.H2H, "draw", 3.20, "Senegal", "Nigeria"),
        _q(Book.UNIBET_BE, MarketType.H2H, "away", 2.10, "Senegal", "Nigeria"),
    ]
    remapped = remap_to_reference(soft_quotes, [ref])
    assert len(remapped) == 3

    # The labels must be flipped so they align with the reference frame:
    # what the soft book called "home" (Senegal) becomes "away".
    by_label = {q.outcome.label: q for q in remapped}
    assert by_label["away"].decimal_odd == 3.50    # was home @ 3.50
    assert by_label["draw"].decimal_odd == 3.20    # draw stays put
    assert by_label["home"].decimal_odd == 2.10    # was away @ 2.10
    # And the event_key now matches Pinnacle's reference.
    assert all(q.event_key == ref for q in remapped)


def test_remap_leaves_outcome_untouched_when_teams_are_already_aligned():
    ref = event_key("Nigeria", "Senegal", NOW)
    soft_quotes = [
        _q(Book.UNIBET_BE, MarketType.H2H, "home", 2.10, "Nigeria", "Senegal"),
        _q(Book.UNIBET_BE, MarketType.H2H, "away", 3.50, "Nigeria", "Senegal"),
    ]
    remapped = remap_to_reference(soft_quotes, [ref])
    by_label = {q.outcome.label: q for q in remapped}
    assert by_label["home"].decimal_odd == 2.10
    assert by_label["away"].decimal_odd == 3.50


def test_remap_does_not_flip_totals_on_swap():
    # Totals don't belong to a side; swap must NOT touch over/under.
    ref = event_key("Nigeria", "Senegal", NOW)
    soft_quotes = [
        _q(Book.UNIBET_BE, MarketType.TOTALS, "over", 2.05, "Senegal", "Nigeria", line=2.5),
        _q(Book.UNIBET_BE, MarketType.TOTALS, "under", 1.85, "Senegal", "Nigeria", line=2.5),
    ]
    remapped = remap_to_reference(soft_quotes, [ref])
    by_label = {q.outcome.label: q for q in remapped}
    assert by_label["over"].decimal_odd == 2.05
    assert by_label["under"].decimal_odd == 1.85
