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

Scrapers call `record(name, book)` for every original team string they see.
Format helpers call `display(normalised)` to recover the human form.
Both are no-ops if init() was never called or with empty input, so unit
tests on the parsers don't need to wire up a database.

⚠️ `display` rend le DERNIER nom vu, tous books confondus — les scrapers
tournent en parallèle, donc n'importe lequel. Une alerte Ladbrokes doit
montrer les noms de Ladbrokes : c'est `display_for_book`, alimenté par le
même `record` quand le scraper dit quel book il est (demande du 27/09).
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from .matcher import normalize_team

if TYPE_CHECKING:
    from .storage import Storage


_DISPLAY: dict[str, str] = {}
#: (book, clé normalisée) → le nom tel que CE book l'écrit.
_PAR_BOOK: dict[tuple[str, str], str] = {}
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
    try:
        for row in storage.all_team_names_by_book():
            _PAR_BOOK[(row["book"], row["normalized_name"])] = row["display_name"]
    except Exception:                                           # noqa: BLE001
        pass


def _book_id(book) -> str:
    """`Book.LADBROKES_BE` ou "ladbrokes_be" → "ladbrokes_be"."""
    return str(getattr(book, "value", book) or "")


def record(name: str, book=None) -> None:
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
    et en attendant l'affichage est déjà correct.

    `book` : le book qui écrit ce nom. Il alimente en plus le registre PAR
    BOOK (`display_for_book`), avec la même discrétion en cas de base
    verrouillée."""
    if not name:
        return
    key = _normalised_key(name)
    if not key:
        return
    b = _book_id(book)
    if b and _PAR_BOOK.get((b, key)) != name:
        _PAR_BOOK[(b, key)] = name
        if _STORAGE is not None:
            try:
                _STORAGE.record_team_for_book(b, key, name)
            except Exception:                                   # noqa: BLE001
                pass
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


def record_pair(home: str | None, away: str | None, book=None) -> None:
    """Convenience used at the scraper sites where home and away both
    appear together — one call instead of two."""
    if home:
        record(home, book)
    if away:
        record(away, book)


def display_for_book(book, normalised: str) -> Optional[str]:
    """Le nom tel que `book` l'écrit, ou None si ce book ne l'a jamais
    enregistré (l'appelant retombe alors sur `display`)."""
    if not normalised:
        return None
    b = _book_id(book)
    if not b:
        return None
    hit = _PAR_BOOK.get((b, normalised))
    if hit:
        return hit
    if _STORAGE is not None:
        try:
            row = _STORAGE.get_team_for_book(b, normalised)
        except Exception:                                       # noqa: BLE001
            row = None
        if row:
            _PAR_BOOK[(b, normalised)] = row["display_name"]
            return row["display_name"]
    return None


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
    _PAR_BOOK.clear()
    _STORAGE = None
