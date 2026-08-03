"""Registry of team display names.

The matcher's event_key strips spaces and lowercases everything so that
"Manchester City" and "manchester city" both collapse to "manchestercity" —
great for cross-book matching, terrible for alert readability. This module
keeps a side index of `normalised_name -> first-seen original` so the
Telegram formatter can show "Manchester City" again instead of the
title-cased blob "Manchestercity".

Two-tier cache:
- Process-local dict for O(1) lookups inside a scan cycle.
- Optional SQLite backing (via Storage) so a fresh process can warm its
  cache from previous scans' data — critical if the scan that detected a
  value bet ran hours before the one rendering it.

Scrapers call `record(name)` for every original team string they see.
Format helpers call `display(normalised)` to recover the human form.
Both are no-ops if init() was never called or with empty input, so unit
tests on the parsers don't need to wire up a database.
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from .matcher import normalize_team

if TYPE_CHECKING:
    from .storage import Storage


_DISPLAY: dict[str, str] = {}
_STORAGE: Optional["Storage"] = None


def _normalised_key(name: str) -> str:
    """Same shape that event_key() uses for team fragments."""
    return normalize_team(name).replace(" ", "")


def init(storage: "Storage | None") -> None:
    """Wire up the persistent backing store and warm the in-memory cache
    from rows already on disk. Safe to call multiple times — subsequent
    calls just refresh the cache."""
    global _STORAGE
    _STORAGE = storage
    if storage is None:
        return
    for row in storage.all_teams():
        _DISPLAY[row["normalized_name"]] = row["display_name"]


def record(name: str) -> None:
    """Remember an original team name so display() can recover it later.
    Idempotent — only writes to disk on a new or changed entry.

    L'écriture ne doit JAMAIS faire échouer l'appelant. Ce registre sert
    uniquement à réafficher « Manchester City » au lieu de
    « manchestercity » : c'est du confort de lecture, rien de plus.

    Or il est appelé depuis l'intérieur des scrapers, en plein parsing. Une
    base verrouillée — par la purge nocturne ou par close-lines, qui tourne
    toutes les heures — levait donc une exception au milieu du parsing et
    faisait tomber le fetch entier. Mesuré sur onze heures de journal :
    695 récupérations perdues pour cette seule raison, dont 92 sur Pinnacle,
    soit 34 Mo téléchargés et jetés à chaque fois. Le message
    « Pinnacle skipped: database is locked » n'a rien d'un blocage réseau,
    et cherchait un problème là où il n'y en avait pas.

    Le nom reste dans le cache mémoire : la prochaine occasion le réécrira,
    et en attendant l'affichage est déjà correct."""
    if not name:
        return
    key = _normalised_key(name)
    if not key:
        return
    previous = _DISPLAY.get(key)
    if previous == name:
        return
    _DISPLAY[key] = name
    if _STORAGE is not None:
        try:
            _STORAGE.record_team(key, name)
        except Exception:                                       # noqa: BLE001
            # Volontairement muet : la base est momentanément prise, le nom
            # est déjà en mémoire, et signaler chaque nom manqué remplirait
            # le journal pendant les purges sans rien apprendre.
            pass


def record_pair(home: str | None, away: str | None) -> None:
    """Convenience used at the scraper sites where home and away both
    appear together — one call instead of two."""
    if home:
        record(home)
    if away:
        record(away)


def display(normalised: str) -> str:
    """Return the human-readable display name for a normalised team key.
    Falls back to a title-cased version of the key when nothing is on file
    (compound names like 'manchestercity' will still look ugly until a
    scraper records the original)."""
    if not normalised:
        return ""
    hit = _DISPLAY.get(normalised)
    if hit:
        return hit
    # Cold-cache miss: try the DB once and stash the result.
    if _STORAGE is not None:
        row = _STORAGE.get_team(normalised)
        if row:
            _DISPLAY[normalised] = row["display_name"]
            return row["display_name"]
    return normalised.capitalize()


def clear_cache() -> None:
    """Test helper — drops the in-memory cache so a fresh test starts clean."""
    global _STORAGE
    _DISPLAY.clear()
    _STORAGE = None
