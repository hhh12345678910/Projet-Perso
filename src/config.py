from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off", "non")


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

    # Value bets on events that have already kicked off. Off by default: the
    # fair line comes from Pinnacle's PREMATCH feed (the scraper skips isLive
    # matchups), so once a match starts there is no live reference and the
    # comparison is against a price frozen at kick-off. Measured over a week,
    # those detections showed +20.5% CLV against +7.6% prematch — not an edge,
    # a stale denominator. They reached no alert channel anyway (premium and
    # critical are prematch-only, the main chat caps at 8% EV), so switching
    # this off changes no alert; it only stops them polluting the statistics.
    #
    # Scope: value bets ONLY. Surebets and middles compare soft books against
    # each other and need no sharp reference, so they keep running live — as
    # does quote storage, and therefore closing-line capture and CLV.
    #
    # Re-enable with VALUEBET_SCAN_LIVE=1 in .env, then restart the daemon.
    scan_live_value_bets: bool = field(
        default_factory=lambda: _env_flag("VALUEBET_SCAN_LIVE", default=False)
    )
