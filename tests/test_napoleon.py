from __future__ import annotations

import json
from pathlib import Path

from src.models import Book, MarketType
from src.scrapers.napoleon import parse_by_date, _odd

FIXTURE = Path(__file__).parent / "fixtures" / "napoleon_by_date_sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_odd_guard():
    assert _odd(3.65) == 3.65
    assert _odd(1.0) is None
    assert _odd("x") is None
    assert _odd(None) is None


def test_parse_by_date_yields_napoleon_1x2():
    quotes = list(parse_by_date(_load()))
    assert quotes
    assert all(q.book == Book.NAPOLEON_BE for q in quotes)
    assert all(q.market == MarketType.H2H for q in quotes)
    # 1X2 codes map to home/draw/away.
    labels = {q.outcome.label for q in quotes}
    assert labels <= {"home", "draw", "away"}
    assert "home" in labels and "away" in labels


def test_event_keys_and_odds_sane():
    quotes = list(parse_by_date(_load()))
    assert all("::" in q.event_key for q in quotes)
    assert all(q.decimal_odd > 1.0 for q in quotes)
    # matchName "home·away" splits cleanly into the event key.
    assert all("__vs__" in q.event_key for q in quotes)


def _tennis_payload() -> dict:
    """Forme réelle observée sur l'API : marketId 521, codes 1 et 2, pas de 0.

    Le marché 547 y figure aussi pour vérifier qu'on ne prend pas le premier
    venu — un flux mêlant les deux ne doit livrer que le vainqueur du sport
    demandé."""
    return {"data": [{
        "matchName": "Alcaraz C.·Sinner J.",
        "matchDate": "2026-08-01 12:00:00",
        "tournamentName": "ATP Toronto",
        "eventId": 991,
        "odds": [
            {"marketId": 521, "code": "1", "price": 2.47, "status": "active"},
            {"marketId": 521, "code": "2", "price": 1.46, "status": "active"},
            {"marketId": 547, "code": "1", "price": 9.99, "status": "active"},
        ],
    }]}


def test_tennis_reads_the_two_way_winner():
    """Sans le sport, le tennis était intégralement jeté : son vainqueur porte
    le marché 521, pas le 1X2 à trois issues du football."""
    quotes = list(parse_by_date(_tennis_payload(), "tennis"))
    assert {(q.outcome.label, q.decimal_odd) for q in quotes} == {
        ("home", 2.47), ("away", 1.46),
    }


def test_a_two_way_market_is_ignored_on_a_sport_that_has_a_draw():
    """Sur un sport à match nul, un marché à deux issues n'est pas un
    vainqueur mais un draw-no-bet, avec exactement les mêmes codes 1/2. Le
    prendre pour un vainqueur inventerait de la valeur face au 1X2 de
    Pinnacle."""
    quotes = list(parse_by_date(_tennis_payload(), "soccer"))
    assert [q.decimal_odd for q in quotes] == [9.99]


def test_an_unknown_sport_yields_nothing_rather_than_wrong():
    """Un sport non répertorié retombe sur le marché à trois issues. Face à un
    flux qui n'en contient pas, il ne produit rien — préférable à produire une
    cote dont on ignore ce qu'elle price."""
    payload = _tennis_payload()
    payload["data"][0]["odds"] = [
        o for o in payload["data"][0]["odds"] if o["marketId"] == 521
    ]
    assert list(parse_by_date(payload, "handball")) == []
