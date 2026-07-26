from __future__ import annotations

import json
from pathlib import Path

import httpx

from src.models import Book, MarketType
from src.scrapers.betano import (
    BetanoAuthError,
    _extract_home_away,
    _h2h_label,
    _is_retryable,
    _market_type,
    _normalise_outcome_label,
    _parse_cookie_header,
    _parse_datetime,
    _side_from_team,
    parse_overview,
    parse_prematch,
)


FIXTURE = Path(__file__).parent / "fixtures" / "betano_overview_sample.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parse_overview_extracts_1x2_with_correct_labels():
    quotes = list(parse_overview(_load()))
    catania = {
        (q.market, q.outcome.label): q
        for q in quotes
        if "calciocatania" in q.event_key
    }
    assert catania[(MarketType.H2H, "home")].decimal_odd == 1.45
    assert catania[(MarketType.H2H, "draw")].decimal_odd == 3.65
    assert catania[(MarketType.H2H, "away")].decimal_odd == 8.25
    assert catania[(MarketType.TOTALS, "over")].decimal_odd == 2.22
    assert catania[(MarketType.TOTALS, "under")].decimal_odd == 1.57


def test_parse_overview_carries_totals_line():
    totals = [q for q in parse_overview(_load()) if q.market == MarketType.TOTALS]
    assert totals and all(q.outcome.line == 2.5 for q in totals)


def test_parse_overview_handicap_labels_and_signed_line():
    hc = {q.outcome.label: q for q in parse_overview(_load()) if q.market == MarketType.HANDICAP}
    assert hc["home"].outcome.line == -1.5
    assert hc["away"].outcome.line == 1.5
    assert hc["home"].decimal_odd == 3.7


def test_parse_overview_two_way_winner_mapped_to_home_away():
    # H2HT market labelled with team names -> resolved to home/away.
    monaco = {q.outcome.label: q for q in parse_overview(_load()) if "monaco" in q.event_key}
    assert monaco["home"].decimal_odd == 5.8   # Bourg-en-Bresse (isHome)
    assert monaco["away"].decimal_odd == 1.11  # Monaco
    assert "draw" not in monaco


def test_parse_overview_sets_book():
    quotes = list(parse_overview(_load()))
    assert quotes and all(q.book == Book.BETANO_BE for q in quotes)


def test_parse_overview_handles_empty():
    assert list(parse_overview({})) == []
    assert list(parse_overview({"events": {}, "markets": {}, "selections": {}})) == []


def test_market_type_by_code():
    assert _market_type({"type": "MRES"}) == MarketType.H2H
    assert _market_type({"type": "H2HT"}) == MarketType.H2H
    assert _market_type({"type": "HCTG"}) == MarketType.TOTALS
    assert _market_type({"type": "HCAP"}) == MarketType.HANDICAP
    assert _market_type({"type": "DNOB"}) is None       # draw-no-bet, not moneyline
    assert _market_type({"type": "BTSC"}) is None        # BTTS, no Pinnacle reference
    assert _market_type({"type": None}) is None


def test_h2h_label_direct_and_by_team():
    assert _h2h_label("1", "Catania", "Ascoli") == "home"
    assert _h2h_label("X", "Catania", "Ascoli") == "draw"
    assert _h2h_label("2", "Catania", "Ascoli") == "away"
    assert _h2h_label("Monaco", "Bourg-en-Bresse", "Monaco") == "away"


def test_side_from_team():
    assert _side_from_team("Toronto Blue Jays -1.5", "Toronto Blue Jays", "Miami Marlins") == "home"
    assert _side_from_team("Miami Marlins +1.5", "Toronto Blue Jays", "Miami Marlins") == "away"
    assert _side_from_team("Unrelated FC", "Toronto Blue Jays", "Miami Marlins") is None


def test_normalise_outcome_label():
    assert _normalise_outcome_label("Plus de 2.5", MarketType.TOTALS) == "over"
    assert _normalise_outcome_label("Moins de 2.5", MarketType.TOTALS) == "under"


def test_extract_home_away_by_is_home_flag():
    home, away = _extract_home_away([
        {"name": "Calcio Catania", "isHome": True},
        {"name": "Ascoli Calcio 1898"},
    ])
    assert home == "Calcio Catania"
    assert away == "Ascoli Calcio 1898"


def test_extract_home_away_by_role():
    home, away = _extract_home_away([
        {"name": "Valencia CF", "type": "home"},
        {"name": "Rayo Vallecano", "type": "away"},
    ])
    assert home == "Valencia CF"
    assert away == "Rayo Vallecano"


def test_extract_home_away_positional_fallback():
    home, away = _extract_home_away([{"name": "A"}, {"name": "B"}])
    assert (home, away) == ("A", "B")


def test_parse_datetime_iso_and_epoch():
    assert _parse_datetime("2026-05-17T18:30:00Z").year == 2026
    assert _parse_datetime(1778775134).year >= 2026
    assert _parse_datetime(1778775134000).year >= 2026
    assert _parse_datetime(None) is None
    assert _parse_datetime("garbage") is None


