"""Circus / Gaming1 — sur une capture réelle de GetPrematchSport."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.models import Book, MarketType
from src.scrapers.circus import (
    load_pushed_quotes, parse_prematch, parse_push,
)

FIXTURE = Path(__file__).parent / "fixtures" / "circus_prematch_sample.json"


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def quotes(payload):
    return list(parse_prematch(payload))


def test_reads_the_three_1x2_outcomes(quotes):
    h2h = [q for q in quotes if q.market is MarketType.H2H]
    by_event: dict[str, set[str]] = {}
    for q in h2h:
        by_event.setdefault(q.event_key, set()).add(q.outcome.label)
    assert by_event
    for labels in by_event.values():
        assert labels == {"home", "draw", "away"}


def test_team_names_map_to_home_and_away(payload):
    ev = payload["Leagues"][0]["Events"][0]
    quotes = [q for q in parse_prematch(payload) if q.market is MarketType.H2H]
    mine = [q for q in quotes if str(ev["EventId"]) == q.source_event_id]
    odds = {q.outcome.label: q.decimal_odd for q in mine}
    market = next(m for m in ev["Markets"] if m["BetType"] == "P1XP2")
    expected = {o["Name"]: o["Odd"] for o in market["Outcomes"]}
    assert odds["home"] == expected[ev["HomeName"]]
    assert odds["away"] == expected[ev["AwayName"]]


def test_totals_take_their_line_from_base(quotes):
    """Les issues s'appellent « Plus de » / « Moins de » sans la valeur : la
    ligne n'existe que dans Base. La lire ailleurs donnerait des totaux sans
    ligne, incomparables à Pinnacle."""
    totals = [q for q in quotes if q.market is MarketType.TOTALS]
    assert totals
    assert all(q.outcome.line is not None for q in totals)
    assert {q.outcome.label for q in totals} == {"over", "under"}


def test_derivative_markets_are_reported_not_swallowed(payload):
    """Un BetType inconnu doit remonter. Jeter en silence, c'est ne jamais voir
    qu'un book a renommé un code."""
    unknown: set[str] = set()
    list(parse_prematch(payload, unknown_types=unknown))
    assert "both-teams-to-score" in unknown
    assert "1st-half-1x2" in unknown


def test_half_time_markets_never_become_quotes(quotes):
    """Comparer une mi-temps au total match complet de Pinnacle fabriquerait
    de faux value bets — le piège déjà rencontré sur Betano."""
    assert all(q.market in (MarketType.H2H, MarketType.TOTALS) for q in quotes)
    # 1ère mi-temps : 3 issues comme un 1X2, donc invisible sans ce contrôle.
    lines = {(q.event_key, q.market, q.outcome.label, q.outcome.line) for q in quotes}
    assert len(lines) == len(quotes), "aucun doublon issu d'un marché dérivé"


def test_closed_market_odds_are_dropped(payload):
    """Gaming1 marque un marché fermé par une cote négative."""
    ev = payload["Leagues"][0]["Events"][0]
    market = next(m for m in ev["Markets"] if m["BetType"] == "P1XP2")
    market["Outcomes"][0]["Odd"] = -1.0

    labels = {q.outcome.label for q in parse_prematch(payload)
              if q.market is MarketType.H2H and q.source_event_id == str(ev["EventId"])}
    assert "home" not in labels
    assert {"draw", "away"} <= labels


def test_blocked_event_is_skipped(payload):
    for ev in payload["Leagues"][0]["Events"]:
        ev["IsBlocked"] = True
    kept = {q.source_event_id for q in parse_prematch(payload)}
    for ev in payload["Leagues"][0]["Events"]:
        assert str(ev["EventId"]) not in kept


def test_quotes_carry_book_and_league(quotes):
    assert all(q.book is Book.CIRCUS_BE for q in quotes)
    assert any(q.league for q in quotes), "la compétition sert dans l'alerte"


def test_overlapping_days_are_deduplicated(payload):
    """Le userscript interroge plusieurs jours et ils se recouvrent."""
    once = parse_push([payload])
    twice = parse_push([payload, payload])
    assert len(twice) == len(once)


def test_leagues_of_another_sport_are_refused(payload):
    """Le pont a déjà croisé deux réponses et écrit le tennis dans
    soccer.json. Le nom du fichier ne prouve rien ; le SportId de la ligue,
    si."""
    sid = payload["Leagues"][0]["SportId"]
    foreign: list[int] = []
    quotes = parse_push([payload], expect_sport_id=sid + 1, foreign=foreign)
    assert quotes == []
    assert set(foreign) == {sid}

    assert parse_push([payload], expect_sport_id=sid)


def test_a_mixed_dump_keeps_only_the_expected_sport(payload):
    intruder = json.loads(json.dumps(payload["Leagues"][0]))
    intruder["SportId"] = 999
    for ev in intruder["Events"]:
        ev["EventId"] = str(ev["EventId"]) + "-x"
    payload["Leagues"].append(intruder)

    sid = payload["Leagues"][0]["SportId"]
    quotes = parse_push([payload], expect_sport_id=sid)
    assert quotes
    assert all(not q.source_event_id.endswith("-x") for q in quotes)


def test_a_misrouted_dump_is_reported(tmp_path, payload):
    p = tmp_path / "soccer.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    msgs: list[str] = []
    sid = payload["Leagues"][0]["SportId"]
    assert load_pushed_quotes(str(p), expect_sport_id=sid + 1,
                              print_fn=msgs.append) == []
    assert msgs and "autre sport" in msgs[0]


def test_a_stale_dump_is_refused(tmp_path, payload):
    """Un onglet fermé ne doit pas produire des cotes mortes traitées comme
    fraîches — en silence. C'est la garde apprise sur Betano."""
    p = tmp_path / "circus.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
    import os
    os.utime(p, (old, old))

    msgs: list[str] = []
    assert load_pushed_quotes(str(p), max_age_minutes=30, print_fn=msgs.append) == []
    assert msgs and "vieux" in msgs[0]


def test_a_fresh_dump_is_read(tmp_path, payload):
    p = tmp_path / "circus.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    assert load_pushed_quotes(str(p), max_age_minutes=30)


def test_a_missing_or_broken_dump_is_not_fatal(tmp_path):
    assert load_pushed_quotes(str(tmp_path / "absent.json")) == []
    bad = tmp_path / "bad.json"
    bad.write_text("{ pas du json", encoding="utf-8")
    msgs: list[str] = []
    assert load_pushed_quotes(str(bad), print_fn=msgs.append) == []
    assert msgs
