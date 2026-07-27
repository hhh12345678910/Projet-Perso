"""Layout of the played-bets tracker.

Two processes write this file — bot_listener.py appends a line the moment
Jouer is tapped, `track-update` rebuilds it in full from the database — so the
column list lives here rather than in either of them. Kept free of heavy
imports on purpose: bot_listener is a small always-on service and must not drag
the scrapers in just to log a click.
"""
from __future__ import annotations

import csv
from pathlib import Path

PATH = "data/paris_track.csv"

HEADERS = [
    "Date", "Sport", "Match", "Book", "Marché", "Sélection", "Cote prise",
    "Cote fair (détection)", "EV %", "Clôture juste (dévigée)", "CLV %",
    "Mise fictive", "Score dom.", "Score ext.", "Résultat", "P&L", "Cumul P&L",
    "event_key",
]

# One flat stake on every bet. The tracker exists to compare signals against
# each other; letting the stake vary would mix bet quality with sizing and make
# the P&L column answer neither question cleanly.
STAKE_EUR = 25.0


def append(path: str | Path, row: list) -> None:
    """Append one bet, writing the header first if the file is new."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fresh = not p.exists() or p.stat().st_size == 0
    with p.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if fresh:
            w.writerow(HEADERS)
        w.writerow(row)


def write_all(path: str | Path, rows: list[list]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADERS)
        w.writerows(rows)
