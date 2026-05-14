from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from typing import Iterable, Optional

from rapidfuzz import fuzz

from .models import Event


_TEAM_NOISE = re.compile(
    r"\b("
    r"fc|cf|sc|ac|cd|ss|us|sv|sk|club|cp|de|the|"
    r"rsc|rfc|rcs|rwdm|kv|kvc|kvk|kaa|kfc|kas|kvm|kvo|kmsk|kv|royal|r|k|"
    r"fk|nk|bk|rb|ssc|asse|ase|"
    r"u17|u18|u19|u20|u21|u23|"
    r"reserves|reserve|ii|b|women|w|fem"
    r")\b",
    re.IGNORECASE,
)


def normalize_team(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = _TEAM_NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def team_similarity(a: str, b: str) -> float:
    return max(
        fuzz.token_set_ratio(normalize_team(a), normalize_team(b)),
        fuzz.partial_ratio(normalize_team(a), normalize_team(b)),
    )


def event_key(home: str, away: str, start: datetime) -> str:
    h = normalize_team(home).replace(" ", "")
    a = normalize_team(away).replace(" ", "")
    return f"{start.strftime('%Y%m%d%H%M')}::{h}__vs__{a}"


def match_event(
    target: Event,
    candidates: Iterable[Event],
    *,
    time_tolerance_minutes: int = 10,
    min_score: float = 85.0,
) -> Optional[Event]:
    tol = timedelta(minutes=time_tolerance_minutes)
    best: Optional[Event] = None
    best_score = 0.0
    for c in candidates:
        if abs(c.start_time - target.start_time) > tol:
            continue
        s_direct = (team_similarity(target.home, c.home) + team_similarity(target.away, c.away)) / 2
        s_swap = (team_similarity(target.home, c.away) + team_similarity(target.away, c.home)) / 2
        score = max(s_direct, s_swap)
        if score > best_score:
            best_score = score
            best = c
    if best_score < min_score:
        return None
    return best
