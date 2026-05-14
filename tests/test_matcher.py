from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.matcher import event_key, match_event, normalize_team, team_similarity
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
