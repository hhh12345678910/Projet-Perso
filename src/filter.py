from __future__ import annotations

import re


# Patterns whose presence in either participant name (or the explicit league
# label) makes the event unfit for value betting against Pinnacle's fair line.
# Youth + reserve + eSoccer events all share two problems: the soft books mis-
# label them as the senior team, and Pinnacle either doesn't price them or
# carries a wide trader margin that produces phantom EV.
_NOISE_TOKEN_RE = re.compile(
    r"\b("
    r"u1[0-9]|u2[0-3]|"                 # U10..U23 academy sides
    r"reserves?|reserve|"
    r"e[\-\s]?soccer|e[\-\s]?football|"  # FIFA video-game tournaments
    r"esports?|"
    r"cyber|"                            # Sportradar's 'Cyber Live Arena' streams
    r"virtual"
    r")\b",
    re.IGNORECASE,
)

# A parenthesised single-word handle right after a club name is the standard
# eSoccer convention for the player avatar — e.g. 'Tottenham Hotspur (Beckham)'.
# It always denotes a video-game match, not a real fixture.
_BRACKET_HANDLE_RE = re.compile(r"\([A-Za-z][A-Za-z0-9 .\-']{1,30}\)")


def is_noise_event(*texts: str) -> bool:
    """Return True if any of the provided strings (team names, league name,
    competition label) match a known low-quality / unrealisable pattern. The
    scrapers call this just before they emit OddQuotes so the rest of the
    pipeline never sees the event at all — much cheaper than trying to clean
    it up after fuzzy-matching has already wired it to a real Pinnacle event."""
    for t in texts:
        if not t:
            continue
        if _NOISE_TOKEN_RE.search(t):
            return True
        if _BRACKET_HANDLE_RE.search(t):
            return True
    return False
