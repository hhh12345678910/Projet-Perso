from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from .models import Book, MarketType, OddQuote, Outcome, ValueBet


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_key   TEXT PRIMARY KEY,
    sport       TEXT NOT NULL,
    league      TEXT NOT NULL,
    home        TEXT NOT NULL,
    away        TEXT NOT NULL,
    start_time  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key       TEXT NOT NULL,
    book            TEXT NOT NULL,
    market          TEXT NOT NULL,
    outcome_label   TEXT NOT NULL,
    line            REAL,
    decimal_odd     REAL NOT NULL,
    fetched_at      TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    FOREIGN KEY (event_key) REFERENCES events(event_key)
);

CREATE INDEX IF NOT EXISTS idx_quotes_event ON quotes(event_key);
CREATE INDEX IF NOT EXISTS idx_quotes_fetched ON quotes(fetched_at);

CREATE TABLE IF NOT EXISTS value_bets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key       TEXT NOT NULL,
    book            TEXT NOT NULL,
    market          TEXT NOT NULL,
    outcome_label   TEXT NOT NULL,
    line            REAL,
    odd_taken       REAL NOT NULL,
    fair_prob       REAL NOT NULL,
    fair_odd        REAL NOT NULL,
    ev_pct          REAL NOT NULL,
    kelly_pct       REAL NOT NULL,
    stake           REAL,
    detected_at     TEXT NOT NULL,
    placed          INTEGER DEFAULT 0,
    result          TEXT,
    pnl             REAL
);

CREATE INDEX IF NOT EXISTS idx_vb_event ON value_bets(event_key);
CREATE INDEX IF NOT EXISTS idx_vb_detected ON value_bets(detected_at);

CREATE TABLE IF NOT EXISTS clv_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    value_bet_id    INTEGER NOT NULL,
    snapshot_at     TEXT NOT NULL,
    closing         INTEGER DEFAULT 0,
    pinnacle_odd    REAL NOT NULL,
    pinnacle_prob   REAL NOT NULL,
    FOREIGN KEY (value_bet_id) REFERENCES value_bets(id)
);
"""


class Storage:
    def __init__(self, path: str | Path = "data/valuebet.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_event(
        self, event_key: str, sport: str, league: str, home: str, away: str, start_time: datetime
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO events(event_key, sport, league, home, away, start_time) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (event_key, sport, league, home, away, start_time.isoformat()),
            )

    def insert_quote(self, q: OddQuote) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO quotes(event_key, book, market, outcome_label, line, decimal_odd, "
                "fetched_at, source_event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    q.event_key,
                    q.book.value,
                    q.market.value,
                    q.outcome.label,
                    q.outcome.line,
                    q.decimal_odd,
                    q.fetched_at.isoformat(),
                    q.source_event_id,
                ),
            )

    def insert_value_bet(self, vb: ValueBet, stake: Optional[float] = None) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO value_bets(event_key, book, market, outcome_label, line, odd_taken, "
                "fair_prob, fair_odd, ev_pct, kelly_pct, stake, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    vb.event_key,
                    vb.book.value,
                    vb.market.value,
                    vb.outcome.label,
                    vb.outcome.line,
                    vb.odd_taken,
                    vb.fair_prob,
                    vb.fair_odd,
                    vb.ev_pct,
                    vb.kelly_stake_pct,
                    stake,
                    vb.detected_at.isoformat(),
                ),
            )
            return int(cur.lastrowid or 0)

    def insert_clv_snapshot(
        self, value_bet_id: int, pinnacle_odd: float, pinnacle_prob: float,
        snapshot_at: datetime, closing: bool = False,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO clv_snapshots(value_bet_id, snapshot_at, closing, pinnacle_odd, "
                "pinnacle_prob) VALUES (?, ?, ?, ?, ?)",
                (value_bet_id, snapshot_at.isoformat(), 1 if closing else 0, pinnacle_odd, pinnacle_prob),
            )

    def recent_value_bets(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM value_bets ORDER BY detected_at DESC LIMIT ?", (limit,)
            ))
