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


def test_a_locked_database_never_breaks_a_scrape(tmp_path):
    """Le registre de noms sert à l'affichage, rien de plus. Il est pourtant
    appelé depuis l'intérieur des scrapers, en plein parsing : une base
    verrouillée y levait une exception et faisait tomber le fetch entier.

    Mesuré sur onze heures de journal : 695 récupérations perdues pour cette
    seule raison, dont 92 sur Pinnacle — 34 Mo téléchargés puis jetés à chaque
    fois, avec en prime un « Pinnacle skipped: database is locked » qui
    ressemble à un blocage réseau."""
    import sqlite3
    from src import teams

    class _LockedStorage:
        def all_teams(self):
            return []
        def record_team(self, key, name):
            raise sqlite3.OperationalError("database is locked")

    teams.init(_LockedStorage())
    try:
        teams.record("Manchester City")       # ne doit pas lever
        # Le nom reste utilisable immédiatement : le cache mémoire a été
        # renseigné avant la tentative d'écriture.
        assert teams.display("manchestercity") == "Manchester City"
    finally:
        teams.init(None)


def test_a_working_database_still_gets_written(tmp_path):
    """Le garde-fou précédent ne doit pas avaler les écritures normales."""
    from src import teams

    written = []

    class _Storage:
        def all_teams(self):
            return []
        def record_team(self, key, name):
            written.append((key, name))

    teams.init(_Storage())
    try:
        teams.record("Real Madrid")
        assert written == [("realmadrid", "Real Madrid")]
        teams.record("Real Madrid")           # idempotent
        assert len(written) == 1
    finally:
        teams.init(None)
