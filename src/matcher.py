from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Iterable, Optional

from rapidfuzz import fuzz

from .models import Event
from .teams_i18n import COUNTRY_ALIASES


_TEAM_NOISE = re.compile(
    r"\b("
    r"fc|cf|sc|ac|cd|ss|us|sv|sk|club|cp|de|the|"
    r"rsc|rfc|rcs|rwdm|kv|kvc|kvk|kaa|kfc|kas|kvm|kvo|kmsk|kv|royal|r|k|"
    r"fk|nk|bk|rb|ssc|asse|ase"
    r")\b",
    re.IGNORECASE,
)


# A senior men's side must NEVER be matched against the women's / youth /
# reserve side of the same club: they share the base name but play completely
# different games, so pairing them produces phantom value bets and surebets.
# We pull the class marker out of the name into a canonical tag that survives
# normalisation, and require the class to match before comparing the base name.
_CLASS_RULES = (
    ("xwomen", re.compile(
        r"\b(women|womens|woman|ladies|dames|frauen|feminines?|feminine|feminin|fem|w)\b",
        re.IGNORECASE)),
    ("xyouth", re.compile(r"\b(u1[5-9]|u2[0-3]|youth|junior|jr)\b", re.IGNORECASE)),
    ("xreserve", re.compile(r"\b(reserves?|ii|b)\b", re.IGNORECASE)),
)
_CLASS_TAGS = ("xwomen", "xyouth", "xreserve")


def _extract_class(s: str) -> tuple[str, str]:
    """Return (class_tag, name_without_markers). First rule that hits wins the
    tag; every matched marker is stripped from the base name."""
    cls = ""
    for tag, pat in _CLASS_RULES:
        if pat.search(s):
            if not cls:
                cls = tag
            s = pat.sub(" ", s)
    return cls, s


@lru_cache(maxsize=100_000)
def normalize_team(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    cls, s = _extract_class(s)             # detect class while word boundaries exist
    s = _TEAM_NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = COUNTRY_ALIASES.get(s, s)        # FR/NL/variantes -> canonique EN (international)
    if cls:
        s = f"{s} {cls}".strip()
    return s


@lru_cache(maxsize=100_000)
def team_class(normalized: str) -> str:
    for tag in _CLASS_TAGS:
        if tag in normalized:
            return tag
    return "main"


@lru_cache(maxsize=100_000)
def _strip_class_tag(normalized: str) -> str:
    for tag in _CLASS_TAGS:
        normalized = normalized.replace(tag, " ")
    return re.sub(r"\s+", " ", normalized).strip()


def team_similarity(a: str, b: str) -> float:
    na, nb = normalize_team(a), normalize_team(b)
    # Class gate: women/youth/reserve only match their own kind.
    if team_class(na) != team_class(nb):
        return 0.0
    sa, sb = _strip_class_tag(na), _strip_class_tag(nb)
    return max(
        fuzz.token_set_ratio(sa, sb),
        fuzz.partial_ratio(sa, sb),
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
    ambiguity_margin: float = 4.0,
) -> dict[str, tuple[str, bool]]:
    """Map each candidate (soft-book) event_key onto the best reference
    (Pinnacle) event_key via fuzzy team matching within a time window.

    Returns {candidate_key: (reference_key, swap_required)} for candidates
    that match. `swap_required=True` means the candidate listed home/away
    in the opposite order to the reference, so any outcome labels carried
    by quotes from that candidate need to be flipped before they line up
    with the reference frame. Exact string matches are kept as-is and never
    require a swap.
    """
    refs: list[tuple[str, datetime, str, str]] = []
    for k in reference_keys:
        parsed = parse_event_key(k)
        if parsed is not None:
            refs.append((k, *parsed))

    tol = timedelta(minutes=time_tolerance_minutes)
    mapping: dict[str, tuple[str, bool]] = {}
    ref_keys = {r[0] for r in refs}

    for ck in candidate_keys:
        if ck in ref_keys:
            mapping[ck] = (ck, False)
            continue
        parsed = parse_event_key(ck)
        if parsed is None:
            continue
        c_start, c_home, c_away = parsed
        best_key: Optional[str] = None
        best_score = 0.0
        best_swap = False
        second_score = 0.0     # best score of a *different* reference event
        for rk, r_start, r_home, r_away in refs:
            if abs(r_start - c_start) > tol:
                continue
            s_direct = (team_similarity(c_home, r_home) + team_similarity(c_away, r_away)) / 2
            s_swap = (team_similarity(c_home, r_away) + team_similarity(c_away, r_home)) / 2
            if s_direct >= s_swap:
                score = s_direct
                swap = False
            else:
                score = s_swap
                swap = True
            if score > best_score:
                second_score = best_score
                best_score = score
                best_key = rk
                best_swap = swap
            elif score > second_score:
                second_score = score
        # Ambiguity guard: if a second reference event scores almost as well,
        # the candidate can't be pinned to one match (e.g. several same-day
        # "Tallinn" sides) — skip it rather than guess and emit a phantom.
        if second_score >= min_score and (best_score - second_score) < ambiguity_margin:
            continue
        if best_key is not None and best_score >= min_score:
            mapping[ck] = (best_key, best_swap)
    return mapping


def match_event(
    target: Event,
    candidates: Iterable[Event],
    *,
    time_tolerance_minutes: int = 10,
    min_score: float = 85.0,
    ambiguity_margin: float = 4.0,
) -> Optional[Event]:
    tol = timedelta(minutes=time_tolerance_minutes)
    best: Optional[Event] = None
    best_score = 0.0
    second_score = 0.0
    for c in candidates:
        if abs(c.start_time - target.start_time) > tol:
            continue
        s_direct = (team_similarity(target.home, c.home) + team_similarity(target.away, c.away)) / 2
        s_swap = (team_similarity(target.home, c.away) + team_similarity(target.away, c.home)) / 2
        score = max(s_direct, s_swap)
        if score > best_score:
            second_score = best_score
            best_score = score
            best = c
        elif score > second_score:
            second_score = score
    if best_score < min_score:
        return None
    # Ambiguity guard: two candidates almost equally good -> refuse to guess.
    if second_score >= min_score and (best_score - second_score) < ambiguity_margin:
        return None
    return best
