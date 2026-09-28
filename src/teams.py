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

⚠️ `display` rend le DERNIER nom vu sous une clé d'ÉQUIPE, tous books
confondus — et une clé d'équipe confond des clubs distincts (« Club Olimpia »
et « CD Olimpia » donnent tous deux `olimpia`). Une alerte doit montrer les
noms que le BOOK de l'alerte a écrits pour CE match (demande du 27/09) :
c'est `noms_du_match`, indexé par la clé d'ÉVÉNEMENT du book (équipes et
minute du coup d'envoi), qui ne confond rien. Voir `record_pair`.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, TYPE_CHECKING

from .matcher import event_key, normalize_team

if TYPE_CHECKING:
    from .storage import Storage


_DISPLAY: dict[str, str] = {}
_STORAGE: Optional["Storage"] = None

#: (book, clé d'événement du book) → (domicile, extérieur) tels que CE book
#: les a écrits pour CE match. En mémoire seulement : les alertes qui s'en
#: servent (value bet, marché en retard) partent du cycle même où le scraper
#: les a lus. Purgé des matchs passés depuis `GARDE_NOMS_MATCH_H`.
_NOMS_MATCH: dict[tuple[str, str], tuple[str, str]] = {}
_VERROU = threading.Lock()
MAX_NOMS_MATCH = 200_000
GARDE_NOMS_MATCH_H = 36


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


def _book_id(book) -> str:
    """`Book.LADBROKES_BE` ou "ladbrokes_be" → "ladbrokes_be"."""
    return str(getattr(book, "value", book) or "")


def record_pair(home: str | None, away: str | None, book=None,
                start: "datetime | None" = None,
                noms_affiches: "tuple[str, str] | None" = None) -> None:
    """Convenience used at the scraper sites where home and away both
    appear together — one call instead of two.

    `book` et `start` : le book qui écrit ces noms, et le coup d'envoi qu'il
    annonce — assez pour retrouver ses noms À LUI pour CE match
    (`noms_du_match`), sous la clé même de ses cotes (`event_key(home, away,
    start)`, calculée comme par le scraper).

    `noms_affiches` : quand le book rapproche sur une langue (l'anglais de
    Vivatbet, celle de Pinnacle) mais AFFICHE dans une autre (le français du
    site), les noms à montrer dans l'alerte — ceux qu'on retrouve sur le
    site. La clé, elle, reste calculée sur `home`/`away`."""
    if home:
        record(home)
    if away:
        record(away)
    if not (home and away and book is not None and start is not None):
        return
    try:
        cle = event_key(home, away, start)
    except Exception:                                           # noqa: BLE001
        return
    affiches = noms_affiches if noms_affiches and all(noms_affiches) else (home, away)
    _NOMS_MATCH[(_book_id(book), cle)] = affiches
    if len(_NOMS_MATCH) > MAX_NOMS_MATCH:
        _purger_noms_match()


def noms_du_match(book, cle: str | None) -> "tuple[str, str] | None":
    """(domicile, extérieur) tels que `book` les a écrits pour le match de
    clé `cle` (SA clé d'événement), ou None s'il ne l'a pas enregistré."""
    if not cle:
        return None
    return _NOMS_MATCH.get((_book_id(book), cle))


def _purger_noms_match(maintenant: "datetime | None" = None) -> None:
    """Oublier les matchs commencés depuis plus de `GARDE_NOMS_MATCH_H`. La
    clé commence par la minute du coup d'envoi (AAAAMMJJHHMM) : une
    comparaison de chaînes suffit. Si tout est récent, on repart de zéro —
    le prochain cycle réécrit ce qui sert."""
    seuil = ((maintenant or datetime.now(timezone.utc))
             - timedelta(hours=GARDE_NOMS_MATCH_H)).strftime("%Y%m%d%H%M")
    with _VERROU:
        for k in [k for k in list(_NOMS_MATCH) if k[1][:12] < seuil]:
            _NOMS_MATCH.pop(k, None)
        if len(_NOMS_MATCH) > MAX_NOMS_MATCH:
            _NOMS_MATCH.clear()


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
    _NOMS_MATCH.clear()
    _STORAGE = None
