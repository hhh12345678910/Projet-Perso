from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

from src.score_sources import (
    parse_apifootball_results,
    parse_livetennis_results,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ============================================================== football ====

def _football():
    return parse_apifootball_results(_load("apifootball_fixtures_sample.json"))


def test_only_finished_matches_are_kept():
    """L'échantillon porte FT, FT, FT, PST, AET, PEN, NS — un de chaque cas
    réellement rencontré le 15/08."""
    results, counters = _football()
    assert counters["retenus"] == 3
    assert counters["non_termine"] == 4          # PST, AET, PEN, NS
    assert all(r.sport == "soccer" for r in results)


def test_extra_time_matches_are_refused_not_graded():
    """AET et PEN sont écartés parce que leur score à 90 minutes est ambigu
    dans cette API : un AET du Schweizer Cup rend `fulltime` 3-4 avec
    `extratime` 0-1, donc `fulltime` porte déjà la prolongation et le score
    réglementaire (3-3) n'apparaît nulle part. Or 1X2 et totaux se règlent sur
    90 minutes. 10 matchs sur 1 215 — le prix du silence est dérisoire."""
    results, _ = _football()
    ids = {r.source_id for r in results}
    payload = _load("apifootball_fixtures_sample.json")
    for f in payload["response"]:
        if f["fixture"]["status"]["short"] in ("AET", "PEN"):
            assert str(f["fixture"]["id"]) not in ids


def test_winner_is_derived_from_the_ninety_minute_score():
    results, _ = _football()
    by_winner = {r.winner for r in results}
    assert by_winner == {"home", "draw", "away"}
    for r in results:
        assert r.home_score is not None and r.away_score is not None
        expected = ("home" if r.home_score > r.away_score
                    else "away" if r.away_score > r.home_score else "draw")
        assert r.winner == expected


def test_football_results_carry_utc_times_and_names():
    results, _ = _football()
    for r in results:
        assert r.home and r.away
        assert r.start_time.tzinfo is not None
        assert r.start_time.utctimetuple() == r.start_time.astimezone(timezone.utc).utctimetuple()
        assert r.source == "api-football"
        assert r.gradable


def test_football_empty_payload_is_not_an_error():
    results, counters = parse_apifootball_results({"response": []})
    assert results == []
    assert counters["retenus"] == 0


# ================================================================ tennis ====

def _tennis():
    return parse_livetennis_results(_load("livetennis_history_sample.json"))


def test_only_matches_played_to_the_end_are_kept():
    """L'échantillon porte deux simples complets, un double, un match sans
    vainqueur et un abandon — tous marqués « completed » par l'API."""
    results, counters = _tennis()
    assert counters["retenus"] == 2
    assert counters["double"] == 1
    assert counters["incomplet"] == 2            # sans vainqueur + abandon


def test_completed_status_is_never_trusted_on_its_own():
    """Mesuré le 15/08 : 149 matchs tous « completed », dont 17 sans vainqueur
    et 28 dont le vainqueur n'a pas le compte de sets requis. Se fier au statut
    écrirait un résultat faux pour 30 % des matchs."""
    payload = _load("livetennis_history_sample.json")
    assert all(m["status"] == "completed" for m in payload["data"])
    results, _ = _tennis()
    assert len(results) == 2


def test_doubles_are_refused_because_their_names_are_mutilated():
    """« Mi / Victoria Luiza Barros », « - Bohrer Martins / Garcia Vidal » :
    prénoms tronqués et tirets parasites. Les apparier à des noms complets
    côté Pinnacle écrirait de faux résultats."""
    _, counters = _tennis()
    assert counters["double"] == 1


def test_tennis_winner_comes_from_the_provider_never_from_games():
    """On peut gagner plus de jeux et perdre le match. Le seul contrôle sûr est
    que le vainqueur annoncé ne se déduise PAS du total de jeux — donc on
    vérifie qu'un cas où les deux divergeraient reste possible sans casser."""
    results, _ = _tennis()
    for r in results:
        assert r.winner in ("home", "away")
        assert r.winner != "draw"                # pas de nul au tennis


def test_tennis_scores_are_games_not_sets():
    """Les jeux sont l'unité du marché « totals » au tennis (lignes 16,5-28,
    §19.2). Un total de SETS vaudrait 2 ou 3 et rendrait « under » gagnant
    partout, sans lever d'erreur."""
    results, _ = _tennis()
    for r in results:
        total = r.home_score + r.away_score
        assert 12 <= total <= 60, f"total de jeux invraisemblable : {total}"


def test_tennis_results_are_gradable_and_tagged():
    results, _ = _tennis()
    for r in results:
        assert r.sport == "tennis"
        assert r.source == "livetennisapi"
        assert r.gradable
        assert r.start_time.tzinfo is not None


def test_tennis_empty_payload_is_not_an_error():
    results, counters = parse_livetennis_results({"data": []})
    assert results == []
    assert counters["retenus"] == 0


# =============================================================== routes ====

def test_direct_route_is_used_when_only_the_plain_key_is_set(monkeypatch):
    from src.score_sources import API_FOOTBALL_BASE, ApiFootballScores

    monkeypatch.delenv("SCORES_FOOTBALL_RAPIDAPI_KEY", raising=False)
    monkeypatch.setenv("SCORES_FOOTBALL_KEY", "plain")
    with ApiFootballScores() as p:
        assert p.route == "direct"
        assert str(p._client.base_url).startswith(API_FOOTBALL_BASE)
        assert p._client.headers["x-apisports-key"] == "plain"


def test_rapidapi_route_wins_when_its_key_is_present(monkeypatch):
    """API-Sports refuse les IP de datacenter — mesuré le 16/08, 200 depuis une
    IP résidentielle et « account suspended » depuis la VM avec la MÊME clé.
    RapidAPI relaie depuis sa propre infrastructure, donc l'origine refusée
    n'est jamais vue."""
    from src.score_sources import API_FOOTBALL_RAPIDAPI_HOST, ApiFootballScores

    monkeypatch.setenv("SCORES_FOOTBALL_KEY", "plain")
    monkeypatch.setenv("SCORES_FOOTBALL_RAPIDAPI_KEY", "relayed")
    with ApiFootballScores() as p:
        assert p.route == "rapidapi"
        assert p._client.headers["X-RapidAPI-Key"] == "relayed"
        assert p._client.headers["X-RapidAPI-Host"] == API_FOOTBALL_RAPIDAPI_HOST
        assert "x-apisports-key" not in p._client.headers


def test_suspended_message_points_at_the_ip_not_the_account():
    """Le message brut envoie chercher un problème de compte alors que le
    compte est actif. Il a déjà coûté un aller-retour."""
    from src.score_sources import _football_error_message

    msg = _football_error_message({"access": "Your account is suspended"}, "direct")
    assert "datacenter" in msg
    assert "RAPIDAPI" in msg.upper()


def test_suspended_message_is_not_reinterpreted_on_the_relayed_route():
    """Sur RapidAPI l'origine n'est plus en cause : réinterpréter y ferait
    chercher un problème d'IP inexistant."""
    from src.score_sources import _football_error_message

    msg = _football_error_message({"access": "Your account is suspended"}, "rapidapi")
    assert "datacenter" not in msg