def test_parse_cookie_header():
    c = _parse_cookie_header("a=1; b=2; c = 3")
    assert c == {"a": "1", "b": "2", "c": "3"}


def test_is_retryable_skips_auth_and_4xx():
    req = httpx.Request("GET", "https://x")
    forbidden = httpx.HTTPStatusError(
        "e", request=req, response=httpx.Response(403, request=req)
    )
    server_err = httpx.HTTPStatusError(
        "e", request=req, response=httpx.Response(503, request=req)
    )
    assert _is_retryable(BetanoAuthError("nope")) is False
    assert _is_retryable(forbidden) is False
    assert _is_retryable(server_err) is True
    assert _is_retryable(httpx.ConnectError("boom")) is True


# ---------------------------------------------------------------------------
# Prematch offer (/fr/api/sport/{slug}/matchs-a-venir)
# Shapes below mirror a real tennis capture: startTime in epoch millis,
# selections named after the participant, price as the decimal odd.
# ---------------------------------------------------------------------------

def _prematch_payload(markets: list[dict]) -> dict:
    return {
        "data": {
            "blocks": [
                {
                    "id": 1,
                    "name": "ATP - Estoril",
                    "events": [
                        {
                            "id": "89684492",
                            "sportId": "TENN",
                            "startTime": 1785146400000,
                            "participants": [
                                {"id": 1, "name": "Daria Snigur"},
                                {"id": 2, "name": "Lilli Tagger"},
                            ],
                            "markets": markets,
                        }
                    ],
                }
            ]
        }
    }


def _winner_market(mtype: str = "HTOH") -> dict:
    return {
        "id": "2875196453",
        "name": "Vainqueur",
        "type": mtype,
        "handicap": 0.0,
        "selections": [
            {"id": "a", "name": "Daria Snigur", "price": 1.7, "handicap": 0.0},
            {"id": "b", "name": "Lilli Tagger", "price": 2.1, "handicap": 0.0},
        ],
    }


def test_parse_prematch_maps_winner_to_h2h_sides():
    quotes = list(parse_prematch(_prematch_payload([_winner_market()])))
    assert {q.outcome.label for q in quotes} == {"home", "away"}
    assert all(q.market is MarketType.H2H for q in quotes)
    assert all(q.book is Book.BETANO_BE for q in quotes)
    by_label = {q.outcome.label: q.decimal_odd for q in quotes}
    assert by_label == {"home": 1.7, "away": 2.1}


def test_parse_prematch_drops_padding_line_on_two_way_winner():
    """A 0.0 handicap on a winner market is padding. Keeping it would stop the
    quote matching Pinnacle's line-less H2H fair line."""
    quotes = list(parse_prematch(_prematch_payload([_winner_market()])))
    assert all(q.outcome.line is None for q in quotes)


def test_parse_prematch_reads_epoch_millis_start_time():
    quotes = list(parse_prematch(_prematch_payload([_winner_market()])))
    # 1785146400000 ms -> 2026-07-26 in UTC; event_key is prefixed with it.
    assert quotes[0].event_key.startswith("202607")


def test_parse_prematch_totals_carry_the_line():
    totals = {
        "id": "m2",
        "name": "Jeux",
        "type": "FTGO",
        "handicap": 21.5,
        "selections": [
            {"name": "Plus de", "price": 1.85, "handicap": 21.5},
            {"name": "Moins de", "price": 1.95, "handicap": 21.5},
        ],
    }
    quotes = list(parse_prematch(_prematch_payload([totals])))
    assert {q.outcome.label for q in quotes} == {"over", "under"}
    assert all(q.market is MarketType.TOTALS for q in quotes)
    assert all(q.outcome.line == 21.5 for q in quotes)


def test_parse_prematch_zero_rake_winner_is_still_h2h():
    """HTHP is the 0%-margin winner market — a genuinely bettable price, so it
    must not be dropped just because its code differs from HTOH."""
    quotes = list(parse_prematch(_prematch_payload([_winner_market("HTHP")])))
    assert len(quotes) == 2
    assert all(q.market is MarketType.H2H for q in quotes)


def test_parse_prematch_reports_unknown_market_codes():
    unknown: set[str] = set()
    payload = _prematch_payload([{"type": "XXNEW", "selections": [{"name": "x", "price": 2.0}]}])
    assert list(parse_prematch(payload, unknown_types=unknown)) == []
    assert unknown == {"XXNEW"}


def test_parse_prematch_skips_unusable_prices():
    market = {
        "type": "HTOH",
        "selections": [
            {"name": "Daria Snigur", "price": 1.0},      # no value at evens-or-worse
            {"name": "Lilli Tagger", "price": None},
            {"name": "Daria Snigur", "price": "abc"},
        ],
    }
    assert list(parse_prematch(_prematch_payload([market]))) == []


def test_parse_prematch_tolerates_empty_and_malformed_payloads():
    assert list(parse_prematch({})) == []
    assert list(parse_prematch({"data": {"blocks": []}})) == []
    assert list(parse_prematch({"data": {"blocks": [{"events": [{}]}]}})) == []


