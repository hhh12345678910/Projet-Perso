"""Parseur MagicBetting (Digitain), sur la structure réellement observée."""
from __future__ import annotations

from src.models import Book, MarketType
from src.scrapers.magicbetting import parse_events


def _event(**over):
    ev = {
        "Id": 39417371, "SId": 1, "HT": "Orlando City SC", "AT": "FC Cincinnati",
        "D": "2026-08-16T23:30:00Z", "CN": "USA. MLS", "HS": None, "AS": None,
        "StakeTypes": [
            {"Id": 1, "N": "Wedstrijdweddenschappen", "Stakes": [
                {"N": "Orlando City SC", "F": 2.12, "A": None},
                {"N": "Gelijkspel", "F": 4.15, "A": None},
                {"N": "FC Cincinnati", "F": 2.58, "A": None}]},
            {"Id": 3, "N": "Totaal", "Stakes": [
                {"N": "Over", "F": 1.23, "A": 2.5},
                {"N": "Onder", "F": 3.54, "A": 2.5}]},
        ],
    }
    ev.update(over)
    return ev


def test_h2h_and_totals_are_parsed():
    qs = list(parse_events([_event()]))
    got = {(q.market.value, q.outcome.label, q.outcome.line): q.decimal_odd for q in qs}
    assert got == {
        ("h2h", "home", None): 2.12,
        ("h2h", "draw", None): 4.15,
        ("h2h", "away", None): 2.58,
        ("totals", "over", 2.5): 1.23,
        ("totals", "under", 2.5): 3.54,
    }
    assert all(q.book is Book.MAGICBETTING for q in qs)


def test_the_draw_is_found_without_knowing_the_language():
    """« Gelijkspel » est du néerlandais. L'identifier par comparaison aux noms
    d'équipe de l'événement rend le parseur indépendant de `langId`."""
    ev = _event()
    ev["StakeTypes"][0]["Stakes"][1]["N"] = "Match nul"
    labels = {q.outcome.label for q in parse_events([ev]) if q.market is MarketType.H2H}
    assert labels == {"home", "draw", "away"}


def test_duplicate_featured_markets_are_ignored():
    """Les Id négatifs répètent le même marché sur une seule ligne. Les
    accepter écrirait deux fois la même cote."""
    ev = _event()
    ev["StakeTypes"].append({"Id": -3, "N": "Totaal", "Stakes": [
        {"N": "Over", "F": 1.67, "A": 3.5}, {"N": "Onder", "F": 2.01, "A": 3.5}]})
    lines = [q.outcome.line for q in parse_events([ev]) if q.market is MarketType.TOTALS]
    assert sorted(lines) == [2.5, 2.5], "la ligne vedette ne doit pas être reprise"


def test_excluded_markets_never_produce_quotes():
    """Handicaps, double chance et totaux asiatiques n'ont pas de contrepartie
    chez Pinnacle — même politique que Circus (§10)."""
    ev = _event()
    ev["StakeTypes"] += [
        {"Id": 37, "N": "Dubbele kans", "Stakes": [{"N": "1X", "F": 1.43, "A": None}]},
        {"Id": 2, "N": "Handicap", "Stakes": [{"N": "Orlando City SC", "F": 7.88, "A": -2.5}]},
        {"Id": 2533, "N": "Aziatisch", "Stakes": [{"N": "Over", "F": 1.31, "A": 2.75}]},
    ]
    assert len(list(parse_events([ev]))) == 5


def test_a_started_match_is_dropped():
    """Un score renseigné veut dire que le match a commencé : la ligne de
    référence Pinnacle est prématch et n'a plus de sens (§9)."""
    assert list(parse_events([_event(HS=1, AS=0)])) == []


def test_a_total_without_a_line_is_dropped():
    ev = _event()
    ev["StakeTypes"][1]["Stakes"][0]["A"] = None
    lines = [q.outcome.label for q in parse_events([ev]) if q.market is MarketType.TOTALS]
    assert lines == ["under"], "un total sans seuil est incomparable à Pinnacle"


def test_garbage_never_raises():
    assert list(parse_events(None)) == []
    assert list(parse_events([{"SId": 999}])) == []
    assert list(parse_events([_event(D="pas une date")])) == []
