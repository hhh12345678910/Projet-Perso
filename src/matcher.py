from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
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


def parse_event_key(key: str) -> Optional[tuple[datetime, str, str]]:
    """Inverse of event_key(): recover (start_time_utc, home_norm, away_norm).

    Names are already normalised (and space-stripped); start time is to the minute.
    """
    try:
        time_part, rest = key.split("::", 1)
        home, away = rest.split("__vs__", 1)
    except ValueError:
        return None
    try:
        start = datetime.strptime(time_part, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return start, home, away


def reconcile_event_keys(
    reference_keys: Iterable[str],
    candidate_keys: Iterable[str],
    *,
    time_tolerance_minutes: int = 10,
    min_score: float = 85.0,
) -> dict[str, str]:
    """Map each candidate (soft-book) event_key onto the best reference
    (Pinnacle) event_key via fuzzy team matching within a time window.

    Returns {candidate_key: reference_key} for candidates that match. Exact
    string matches are kept as-is.
    """
    refs: list[tuple[str, datetime, str, str]] = []
    for k in reference_keys:
        parsed = parse_event_key(k)
        if parsed is not None:
            refs.append((k, *parsed))

    tol = timedelta(minutes=time_tolerance_minutes)
    mapping: dict[str, str] = {}
    ref_keys = {r[0] for r in refs}

    for ck in candidate_keys:
        if ck in ref_keys:
            mapping[ck] = ck
            continue
        parsed = parse_event_key(ck)
        if parsed is None:
            continue
        c_start, c_home, c_away = parsed
        best_key: Optional[str] = None
        best_score = 0.0
        for rk, r_start, r_home, r_away in refs:
            if abs(r_start - c_start) > tol:
                continue
            s_direct = (team_similarity(c_home, r_home) + team_similarity(c_away, r_away)) / 2
            s_swap = (team_similarity(c_home, r_away) + team_similarity(c_away, r_home)) / 2
            score = max(s_direct, s_swap)
            if score > best_score:
                best_score = score
                best_key = rk
        if best_key is not None and best_score >= min_score:
            mapping[ck] = best_key
    return mapping


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