def test_parse_prematch_maps_superodds_boosted_1x2():
    """MR12 is 'Résultat de match SuperOdds' — a boosted 1X2. Boosted prices are
    exactly where value shows up, so it must not be dropped."""
    market = {
        "type": "MR12",
        "selections": [
            {"name": "1", "fullName": "Daria Snigur", "price": 2.60},
            {"name": "2", "fullName": "Lilli Tagger", "price": 2.05},
        ],
    }
    quotes = list(parse_prematch(_prematch_payload([market])))
    assert {q.outcome.label for q in quotes} == {"home", "away"}
    assert all(q.market is MarketType.H2H for q in quotes)


def test_parse_prematch_drops_first_half_totals():
    """OUH1 is first-half goals. Treating it as TOTALS would price it against
    Pinnacle's full-match ladder and manufacture phantom value bets."""
    market = {
        "type": "OUH1",
        "name": "But en première mi-temps Plus de/Moins de",
        "handicap": 1.5,
        "selections": [
            {"name": "Plus de", "price": 1.90, "handicap": 1.5},
            {"name": "Moins de", "price": 1.90, "handicap": 1.5},
        ],
    }
    assert list(parse_prematch(_prematch_payload([market]))) == []


def test_parse_prematch_known_exclusions_are_not_reported_as_unknown():
    """Deliberate exclusions must stay out of the unknown set, or the warning
    fires every cycle and hides a genuinely new code."""
    unknown: set[str] = set()
    markets = [
        {"type": t, "selections": [{"name": "x", "price": 2.0}]}
        for t in ("OUH1", "DBLC", "DNOB", "BTSC")
    ]
    assert list(parse_prematch(_prematch_payload(markets), unknown_types=unknown)) == []
    assert unknown == set()


def test_parse_prematch_maps_two_way_volleyball_winner():
    """Volleyball reuses H2HT, the same code the live feed uses for a 2-way
    winner. The prematch map is separate, so it needs its own entry."""
    market = {
        "type": "H2HT",
        "selections": [
            {"name": "Daria Snigur", "price": 1.8},
            {"name": "Lilli Tagger", "price": 2.2},
        ],
    }
    quotes = list(parse_prematch(_prematch_payload([market])))
    assert {q.outcome.label for q in quotes} == {"home", "away"}


# ---------------------------------------------------------------------------
# Freshness guard. Both feeds only advance while a browser tab is open on
# betanosports.be, so a closed tab leaves the files frozen rather than absent —
# a silent failure the daemon would otherwise price as live odds.
# ---------------------------------------------------------------------------

def _write_prematch(dirpath, sport: str, age_minutes: float) -> None:
    import os, time
    payload = {
        "data": {"blocks": [{"name": "L", "events": [{
            "id": "1", "startTime": 1785146400000,
            "participants": [{"name": "A"}, {"name": "B"}],
            "markets": [{"type": "H2HT", "selections": [
                {"name": "A", "price": 1.8}, {"name": "B", "price": 2.2}]}],
        }]}]}
    }
    p = dirpath / f"{sport}.json"
    p.write_text(json.dumps(payload))
    t = time.time() - age_minutes * 60
    os.utime(p, (t, t))


def test_fresh_prematch_file_is_used(tmp_path, monkeypatch):
    from src.main import fetch_betano_quotes

    monkeypatch.setenv("BETANO_PREMATCH_DIR", str(tmp_path))
    _write_prematch(tmp_path, "volleyball", age_minutes=1)
    assert len(fetch_betano_quotes(sport="volleyball", include_live=False)) == 2


def test_stale_prematch_file_is_refused(tmp_path, monkeypatch):
    from src.main import fetch_betano_quotes

    monkeypatch.setenv("BETANO_PREMATCH_DIR", str(tmp_path))
    monkeypatch.setenv("BETANO_PREMATCH_MAX_AGE_MIN", "30")
    _write_prematch(tmp_path, "volleyball", age_minutes=90)
    assert fetch_betano_quotes(sport="volleyball", include_live=False) == []


def test_staleness_threshold_is_configurable(tmp_path, monkeypatch):
    from src.main import fetch_betano_quotes

    monkeypatch.setenv("BETANO_PREMATCH_DIR", str(tmp_path))
    monkeypatch.setenv("BETANO_PREMATCH_MAX_AGE_MIN", "120")
    _write_prematch(tmp_path, "volleyball", age_minutes=90)
    assert len(fetch_betano_quotes(sport="volleyball", include_live=False)) == 2


def test_staleness_check_can_be_disabled(tmp_path, monkeypatch):
    """0 disables the guard — useful when replaying a captured dump offline."""
    from src.main import fetch_betano_quotes

    monkeypatch.setenv("BETANO_PREMATCH_DIR", str(tmp_path))
    monkeypatch.setenv("BETANO_PREMATCH_MAX_AGE_MIN", "0")
    _write_prematch(tmp_path, "volleyball", age_minutes=6000)
    assert len(fetch_betano_quotes(sport="volleyball", include_live=False)) == 2
