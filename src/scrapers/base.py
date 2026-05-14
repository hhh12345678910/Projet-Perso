from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from ..models import Book, Event, OddQuote


class Scraper(ABC):
    book: Book

    @abstractmethod
    def fetch_events(self, sport: str) -> Iterable[Event]:
        ...

    @abstractmethod
    def fetch_quotes(self, event: Event) -> Iterable[OddQuote]:
        ...
