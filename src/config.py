from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScanConfig:
    sport: str = "soccer"
    min_ev_pct: float = 2.0
    max_ev_pct: float = 100.0    # detection cap: keep huge "error-looking" edges
                                 # (the premium channel surfaces these on purpose);
                                 # above 100% is almost always a parsing/line bug
    min_minutes_to_kickoff: int = 30
    devig_method: str = "shin"
    kelly_fraction: float = 0.25
    bankroll: float = 1000.0
    db_path: str = "data/valuebet.db"
