from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScanConfig:
    sport: str = "soccer"
    min_ev_pct: float = 2.0
    max_ev_pct: float = 1000.0   # detection cap: keep huge "error-looking" edges
                                 # (the premium/critical channels surface these on
                                 # purpose); above 1000% is almost certainly a
                                 # parsing/line bug and stays filtered out
    min_minutes_to_kickoff: int = 30
    devig_method: str = "shin"
    kelly_fraction: float = 0.25
    bankroll: float = 1000.0
    db_path: str = "data/valuebet.db"
