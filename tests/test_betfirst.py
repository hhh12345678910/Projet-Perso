from __future__ import annotations

import json
from pathlib import Path

import httpx

from src.models import Book, MarketType
from src.scrapers.betfirst import (
    _extract_home_away,
    _is_retryable,
    _market_type,
    _selection_label,
    parse_events_table,
)


FIXTURE = Path(__file__).parent / "fixtures" / "betfirst_events_table_sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parse_events_table_yields_1x2_with_home_draw_away():
    quotes = list(parse_events_table(_load()))
    assert len(quotes) == 3
    by = {q.outcome.label: q for q in quotes}
    assert by["home"].decimal_odd == 2.02
    assert by["draw"].decimal_odd == 3.25
    assert by["away"].decimal_odd == 3.90
    assert all(q.market == MarketType.H2H for q in quotes)
    assert all(q.book == Book.BETFIRST for q in quotes)


def test_parse_events_table_event_key_uses_home_away_order():
    quotes = list(parse_events_table(_load()))
    # Nice (side=1) is home, Saint-Etienne (side=2) is away.
    assert all("nice" in q.event_key for q in quotes)
    assert all("saintetienne" in q.event_key for q in quotes)
    assert all(q.event_key.startswith("202605291845") for q in quotes)


def test_parse_events_table_handles_empty():
    assert list(parse_events_table({})) == []
    assert list(parse_events_table({"data": {"events": [], "markets": [], "selections": []}})) == []


def test_market_type_by_template_id():
    assert _market_type({"marketTemplateId": "MW3W"}) == MarketType.H2H
    assert _market_type({"marketTemplateId": "MW2W"}) == MarketType.H2H
    assert _market_type({"marketTemplateId": "MTG2W"}) == MarketType.TOTALS
    assert _market_type({"marketTemplateId": "HCAP"}) == MarketType.HANDICAP
    assert _market_type({"marketTemplateId": "BTTS"}) is None       # Pinnacle doesn't price BTTS
    assert _market_type({"marketTemplateId": "DC"}) is None          # double chance
    assert _market_type({"marketTemplateId": None}) is None


def test_selection_label_uses_template_id():
    assert _selection_label({"selectionTemplateId": "HOME"}) == "home"
    assert _selection_label({"selectionTemplateId": "DRAW"}) == "draw"
    assert _selection_label({"selectionTemplateId": "AWAY"}) == "away"
    assert _selection_label({"selectionTemplateId": "OVER"}) == "over"
    assert _selection_label({"selectionTemplateId": "UNDER"}) == "under"
    # Unknown template falls back to isHomeTeam flag.
    assert _selection_label({"selectionTemplateId": "X", "isHomeTeam": True}) == "home"
    assert _selection_label({"selectionTemplateId": "X", "isHomeTeam": False}) == "away"


def test_extract_home_away_by_side():
    home, away = _extract_home_away([
        {"label": "Nice", "side": 1},
        {"label": "Saint-Etienne", "side": 2},
    ])
    assert home == "Nice"
    assert away == "Saint-Etienne"


def test_extract_home_away_positional_fallback():
    home, away = _extract_home_away([{"label": "A"}, {"label": "B"}])
    assert (home, away) == ("A", "B")


def test_is_retryable_only_transient():
    req = httpx.Request("GET", "https://x")
    forbidden = httpx.HTTPStatusError("e", request=req, response=httpx.Response(403, request=req))
    server = httpx.HTTPStatusError("e", request=req, response=httpx.Response(503, request=req))
    assert _is_retryable(httpx.ConnectError("boom")) is True
    assert _is_retryable(server) is True
    assert _is_retryable(forbidden) is False


def test_pages_after_the_first_are_fetched_in_parallel(monkeypatch):
    """En série sur 7 jours la collecte prenait 80 s — et le cycle attend TOUS
    les books avant de rendre la main, donc elle aurait fait passer les cycles
    de 20 s à 80 s. C'est le mode d'échec qui a fait retirer Smarkets (§5)."""
    import time
    from src.scrapers.betfirst import BetFirstScraper

    calls = []

    def fake_page(self, sport="soccer", *, days_ahead=3, max_market_count=10,
                  page_number=1):
        calls.append(page_number)
        time.sleep(0.05)                      # chaque page coûte 50 ms
        return {"data": {"events": [{"id": page_number}], "markets": [],
                         "selections": [], "totalPages": 8}}

    monkeypatch.setattr(BetFirstScraper, "fetch_events_table", fake_page)
    monkeypatch.setattr(BetFirstScraper, "__init__", lambda self, timeout=15.0: None)

    t0 = time.monotonic()
    out = BetFirstScraper().fetch_all_events("soccer")
    elapsed = time.monotonic() - t0

    assert sorted(calls) == list(range(1, 9)), "les 8 pages doivent être lues"
    assert len(out["data"]["events"]) == 8
    # 8 pages en série = 400 ms ; à 4 en parallèle, ~150 ms. La borne est large
    # pour rester stable sur une machine chargée.
    assert elapsed < 0.30, f"pagination toujours séquentielle ({elapsed:.2f}s)"


