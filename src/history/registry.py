from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from ..models import Book
from .base import HistoryImporter, SettledBet
from .kambi import build_kambi_importers


class PendingHistoryImporter(HistoryImporter):
    """Placeholder for a book whose bet-history API isn't decoded yet. Never
    `available()`, so `import-history` lists it as "en attente d'un HAR" and
    skips it. Turning it on = writing a real importer and registering it below;
    the framework (storage, normalisation, CLI, P&L report) already works.
    """

    def __init__(self, book: Book, note: str = ""):
        self.book = book
        self.note = note or "API historique non décodée — fournis un HAR pour l'activer."

    def available(self) -> bool:
        return False

    def fetch(self, *, since: Optional[datetime] = None) -> Iterable[SettledBet]:
        return []


# Books that run their own (non-Kambi) platform. Each lights up once its history
# endpoint is decoded from a HAR capture, following the Kambi importer as a model.
_PENDING_BOOKS = [
    Book.BETANO_BE,
    Book.LADBROKES_BE,
    Book.STARCASINO_SPORT,
    Book.NAPOLEON_BE,
    Book.BETCENTER,
    Book.GOLDEN_PALACE,
    Book.MERIDIAN_BE,
    Book.BETFIRST,
]


def all_importers() -> list[HistoryImporter]:
    """Every known importer, decoded (Kambi) first then the pending stubs.
    The CLI runs the `available()` ones and reports the rest as skipped."""
    importers: list[HistoryImporter] = list(build_kambi_importers())
    importers.extend(PendingHistoryImporter(b) for b in _PENDING_BOOKS)
    return importers


def importers_for(books: Optional[list[str]] = None) -> list[HistoryImporter]:
    """Filter importers to the given book values (e.g. ['unibet_be']). None or
    empty returns them all."""
    everything = all_importers()
    if not books:
        return everything
    wanted = {b.lower() for b in books}
    return [imp for imp in everything if imp.book.value.lower() in wanted]
