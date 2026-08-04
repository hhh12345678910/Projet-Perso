"""La commande /scan : quels paris elle retient, et comment elle les rend.

La sélection est la seule partie qui porte de la logique, et elle est pure —
elle reçoit des lignes, un ensemble de marchés joués et une horloge. Le reste
(getUpdates, sendMessage) n'est que de la plomberie.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot_listener import _channel_marker, format_scan, select_playable
from src.alerter import TelegramConfig


NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _cfg(**kw) -> TelegramConfig:
    base = dict(bot_token="t", chat_id="c", premium_chat_id="prem",
                critical_chat_id="crit", min_minutes_to_kickoff=15)
    base.update(kw)
    return TelegramConfig(**base)


def _row(*, ev=12.0, odd=2.40, book="unibet_be", market="h2h", outcome="home",
         line=None, hours=3.0, home="Anderlecht", away="Club Brugge",
         sport="soccer"):
    """Une ligne value_bets telle que la requête la renvoie (sqlite3.Row-like)."""
    start = NOW + timedelta(hours=hours)
    return {
        "event_key": f"{start.strftime('%Y%m%d%H%M')}::"
                     f"{home.lower().replace(' ', '')}__vs__{away.lower().replace(' ', '')}",
        "book": book, "market": market, "outcome_label": outcome, "line": line,
        "odd_taken": odd, "ev_pct": ev, "home": home, "away": away, "sport": sport,
    }


def _sel(rows, played=frozenset(), cfg=None):
    return select_playable(rows, set(played), cfg or _cfg(), NOW)


# --------------------------------------------------------------- filtres ----

def test_played_market_is_excluded_including_its_other_outcomes():
    """Le cœur de la demande : ce qui a été joué ne réapparaît pas.

    Et pas seulement la sélection jouée — tout le marché, comme pour les
    alertes : avoir pris le 1 d'un 1X2 rend le X et le 2 sans objet."""
    home, draw = _row(outcome="home"), _row(outcome="draw")
    assert len(_sel([home, draw])) == 2          # rien de joué : les deux sortent
    played = {f"{home['event_key']}|h2h|None"}
    assert _sel([home, draw], played) == []      # un clic sur le 1 fait taire le marché


def test_other_markets_of_the_same_match_survive():
    """Jouer le 1X2 ne doit pas faire disparaître les totaux du même match."""
    h2h = _row(market="h2h", outcome="home")
    tot = _row(market="totals", outcome="over", line=2.5)
    played = {f"{h2h['event_key']}|h2h|None"}
    kept = _sel([h2h, tot], played)
    assert [b["market"] for b in kept] == ["totals"]


def test_bets_too_close_to_kickoff_are_excluded():
    """Même règle que les alertes : sous 15 min, ce sont surtout des lignes
    périmées."""
    assert _sel([_row(hours=0.1)]) == []
    assert len(_sel([_row(hours=0.5)])) == 1


def test_started_matches_are_excluded():
    assert _sel([_row(hours=-2)]) == []


def test_bets_reaching_no_channel_are_excluded():
    """Un /scan qui montre des paris qui n'alertent jamais serait trompeur."""
    assert _sel([_row(ev=2.0, odd=2.40)]) == []      # sous le seuil principal
    assert _sel([_row(ev=25.0, odd=12.0)]) == []     # EV forte mais hors bandes
    assert _sel([_row(ev=15.0, odd=5.0)]) == []      # cote 4-6 sous les 20 % requis


def test_huge_ev_outside_premium_bands_is_kept_as_critical():
    kept = _sel([_row(ev=60.0, odd=14.0)])
    assert len(kept) == 1 and kept[0]["marker"] == "🚨"


# ------------------------------------------------------- déduplication ------

def test_same_selection_on_several_books_keeps_the_best_price():
    """Une sélection détectée sur trois books est UNE opportunité (§9), et on
    n'en joue qu'une — donc une seule ligne, au meilleur prix."""
    rows = [
        _row(book="unibet_be", odd=2.30, ev=10.0),
        _row(book="starcasino_sport", odd=2.45, ev=13.0),
        _row(book="circus_be", odd=2.38, ev=11.5),
    ]
    kept = _sel(rows)
    assert len(kept) == 1
    assert kept[0]["book"] == "starcasino_sport" and kept[0]["odd"] == 2.45


def test_results_are_sorted_by_ev_descending():
    rows = [_row(ev=8.0, outcome="home"), _row(ev=22.0, outcome="draw"),
            _row(ev=14.0, outcome="away")]
    assert [b["ev"] for b in _sel(rows)] == [22.0, 14.0, 8.0]


def test_unparseable_event_key_is_skipped_not_crashed():
    bad = _row()
    bad["event_key"] = "garbage"
    assert _sel([bad, _row(outcome="draw")]) != []


# ------------------------------------------------------------- marqueurs ----

@pytest.mark.parametrize("ev,odd,expected", [
    (12.0, 2.40, "💎"),    # bande premium 1.5-4
    (25.0, 5.00, "💎"),    # bande premium haute 4-6
    (60.0, 14.0, "🚨"),    # hors bandes -> critique
    (6.0, 2.40, "📊"),     # canal principal
    (6.0, 12.0, None),     # nulle part
])
def test_channel_marker_mirrors_the_alert_routing(ev, odd, expected):
    assert _channel_marker(_cfg(), ev, odd) == expected


# --------------------------------------------------------------- rendu ------

def test_format_reports_emptiness_explicitly():
    out = format_scan([], now=NOW)
    assert len(out) == 1 and "aucune value jouable" in out[0]


def test_format_lists_teams_price_book_and_delay():
    out = format_scan(_sel([_row(ev=12.4, odd=2.35, hours=3)]), now=NOW)
    assert len(out) == 1
    msg = out[0]
    assert "Anderlecht" in msg and "Club Brugge" in msg
    assert "+12.4%" in msg and "@ 2.35" in msg
    assert "dans 3 h" in msg


def test_format_shows_the_line_for_totals():
    rows = [_row(market="totals", outcome="over", line=2.5)]
    assert "over 2.5" in format_scan(_sel(rows), now=NOW)[0]


def test_format_splits_long_scans_across_messages():
    """Telegram refuse au-delà de 4096 caractères — un scan chargé doit sortir
    en plusieurs messages plutôt que d'échouer en silence."""
    rows = [_row(outcome=f"o{i}", ev=10.0 + i * 0.01) for i in range(60)]
    out = format_scan(_sel(rows), now=NOW)
    assert len(out) > 1
    assert all(len(m) <= 4096 for m in out)


def test_format_escapes_html_in_team_names():
    """Un nom d'équipe contenant < ou & casserait le parse_mode HTML."""
    row = _row(home="A & <b>B</b>")
    msg = format_scan(_sel([row]), now=NOW)[0]
    assert "&amp;" in msg and "&lt;b&gt;" in msg
