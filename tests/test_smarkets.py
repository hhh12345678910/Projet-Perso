from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from src.models import Book, MarketType
from src.scrapers.smarkets import (
    _is_retryable,
    _mid_decimal_odd,
    _outcome_label,
    _parse_event_time,
    _parse_line_from_market_name,
    _split_event_name,
    iter_quotes_for_event,
)


def test_mid_decimal_odd_inverts_smarkets_probability_scale():
    # Prices are probability × 10000 — mid (bid+offer)/2 is the fair prob.
    # 2532 / 10000 = 0.2532, 2597 / 10000 = 0.2597, mid = 0.25645 -> odd 3.90.
    odd = _mid_decimal_odd(
        bids=[{"price": 2532, "quantity": 1}],
        offers=[{"price": 2597, "quantity": 1}],
    )
    assert odd == pytest.approx(1.0 / ((2532 + 2597) / 2 / 10000.0), abs=1e-6)


def test_mid_decimal_odd_returns_none_on_empty_book():
    assert _mid_decimal_odd([], [{"price": 2500, "quantity": 1}]) is None
    assert _mid_decimal_odd([{"price": 2500, "quantity": 1}], []) is None
    assert _mid_decimal_odd([], []) is None


def test_mid_decimal_odd_rejects_degenerate_probabilities():
    # A degenerate 100% / 0% probability would map to odd <= 1 or infinity.
    assert _mid_decimal_odd([{"price": 10000, "quantity": 1}], [{"price": 10000, "quantity": 1}]) is None


def test_split_event_name_supports_vs_variants():
    assert _split_event_name("Ivory Coast vs Ecuador") == ("Ivory Coast", "Ecuador")
    assert _split_event_name("Federer vs. Nadal") == ("Federer", "Nadal")
    assert _split_event_name("Madrid v Bayern") == ("Madrid", "Bayern")
    assert _split_event_name("garbage no separator") == (None, None)


def test_parse_line_from_over_under_market_name():
    assert _parse_line_from_market_name("Over/under 2.5") == 2.5
    assert _parse_line_from_market_name("Over/under 188.5") == 188.5
    assert _parse_line_from_market_name("Full-time result") is None


def test_outcome_label_maps_teams_to_home_away():
    assert _outcome_label("Ivory Coast", "Ivory Coast", "Ecuador", MarketType.H2H) == "home"
    assert _outcome_label("Ecuador", "Ivory Coast", "Ecuador", MarketType.H2H) == "away"
    assert _outcome_label("Draw", "Ivory Coast", "Ecuador", MarketType.H2H) == "draw"
    assert _outcome_label("Unknown", "Ivory Coast", "Ecuador", MarketType.H2H) is None
    assert _outcome_label("Over 2.5", None, None, MarketType.TOTALS) == "over"
    assert _outcome_label("Under 2.5", None, None, MarketType.TOTALS) == "under"


def test_parse_event_time_handles_iso_z_suffix():
    t = _parse_event_time("2026-06-14T23:00:00Z")
    assert t == datetime(2026, 6, 14, 23, 0, tzinfo=timezone.utc)
    assert _parse_event_time(None) is None
    assert _parse_event_time("garbage") is None


def _stub_scraper(markets, contracts_by_market, quotes_by_contract):
    sc = MagicMock()
    sc.fetch_event_markets.return_value = markets
    sc.fetch_market_contracts.side_effect = lambda mid: contracts_by_market.get(mid, [])
    sc.fetch_quotes.return_value = quotes_by_contract
    return sc