def test_a_failing_page_does_not_lose_the_others(monkeypatch):
    """Une page perdue ampute la couverture ; faire échouer l'appel entier la
    supprimerait."""
    from src.scrapers.betfirst import BetFirstScraper

    def flaky(self, sport="soccer", *, days_ahead=3, max_market_count=10,
              page_number=1):
        if page_number == 3:
            raise RuntimeError("boom")
        return {"data": {"events": [{"id": page_number}], "markets": [],
                         "selections": [], "totalPages": 4}}

    monkeypatch.setattr(BetFirstScraper, "fetch_events_table", flaky)
    monkeypatch.setattr(BetFirstScraper, "__init__", lambda self, timeout=15.0: None)
    out = BetFirstScraper().fetch_all_events("soccer")
    assert sorted(e["id"] for e in out["data"]["events"]) == [1, 2, 4]


def test_the_horizon_defaults_to_three_days():
    """7 jours de pagination pour la tranche la moins rentable (>48 h) n'a pas
    de sens : la médiane des opportunités suivies est à 23,5 h."""
    import inspect
    from src.scrapers.betfirst import BetFirstScraper
    sig = inspect.signature(BetFirstScraper.fetch_all_events)
    assert sig.parameters["days_ahead"].default == 3


# ── Hors du cycle : cache rafraîchi en fond ───────────────────────────────
# _fetch_all_parallel attend TOUS les books, donc la durée du plus lent devient
# celle du cycle. BetFirst reste le plus lent même paginé en parallèle, et sa
# fraîcheur n'a aucune valeur : il offre les pires prix du portefeuille.

def _reset_betfirst_cache():
    import src.main as m
    m._BETFIRST_CACHE.clear()
    m._BETFIRST_REFRESHING.clear()


def test_the_cycle_never_waits_for_betfirst(monkeypatch):
    """Le premier appel rend la main immédiatement, même si le rafraîchissement
    prend des dizaines de secondes."""
    import time as _t
    import src.main as m
    _reset_betfirst_cache()

    def slow(sport):
        _t.sleep(1.0)
        with m._BETFIRST_LOCK:
            m._BETFIRST_CACHE[sport] = (_t.monotonic(), ["cote"])
        with m._BETFIRST_LOCK:
            m._BETFIRST_REFRESHING.discard(sport)

    monkeypatch.setattr(m, "_betfirst_refresh", slow)
    t0 = _t.monotonic()
    out = m.fetch_betfirst_quotes("soccer")
    assert _t.monotonic() - t0 < 0.2, "le cycle a attendu BetFirst"
    assert out == [], "premier cycle : cache vide, sans conséquence"
    _t.sleep(1.3)
    assert m.fetch_betfirst_quotes("soccer") == ["cote"]
    _reset_betfirst_cache()


def test_only_one_refresh_runs_at_a_time(monkeypatch):
    """Sans ce garde, chaque cycle lancerait un thread de plus sur un book déjà
    lent — et c'est ainsi qu'on se refait bloquer par un anti-bot."""
    import src.main as m
    _reset_betfirst_cache()
    started = []
    monkeypatch.setattr(m.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda s: started.append(1)})())
    for _ in range(5):
        m.fetch_betfirst_quotes("soccer")
    assert len(started) == 1
    _reset_betfirst_cache()


def test_odds_too_old_are_dropped_rather_than_served(monkeypatch):
    """La leçon de la garde de fraîcheur du §5 : un flux périmé traité comme
    frais fabrique des value bets contre des prix qui n'existent plus."""
    import time as _t
    import src.main as m
    _reset_betfirst_cache()
    monkeypatch.setattr(m, "_BETFIRST_MAX_AGE", 10.0)
    monkeypatch.setattr(m.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda s: None})())
    m._BETFIRST_CACHE["soccer"] = (_t.monotonic() - 60, ["vieille cote"])
    assert m.fetch_betfirst_quotes("soccer") == []
    m._BETFIRST_CACHE["soccer"] = (_t.monotonic() - 2, ["cote fraîche"])
    assert m.fetch_betfirst_quotes("soccer") == ["cote fraîche"]
    _reset_betfirst_cache()


def test_betfirst_participants_are_reordered():
    """BetFirst nomme aussi les joueurs NOM D'ABORD (« Hontama, Mai »). Mesuré
    le 06/08 : 3 matchs partagés avec Pinnacle sur 88, contre 36 à 47 pour les
    autres books."""
    from src.scrapers.betfirst import _extract_home_away
    parts = [{"label": "Ku, Y", "side": 1}, {"label": "Hontama, Mai", "side": 2}]
    assert _extract_home_away(parts) == ("Y Ku", "Mai Hontama")


def test_betfirst_doubles_are_left_alone():
    from src.scrapers.betfirst import _extract_home_away
    parts = [{"label": "Barry C / Genov A", "side": 1},
             {"label": "Loureiro J V C / Ribeiro E", "side": 2}]
    assert _extract_home_away(parts) == ("Barry C / Genov A", "Loureiro J V C / Ribeiro E")
