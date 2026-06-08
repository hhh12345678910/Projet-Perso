from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.matcher import (
    event_key,
    match_event,
    normalize_team,
    parse_event_key,
    reconcile_event_keys,
    team_similarity,
)
from src.models import Book, Event


def _ev(home: str, away: str, ts: datetime, book: Book = Book.PINNACLE) -> Event:
    return Event(
        sport="soccer",
        league="Jupiler Pro League",
        home=home,
        away=away,
        start_time=ts,
        source_id="x",
        book=book,
    )


def test_normalize_team_removes_noise_and_accents():
    assert normalize_team("Real Madrid CF") == "real madrid"
    assert normalize_team("Standard de Liège") == "standard liege"
    assert normalize_team("RSC Anderlecht") == "anderlecht"
    assert normalize_team("KV Mechelen") == "mechelen"
    assert normalize_team("R. Charleroi SC") == "charleroi"


def test_team_similarity_handles_variants():
    assert team_similarity("Standard Liège", "Standard de Liege") > 90
    assert team_similarity("Club Brugge KV", "Club Brugge") > 90


def test_match_event_finds_within_time_window():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    a = _ev("Standard Liège", "Anderlecht", t)
    b = _ev("Standard de Liege", "RSC Anderlecht", t + timedelta(minutes=3), book=Book.UNIBET_BE)
    assert match_event(a, [b]) is b


def test_match_event_rejects_outside_time_window():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    a = _ev("Standard Liège", "Anderlecht", t)
    b = _ev("Standard Liège", "Anderlecht", t + timedelta(hours=3), book=Book.UNIBET_BE)
    assert match_event(a, [b]) is None


def test_match_event_handles_home_away_swap():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    a = _ev("Anderlecht", "Club Brugge", t)
    b = _ev("Club Brugge KV", "RSC Anderlecht", t, book=Book.UNIBET_BE)
    assert match_event(a, [b]) is b


def test_event_key_is_deterministic_and_normalised():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    k1 = event_key("Standard Liège", "RSC Anderlecht", t)
    k2 = event_key("standard de liege", "Anderlecht", t)
    assert k1 == k2


def test_parse_event_key_roundtrip():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    k = event_key("Valencia CF", "Rayo Vallecano", t)
    start, home, away = parse_event_key(k)
    assert start == t
    assert home == "valencia"
    assert away == "rayovallecano"
    assert parse_event_key("garbage") is None


def test_reconcile_keeps_exact_match():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    k = event_key("Valencia", "Rayo Vallecano", t)
    assert reconcile_event_keys([k], [k]) == {k: (k, False)}


def test_reconcile_maps_soft_key_within_time_window():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    ref = event_key("Standard Liège", "RSC Anderlecht", t)
    soft = event_key("Standard de Liege", "Anderlecht", t + timedelta(minutes=4))
    assert reconcile_event_keys([ref], [soft]) == {soft: (ref, False)}


def test_reconcile_flags_team_order_swap():
    # Same event, but the soft book lists Senegal as home instead of Nigeria.
    # The matcher should still link the two keys, with the swap flag set.
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    ref = event_key("Nigeria", "Senegal", t)
    soft = event_key("Senegal", "Nigeria", t)
    result = reconcile_event_keys([ref], [soft])
    assert result == {soft: (ref, True)}


def test_reconcile_rejects_outside_time_window():
    t = datetime(2026, 5, 14, 20, 0, tzinfo=timezone.utc)
    ref = event_key("Standard Liège", "Anderlecht", t)
    soft = event_key("Standard Liège", "Anderlecht", t + timedelta(hours=3))
    assert reconcile_event_keys([ref], [soft]) == {}