def test_iter_quotes_yields_3way_winner_and_totals_for_one_event():
    event = {
        "id": 44754796,
        "name": "Ivory Coast vs Ecuador",
        "start_datetime": "2026-06-14T23:00:00Z",
    }
    markets = [
        {
            "id": 119416335,
            "state": "open",
            "name": "Full-time result",
            "market_type": {"name": "WINNER_3_WAY"},
        },
        {
            "id": 119416372,
            "state": "open",
            "name": "Over/under 2.5",
            "market_type": {"name": "OVER_UNDER"},
        },
        {
            "id": 999999,
            "state": "open",
            "name": "Correct score",
            "market_type": {"name": "CORRECT_SCORE"},  # ignored downstream
        },
    ]
    contracts = {
        119416335: [
            {"id": 1, "name": "Ivory Coast"},
            {"id": 2, "name": "Draw"},
            {"id": 3, "name": "Ecuador"},
        ],
        119416372: [
            {"id": 4, "name": "Over 2.5"},
            {"id": 5, "name": "Under 2.5"},
        ],
    }
    quotes = {
        "1": {"bids": [{"price": 2532, "quantity": 1}], "offers": [{"price": 2597, "quantity": 1}]},
        "2": {"bids": [{"price": 3175, "quantity": 1}], "offers": [{"price": 3279, "quantity": 1}]},
        "3": {"bids": [{"price": 3876, "quantity": 1}], "offers": [{"price": 3968, "quantity": 1}]},
        "4": {"bids": [{"price": 5500, "quantity": 1}], "offers": [{"price": 5550, "quantity": 1}]},
        "5": {"bids": [{"price": 4400, "quantity": 1}], "offers": [{"price": 4500, "quantity": 1}]},
    }
    sc = _stub_scraper(markets, contracts, quotes)
    qs = list(iter_quotes_for_event(sc, event))
    # 3 H2H + 2 totals = 5 quotes; the CORRECT_SCORE market is dropped.
    assert len(qs) == 5
    labels = {(q.market.value, q.outcome.label): q for q in qs}
    assert labels[("h2h", "home")].decimal_odd > 1.0
    assert labels[("h2h", "draw")].decimal_odd > 1.0
    assert labels[("h2h", "away")].decimal_odd > 1.0
    assert labels[("totals", "over")].outcome.line == 2.5
    assert labels[("totals", "under")].outcome.line == 2.5
    assert all(q.book == Book.SMARKETS for q in qs)


def test_iter_quotes_skips_event_with_unparseable_name():
    sc = _stub_scraper([], {}, {})
    qs = list(iter_quotes_for_event(sc, {
        "id": 1, "name": "no separator here", "start_datetime": "2026-06-14T23:00:00Z",
    }))
    assert qs == []


def test_is_retryable_only_transient():
    req = httpx.Request("GET", "https://x")
    forbidden = httpx.HTTPStatusError("e", request=req, response=httpx.Response(403, request=req))
    server = httpx.HTTPStatusError("e", request=req, response=httpx.Response(503, request=req))
    assert _is_retryable(httpx.ConnectError("x")) is True
    assert _is_retryable(server) is True
    assert _is_retryable(forbidden) is False


# ---------------------------------------------------------------- appels groupés
#
# Le §5 avait retiré Smarkets pour ses ~26 minutes de rafraîchissement, dues à
# une requête par événement et par marché. Les tests ci-dessous couvrent le
# chemin groupé qui lève ce blocage — et surtout son ATTRIBUTION, qui est le
# piège du §10 : une réponse groupée ne dit pas quelle requête elle sert.

from datetime import timedelta                                        # noqa: E402
from src.scrapers.smarkets import (                                   # noqa: E402
    SmarketsScraper,
    iter_all_quotes_fast,
)


def _scraper_with(get_impl):
    """Un vrai SmarketsScraper dont seul le transport est remplacé."""
    sc = SmarketsScraper.__new__(SmarketsScraper)
    sc._delay = 0
    sc._get = get_impl
    return sc


def test_grouped_markets_are_attributed_by_event_id_not_arrival_order():
    # Le serveur rend les marchés dans le DÉSORDRE : ceux de l'événement 2
    # arrivent avant ceux de l'événement 1. Attribuer par ordre d'arrivée
    # croiserait les deux événements — la panne Circus du §10.
    def _get(path, params=None):
        assert path == "/events/1,2/markets/"
        return {"markets": [
            {"id": 20, "event_id": 2, "state": "open",
             "market_type": {"name": "WINNER_3_WAY"}},
            {"id": 10, "event_id": 1, "state": "open",
             "market_type": {"name": "WINNER_3_WAY"}},
        ]}
    got = _scraper_with(_get).fetch_markets_for_events([1, 2])
    assert [m["id"] for m in got[1]] == [10]
    assert [m["id"] for m in got[2]] == [20]


