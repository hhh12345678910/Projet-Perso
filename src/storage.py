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

CREATE TABLE IF NOT EXISTS notified_surebets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key       TEXT NOT NULL,
    market          TEXT NOT NULL,
    line            REAL,
    margin_pct      REAL NOT NULL,
    notified_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ns_lookup ON notified_surebets(event_key, market);

CREATE TABLE IF NOT EXISTS notified_value_bets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key       TEXT NOT NULL,
    book            TEXT NOT NULL,
    market          TEXT NOT NULL,
    outcome_label   TEXT NOT NULL,
    line            REAL,
    ev_pct          REAL NOT NULL,
    notified_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nvb_lookup ON notified_value_bets(event_key, book, market);

CREATE TABLE IF NOT EXISTS notified_clv_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    value_bet_id    INTEGER NOT NULL,
    clv_pct         REAL NOT NULL,
    current_pin_odd REAL NOT NULL,
    notified_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nca_vb ON notified_clv_alerts(value_bet_id);

CREATE TABLE IF NOT EXISTS teams (
    normalized_name  TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL
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
        # timeout=10 s : attend jusqu'à 10 s si une autre connexion écrit.
        # WAL activé au premier appel (persisté dans le fichier) : permet les
        # lectures concurrentes pendant les écritures → plus de "database is locked".
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
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
        """Persist a detected value bet. Returns the new row id, or the
        existing row id if the same (event, book, market, outcome, line) tuple
        is already on file — we want one tracking record per opportunity, not
        one per scan that re-surfaced it."""
        existing = self.find_value_bet_id(
            vb.event_key, vb.book.value, vb.market.value, vb.outcome.label, vb.outcome.line
        )
        if existing is not None:
            return existing
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

    def find_value_bet_id(
        self, event_key: str, book: str, market: str, outcome_label: str,
        line: Optional[float],
    ) -> Optional[int]:
        with self._conn() as c:
            if line is None:
                row = c.execute(
                    "SELECT id FROM value_bets WHERE event_key=? AND book=? AND market=? "
                    "AND outcome_label=? AND line IS NULL",
                    (event_key, book, market, outcome_label),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT id FROM value_bets WHERE event_key=? AND book=? AND market=? "
                    "AND outcome_label=? AND line=?",
                    (event_key, book, market, outcome_label, line),
                ).fetchone()
            return int(row["id"]) if row else None

    def find_value_bet_ev(
        self, event_key: str, book: str, market: str, outcome_label: str,
        line: Optional[float],
    ) -> Optional[tuple[int, float]]:
        """Return (id, ev_pct) for an existing bet, or None if not yet tracked."""
        with self._conn() as c:
            if line is None:
                row = c.execute(
                    "SELECT id, ev_pct FROM value_bets WHERE event_key=? AND book=? AND market=? "
                    "AND outcome_label=? AND line IS NULL",
                    (event_key, book, market, outcome_label),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT id, ev_pct FROM value_bets WHERE event_key=? AND book=? AND market=? "
                    "AND outcome_label=? AND line=?",
                    (event_key, book, market, outcome_label, line),
                ).fetchone()
            return (int(row["id"]), float(row["ev_pct"])) if row else None

    def update_value_bet_ev(self, value_bet_id: int, ev_pct: float) -> None:
        """Refresh the stored EV% so the next scan delta-checks against the latest value."""
        with self._conn() as c:
            c.execute(
                "UPDATE value_bets SET ev_pct=? WHERE id=?",
                (ev_pct, value_bet_id),
            )

    def open_value_bets(self) -> list[sqlite3.Row]:
        """Bets that don't have a closing snapshot yet."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT vb.* FROM value_bets vb "
                "LEFT JOIN clv_snapshots cs ON cs.value_bet_id = vb.id AND cs.closing = 1 "
                "WHERE cs.id IS NULL ORDER BY vb.detected_at"
            ))

    def closing_snapshot(self, value_bet_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM clv_snapshots WHERE value_bet_id=? AND closing=1 "
                "ORDER BY snapshot_at DESC LIMIT 1",
                (value_bet_id,),
            ).fetchone()

    @staticmethod
    def _event_key_like(event_key: str) -> str:
        """Build a LIKE pattern that matches any event_key with the same date
        and teams regardless of the exact kick-off minute.

        event_key format: "YYYYMMDDHHMM::home__vs__away"
        Pattern produced:  "YYYYMMDD%::home__vs__away"

        Pinnacle sometimes adjusts a match's start time by a few minutes
        between scans (DST corrections, late schedule changes). When that
        happens the full key changes but the date + teams stay identical, so
        exact-key dedup would miss the stored notification and fire again.
        Matching on date+teams makes dedup robust to minor time drifts."""
        if "::" not in event_key:
            return event_key  # malformed key — fall back to exact match
        date_prefix = event_key[:8]          # "YYYYMMDD"
        teams_part = event_key.split("::", 1)[1]  # "home__vs__away"
        return f"{date_prefix}%::{teams_part}"

    def surebet_already_notified(
        self, event_key: str, market: str, line: Optional[float],
        current_margin_pct: float = 0.0, roi_delta_pct: float = 0.5,
    ) -> bool:
        """Return True (skip) only when the surebet was already notified AND
        its margin hasn't moved by more than roi_delta_pct since the last alert.
        A ROI change >= roi_delta_pct triggers a fresh notification so the user
        sees the updated opportunity without needing to disable dedup entirely."""
        like_key = self._event_key_like(event_key)
        with self._conn() as c:
            if line is None:
                row = c.execute(
                    "SELECT margin_pct FROM notified_surebets WHERE event_key LIKE ? AND market=? "
                    "AND line IS NULL ORDER BY notified_at DESC LIMIT 1",
                    (like_key, market),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT margin_pct FROM notified_surebets WHERE event_key LIKE ? AND market=? "
                    "AND line=? ORDER BY notified_at DESC LIMIT 1",
                    (like_key, market, line),
                ).fetchone()
            if row is None:
                return False
            last_margin_pct = row[0]
            return abs(current_margin_pct - last_margin_pct) < roi_delta_pct

    def mark_surebet_notified(
        self, event_key: str, market: str, line: Optional[float],
        margin_pct: float, notified_at: datetime,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO notified_surebets(event_key, market, line, margin_pct, notified_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_key, market, line, margin_pct, notified_at.isoformat()),
            )

    def clv_alert_already_notified(self, value_bet_id: int) -> bool:
        """True if a pre-kickoff CLV alert was already sent for this value bet row."""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM notified_clv_alerts WHERE value_bet_id=? LIMIT 1",
                (value_bet_id,),
            ).fetchone()
            return row is not None

    def mark_clv_alert_notified(
        self, value_bet_id: int, clv_pct: float,
        current_pin_odd: float, notified_at: datetime,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO notified_clv_alerts(value_bet_id, clv_pct, current_pin_odd, notified_at) "
                "VALUES (?, ?, ?, ?)",
                (value_bet_id, clv_pct, current_pin_odd, notified_at.isoformat()),
            )

    def value_bet_already_notified(
        self, event_key: str, book: str, market: str, outcome_label: str,
        line: Optional[float],
        current_ev_pct: float = 0.0, ev_delta_pct: float = 1.0,
    ) -> bool:
        """Return True (skip) when this value bet was already notified AND its
        EV hasn't moved by ev_delta_pct since the last alert."""
        like_key = self._event_key_like(event_key)
        with self._conn() as c:
            if line is None:
                row = c.execute(
                    "SELECT ev_pct FROM notified_value_bets "
                    "WHERE event_key LIKE ? AND book=? AND market=? AND outcome_label=? AND line IS NULL "
                    "ORDER BY notified_at DESC LIMIT 1",
                    (like_key, book, market, outcome_label),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT ev_pct FROM notified_value_bets "
                    "WHERE event_key LIKE ? AND book=? AND market=? AND outcome_label=? AND line=? "
                    "ORDER BY notified_at DESC LIMIT 1",
                    (like_key, book, market, outcome_label, line),
                ).fetchone()
            if row is None:
                return False
            return abs(current_ev_pct - row[0]) < ev_delta_pct

    def mark_value_bet_notified(
        self, event_key: str, book: str, market: str, outcome_label: str,
        line: Optional[float], ev_pct: float, notified_at: datetime,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO notified_value_bets"
                "(event_key, book, market, outcome_label, line, ev_pct, notified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_key, book, market, outcome_label, line, ev_pct, notified_at.isoformat()),
            )

    def record_team(self, normalized_name: str, display_name: str) -> None:
        """Persist (or refresh) the mapping from the matcher's space-stripped
        team key to the original human-readable name a scraper just saw.
        UPSERT semantics — the most recent scraper to see the team wins."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO teams(normalized_name, display_name, last_seen_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(normalized_name) DO UPDATE SET "
                "  display_name = excluded.display_name, "
                "  last_seen_at = excluded.last_seen_at",
                (normalized_name, display_name, datetime.utcnow().isoformat()),
            )

    def get_team(self, normalized_name: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM teams WHERE normalized_name=?",
                (normalized_name,),
            ).fetchone()

    def all_teams(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute("SELECT * FROM teams"))

    def latest_pinnacle_quote_before(
        self, event_key: str, market: str, outcome_label: str,
        line: Optional[float], before: datetime,
    ) -> Optional[sqlite3.Row]:
        """Return the most recent Pinnacle quote stored before `before` for
        this exact (event, market, outcome, line) tuple. close-lines uses
        this to capture the closing price from our own historical capture
        instead of asking Pinnacle's live API — by kickoff the live market
        is already gone, the only place the real closing line still exists
        is in our quotes table."""
        with self._conn() as c:
            params: list = [event_key, market, outcome_label, before.isoformat()]
            line_clause = "line IS NULL" if line is None else "line = ?"
            if line is not None:
                # Splice the line value just after outcome_label in the params list.
                params = [event_key, market, outcome_label, line, before.isoformat()]
            sql = (
                f"SELECT * FROM quotes "
                f"WHERE book = 'pinnacle' "
                f"  AND event_key = ? "
                f"  AND market = ? "
                f"  AND outcome_label = ? "
                f"  AND {line_clause} "
                f"  AND fetched_at < ? "
                f"ORDER BY fetched_at DESC "
                f"LIMIT 1"
            )
            return c.execute(sql, params).fetchone()

    def all_closed_bets(self) -> list[sqlite3.Row]:
        """Bets joined with their closing snapshot, ready for CLV aggregation."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT vb.*, cs.pinnacle_odd AS closing_odd, cs.snapshot_at AS closed_at "
                "FROM value_bets vb "
                "JOIN clv_snapshots cs ON cs.value_bet_id = vb.id AND cs.closing = 1 "
                "ORDER BY vb.detected_at DESC"
            ))

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
