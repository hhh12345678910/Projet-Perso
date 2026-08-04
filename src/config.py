from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_env_file(path: str | Path | None = None) -> int:
    """Charge `.env` dans os.environ. Renvoie le nombre de clés posées.

    Le daemon reçoit sa config par `scan-daemon.sh`, qui source `.env` avant de
    lancer Python. Tout ce qui démarre autrement — une commande lancée à la
    main, `doctor`, ou `bot_listener` sous systemd — n'a rien dans son
    environnement et croit le projet non configuré. Ça s'est produit deux fois :
    `doctor` annonçait « Telegram non configuré » sur une installation qui
    marchait, et `/scan` se désactivait tout seul en n'acceptant aucun chat.

    L'environnement existant gagne (`setdefault`) : un override explicite passé
    au service reste prioritaire sur le fichier.
    """
    env_file = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return 0
    n = 0
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if v[:1] == v[-1:] and v[:1] in ("'", '"') and len(v) >= 2:
            v = v[1:-1]
        if k not in os.environ:
            n += 1
        os.environ.setdefault(k, v)
    return n


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