def test_grouped_contracts_are_attributed_by_market_id():
    def _get(path, params=None):
        assert path == "/markets/10,20/contracts/"
        return {"contracts": [
            {"id": 200, "market_id": 20, "name": "Over 2.5"},
            {"id": 100, "market_id": 10, "name": "Draw"},
        ]}
    got = _scraper_with(_get).fetch_contracts_for_markets([10, 20])
    assert [c["id"] for c in got[10]] == [100]
    assert [c["id"] for c in got[20]] == [200]


def test_grouped_call_falls_back_to_single_calls_when_api_refuses():
    # Si l'API refuse la syntaxe groupée, on ne devine pas : on retombe sur
    # les appels unitaires, lents mais jamais faux.
    calls = []

    def _get(path, params=None):
        if "," in path:
            raise httpx.HTTPStatusError("400", request=None, response=None)
        calls.append(path)
        return {"contracts": [{"id": 1, "name": "Draw"}]}

    sc = _scraper_with(_get)
    sc.fetch_market_contracts = lambda mid: sc._get(f"/markets/{mid}/contracts/")["contracts"]
    got = sc.fetch_contracts_for_markets([10, 20])
    assert calls == ["/markets/10/contracts/", "/markets/20/contracts/"]
    assert len(got[10]) == 1 and len(got[20]) == 1


def test_grouped_call_falls_back_when_response_cannot_be_attributed():
    # Réponse groupée acceptée, mais sans market_id : inattribuable, donc
    # repli. Preuve positive exigée, jamais de supposition.
    def _get(path, params=None):
        if "," in path:
            return {"contracts": [{"id": 1, "name": "Draw"}]}   # pas de market_id
        return {"contracts": [{"id": 9, "name": "Draw"}]}

    sc = _scraper_with(_get)
    sc.fetch_market_contracts = lambda mid: sc._get(f"/markets/{mid}/contracts/")["contracts"]
    got = sc.fetch_contracts_for_markets([10, 20])
    assert got[10][0]["id"] == 9 and got[20][0]["id"] == 9


def test_within_hours_drops_far_events_but_keeps_unparseable_dates():
    soon = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat().replace("+00:00", "Z")
    far = (datetime.now(timezone.utc) + timedelta(hours=200)).isoformat().replace("+00:00", "Z")

    def _get(path, params=None):
        return {"events": [
            {"id": 1, "name": "A vs B", "start_datetime": soon},
            {"id": 2, "name": "C vs D", "start_datetime": far},
            {"id": 3, "name": "E vs F", "start_datetime": None},
        ], "pagination": {}}

    got = _scraper_with(_get).fetch_upcoming_events("soccer", within_hours=48)
    assert {e["id"] for e in got} == {1, 3}


