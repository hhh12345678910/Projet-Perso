from __future__ import annotations

from pathlib import Path

import pytest

from src.storage import Storage
from src import teams


@pytest.fixture(autouse=True)
def _reset_registry():
    """Every test starts from an empty in-memory cache so order doesn't matter."""
    teams.clear_cache()
    yield
    teams.clear_cache()


def test_display_falls_back_to_capitalised_when_not_recorded():
    assert teams.display("manchestercity") == "Manchestercity"
    assert teams.display("") == ""


def test_record_then_display_returns_original_name():
    teams.record("Manchester City")
    assert teams.display("manchestercity") == "Manchester City"


def test_record_normalises_lookup_key_consistently():
    # Subtle different inputs should all resolve to the same display name.
    teams.record("Real Madrid")
    assert teams.display("realmadrid") == "Real Madrid"


def test_record_overwrites_with_most_recent_value():
    teams.record("Manchester City")
    teams.record("Manchester City FC")
    # Both normalise to "manchestercity"; later record wins so we can correct
    # a previously-recorded ugly variant.
    assert teams.display("manchestercity") == "Manchester City FC"


def test_record_empty_and_whitespace_inputs_are_safe():
    teams.record("")
    teams.record("   ")
    assert teams.display("") == ""


def test_record_pair_records_both_sides():
    teams.record_pair("Paris Saint-Germain", "Arsenal FC")
    assert teams.display("parissaintgermain") == "Paris Saint-Germain"
    assert teams.display("arsenal") == "Arsenal FC"


def test_storage_round_trip_persists_across_init(tmp_path: Path):
    db = tmp_path / "test.db"
    storage = Storage(db)
    teams.init(storage)
    teams.record("Manchester City")
    # Pretend the process restarts: clear the cache and re-init with the same DB.
    teams.clear_cache()
    teams.init(Storage(db))
    assert teams.display("manchestercity") == "Manchester City"


def test_init_with_none_keeps_in_memory_only():
    # No storage wired — record/display still work, just don't persist.
    teams.init(None)
    teams.record("Liverpool")
    assert teams.display("liverpool") == "Liverpool"