def test_fast_path_never_crosses_two_events():
    # Le test qui compte : deux événements traités dans la même passe groupée
    # ne doivent jamais échanger leurs cotes.
    soon = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")

    def _get(path, params=None):
        if path.startswith("/events/") and path.endswith("/markets/"):
            return {"markets": [
                {"id": 20, "event_id": 2, "state": "open", "name": "Full-time result",
                 "market_type": {"name": "WINNER_3_WAY"}},
                {"id": 10, "event_id": 1, "state": "open", "name": "Full-time result",
                 "market_type": {"name": "WINNER_3_WAY"}},
            ]}
        if path.startswith("/events/"):
            return {"events": [
                {"id": 1, "name": "Arsenal vs Chelsea", "start_datetime": soon},
                {"id": 2, "name": "Lyon vs Monaco", "start_datetime": soon},
            ], "pagination": {}}
        if "contracts" in path:
            return {"contracts": [
                {"id": 201, "market_id": 20, "name": "Lyon"},
                {"id": 101, "market_id": 10, "name": "Arsenal"},
            ]}
        return {
            "101": {"bids": [{"price": 4000, "quantity": 1}],
                    "offers": [{"price": 4100, "quantity": 1}]},
            "201": {"bids": [{"price": 5000, "quantity": 1}],
                    "offers": [{"price": 5100, "quantity": 1}]},
        }

    qs = list(iter_all_quotes_fast(_scraper_with(_get), "soccer"))
    assert len(qs) == 2
    by_key = {q.event_key: q for q in qs}
    assert len(by_key) == 2, "deux événements distincts attendus"
    # Arsenal (contrat 101, prob ~0.405) doit porter la cote ~2.47, et Lyon
    # (contrat 201, prob ~0.505) la cote ~1.98. Un croisement les échangerait.
    arsenal = [q for q in qs if "arsenal" in q.event_key][0]
    lyon = [q for q in qs if "lyon" in q.event_key][0]
    assert arsenal.decimal_odd == pytest.approx(1 / 0.4050, abs=1e-3)
    assert lyon.decimal_odd == pytest.approx(1 / 0.5050, abs=1e-3)


# ------------------------------------- mesurer un pari valorisé sur un repli --
#
# Un pari valorisé contre Smarkets ne peut pas être mesuré contre la clôture
# Pinnacle : si Pinnacle pricait ce marché, il n'y aurait pas eu de repli. Sans
# les deux correctifs ci-dessous, ces paris n'auraient JAMAIS de CLV et
# disparaîtraient de toute analyse — sans une seule erreur nulle part.

def test_closing_group_reads_the_book_it_is_asked_for(tmp_path):
    from datetime import datetime as _dt, timezone as _tz
    from src.storage import Storage
    from src.models import Book as _B, MarketType as _MT, OddQuote as _Q, Outcome as _O

    st = Storage(str(tmp_path / "t.db"))
    ek = "202608141900::alpha__vs__beta"
    seen = _dt(2026, 8, 14, 18, 0, tzinfo=_tz.utc)
    st.insert_quotes([
        _Q(event_key=ek, book=_B.SMARKETS, market=_MT.H2H,
           outcome=_O(label=lbl), decimal_odd=odd, fetched_at=seen,
           source_event_id="x")
        for lbl, odd in (("home", 2.00), ("away", 2.05))
    ])
    before = _dt(2026, 8, 14, 19, 0, tzinfo=_tz.utc)

    # Pinnacle n'a rien : la clôture doit être introuvable de son côté...
    assert st.closing_group(ek, "h2h", None, before, book="pinnacle") == []
    # ...et parfaitement lisible du côté de la référence réelle.
    grp = st.closing_group(ek, "h2h", None, before, book="smarkets")
    assert {r["outcome_label"] for r in grp} == {"home", "away"}


def test_pinnacle_closing_group_still_behaves_as_before(tmp_path):
    """Le nom historique reste utilisé partout ailleurs — il ne doit pas
    changer de comportement."""
    from datetime import datetime as _dt, timezone as _tz
    from src.storage import Storage
    from src.models import Book as _B, MarketType as _MT, OddQuote as _Q, Outcome as _O

    st = Storage(str(tmp_path / "t2.db"))
    ek = "202608141900::alpha__vs__beta"
    seen = _dt(2026, 8, 14, 18, 0, tzinfo=_tz.utc)
    st.insert_quotes([
        _Q(event_key=ek, book=b, market=_MT.H2H, outcome=_O(label="home"),
           decimal_odd=2.0, fetched_at=seen, source_event_id="x")
        for b in (_B.PINNACLE, _B.SMARKETS)
    ])
    before = _dt(2026, 8, 14, 19, 0, tzinfo=_tz.utc)
    grp = st.pinnacle_closing_group(ek, "h2h", None, before)
    assert len(grp) == 1 and grp[0]["book"] == "pinnacle"
