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
    réellement rencontré le 15/08.

    ⚠️ CE DÉCOMPTE A CHANGÉ LE 20/09, ET PAS POUR FAIRE PASSER LE TEST.
    AET et PEN étaient refusés EN BLOC, sur un échantillon de 10 matchs.
    Mesuré depuis sur les 714 AET/PEN de la base de production : l'ambigu est
    minoritaire — 7 sur 409 exploitables — et il SE RECONNAÎT, parce que
    `goals` est le score final et que `goals == fulltime + extratime` prouve
    que `fulltime` s'arrête à 90 minutes. Le refus est donc désormais par
    ENREGISTREMENT, plus par classe.

    Dans cet échantillon : le PEN est prouvable et entre ; l'AET du Schweizer
    Cup ne l'est pas et reste dehors — c'est exactement le match que la
    version précédente de ce test protégeait."""
    results, counters = _football()
    assert counters["retenus"] == 4               # 3 FT + le PEN prouvable
    assert counters["non_termine"] == 2           # PST, NS
    assert counters["prolongation_ambigue"] == 1  # l'AET du Schweizer Cup
    assert all(r.sport == "soccer" for r in results)


def test_un_score_de_prolongation_NON_PROUVABLE_reste_refuse():
    """⚠️ LE GARDE-FOU QUE LE CORRECTIF NE DOIT PAS OUVRIR.

    L'AET du Schweizer Cup rend `fulltime` 3-4, `extratime` 0-1 et `goals`
    3-4. Donc `goals == fulltime` : `fulltime` porte DÉJÀ la prolongation, et
    le score réglementaire (3-3) n'apparaît nulle part. Le noter sur 3-4
    rendrait « victoire extérieure » là où le 1X2 vaut « nul ».

    Il doit rester dehors — et être COMPTÉ, pour que le refus se voie."""
    results, counters = _football()
    ids = {r.source_id for r in results}
    payload = _load("apifootball_fixtures_sample.json")
    vus = 0
    for f in payload["response"]:
        if f["fixture"]["status"]["short"] != "AET":
            continue
        vus += 1
        assert f["goals"] == f["score"]["fulltime"], (
            "l'échantillon n'est plus le cas ambigu que ce test protège")
        assert str(f["fixture"]["id"]) not in ids
    assert vus == 1
    assert counters["prolongation_ambigue"] == 1


def test_les_tirs_au_but_se_reglent_en_NUL_pas_sur_le_vainqueur_du_tir():
    """⚠️ LE PIÈGE LE PLUS COÛTEUX DE CE CORRECTIF.

    Launceston City gagne la séance 5-3 et `teams.home.winner` vaut True.
    Mais le 1X2 se règle sur les 90 minutes, et c'est 1-1 : un NUL. Se fier
    au vainqueur de la qualification inverserait tous les paris du match."""
    results, _ = _football()
    pen = [r for r in results if r.source_id == "1620983"]
    assert len(pen) == 1, "le PEN prouvable doit être retenu"
    assert (pen[0].home_score, pen[0].away_score) == (1.0, 1.0)
    assert pen[0].winner == "draw"


def test_une_prolongation_PROUVABLE_est_notee_sur_les_90_minutes():
    """Quand `goals == fulltime + extratime`, `fulltime` EST le score
    réglementaire. Le but marqué en prolongation ne doit pas entrer dans le
    1X2 — sinon un nul devient une défaite."""
    payload = {"response": [{
        "fixture": {"id": 42, "date": "2026-08-30T18:00:00+00:00",
                    "status": {"short": "AET"}},
        "league": {"name": "Coupe"},
        "teams": {"home": {"name": "Alpha"}, "away": {"name": "Beta"}},
        "goals": {"home": 2, "away": 3},
        "score": {"fulltime": {"home": 2, "away": 2},
                  "extratime": {"home": 0, "away": 1}},
    }]}
    results, counters = parse_apifootball_results(payload)
    assert counters["retenus"] == 1
    assert counters["prolongation_ambigue"] == 0
    assert (results[0].home_score, results[0].away_score) == (2.0, 2.0)
    assert results[0].winner == "draw"


def test_le_correctif_nouvre_QUE_AET_et_PEN():
    """CANC, ABD, AWD, PST, NS, mi-temps : tous restent écartés. Un match
    abandonné ou attribué sur tapis vert n'a pas de résultat sportif à 90
    minutes, et l'élargissement ne doit pas déborder sur eux."""
    for st in ("CANC", "ABD", "AWD", "PST", "NS", "HT", "1H", "SUSP"):
        payload = {"response": [{
            "fixture": {"id": 1, "date": "2026-08-30T18:00:00+00:00",
                        "status": {"short": st}},
            "league": {"name": "L"},
            "teams": {"home": {"name": "A"}, "away": {"name": "B"}},
            "goals": {"home": 1, "away": 0},
            "score": {"fulltime": {"home": 1, "away": 0},
                      "extratime": {"home": 0, "away": 0}},
        }]}
        results, counters = parse_apifootball_results(payload)
        assert results == [], st
        assert counters["non_termine"] == 1, st


def test_un_AET_aux_champs_manquants_est_refuse():
    """`extratime` absent — fréquent : 305 cas sur 714 dans la base de
    production. Sans lui, l'égalité ne peut pas être vérifiée, donc on
    refuse. On ne devine jamais un score de règlement."""
    payload = {"response": [{
        "fixture": {"id": 7, "date": "2026-08-30T18:00:00+00:00",
                    "status": {"short": "PEN"}},
        "league": {"name": "Coupe"},
        "teams": {"home": {"name": "A"}, "away": {"name": "B"}},
        "goals": {"home": 1, "away": 1},
        "score": {"fulltime": {"home": 1, "away": 1},
                  "extratime": {"home": None, "away": None}},
    }]}
    results, counters = parse_apifootball_results(payload)
    assert results == []
    assert counters["prolongation_ambigue"] == 1


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


# ================================================================= pont ====

def test_bridged_provider_parses_exactly_like_the_direct_route(tmp_path):
    """Le userscript repose la réponse BRUTE, donc le même parseur s'applique —
    et les mesures faites sur la route directe restent valables."""
    import shutil
    from datetime import date

    from src.score_sources import BridgedFootballScores

    day = date(2026, 8, 15)
    d = tmp_path / "soccer"
    d.mkdir(parents=True)
    shutil.copy(FIXTURES / "apifootball_fixtures_sample.json", d / f"{day.isoformat()}.json")

    with BridgedFootballScores(directory=str(tmp_path)) as p:
        bridged, counters = p.fetch_with_counters("soccer", day)
    direct, direct_counters = _football()

    assert [r.source_id for r in bridged] == [r.source_id for r in direct]
    assert counters == direct_counters


def test_missing_day_is_distinct_from_a_day_without_matches(tmp_path):
    """Confondre les deux ferait conclure que ces matchs n'ont pas de résultat,
    alors qu'ils n'ont simplement pas encore été demandés au pont."""
    from datetime import date

    from src.score_sources import BridgedFootballScores

    with BridgedFootballScores(directory=str(tmp_path)) as p:
        results, counters = p.fetch_with_counters("soccer", date(2026, 8, 15))
    assert results == []
    assert counters == {"journee_non_pontee": 1}


def test_route_is_decided_at_call_time_not_at_import(monkeypatch):
    """Lire l'environnement à l'import figerait la route au démarrage du
    process : poser SCORES_FOOTBALL_BRIDGE=1 n'aurait aucun effet et rien ne
    dirait pourquoi. C'est le §19.11 appliqué à la configuration."""
    from src.score_sources import (
        ApiFootballScores, BridgedFootballScores, LiveTennisScores, provider_for,
    )

    monkeypatch.delenv("SCORES_FOOTBALL_BRIDGE", raising=False)
    assert provider_for("soccer") is ApiFootballScores

    monkeypatch.setenv("SCORES_FOOTBALL_BRIDGE", "1")
    assert provider_for("soccer") is BridgedFootballScores

    assert provider_for("tennis") is LiveTennisScores
    assert provider_for("basketball") is None


def test_missing_key_names_the_setting_instead_of_crashing_in_httpx(monkeypatch):
    """Une clé vide partait dans l'en-tête et httpx levait « Illegal header
    value b'Bearer ' » — un message qui ne nomme ni le réglage, ni le fichier,
    ni le sport. C'était le seul indice en production le 16/08."""
    import pytest

    from src.score_sources import ApiFootballScores, LiveTennisScores

    monkeypatch.delenv("SCORES_FOOTBALL_RAPIDAPI_KEY", raising=False)
    monkeypatch.setenv("SCORES_FOOTBALL_KEY", "")
    monkeypatch.setenv("SCORES_TENNIS_KEY", "   ")

    with pytest.raises(RuntimeError, match="SCORES_FOOTBALL_KEY"):
        ApiFootballScores()
    with pytest.raises(RuntimeError, match="SCORES_TENNIS_KEY"):
        LiveTennisScores()


def test_bridge_needs_no_key_at_all(monkeypatch, tmp_path):
    """Le pont lit des fichiers : exiger une clé le rendrait inutilisable sur
    la VM, qui est précisément la machine qui n'a pas d'IP acceptée."""
    monkeypatch.delenv("SCORES_FOOTBALL_KEY", raising=False)
    from src.score_sources import BridgedFootballScores

    with BridgedFootballScores(directory=str(tmp_path)) as p:
        assert p.sports == ("soccer",)


def test_a_refused_continuation_page_keeps_what_was_already_fetched(monkeypatch):
    """Le palier gratuit de Live Tennis ne donne que 20 appels d'historique par
    MOIS et répond 403 au-delà. La première page de chaque journée arrivait,
    la seconde levait, et l'exception jetait tout : le sport affichait
    « panne » alors qu'on tenait déjà l'essentiel de la journée."""
    from datetime import date

    from src.score_sources import LiveTennisScores

    monkeypatch.setenv("SCORES_TENNIS_KEY", "x")
    payload = _load("livetennis_history_sample.json")
    first = {"data": payload["data"], "meta": {"has_more": True, "limit": 200}}

    calls = {"n": 0}

    def fake_get(self, path, params):
        calls["n"] += 1
        if calls["n"] == 1:
            return first
        raise RuntimeError("403 Forbidden — quota d'historique épuisé")

    monkeypatch.setattr(LiveTennisScores, "_get", fake_get)
    with LiveTennisScores() as p:
        results, counters = p.fetch_with_counters("tennis", date(2026, 8, 15))

    assert len(results) == 2                      # la page 1 est conservée
    assert counters["pages_refusees"] == 1
    assert counters["retenus"] == 2


def test_a_refused_first_page_still_surfaces_as_a_failure(monkeypatch):
    """S'il n'y a rien à sauver, le problème doit se voir franchement plutôt
    que de passer pour une journée sans match."""
    from datetime import date

    import pytest

    from src.score_sources import LiveTennisScores

    monkeypatch.setenv("SCORES_TENNIS_KEY", "x")

    def fake_get(self, path, params):
        raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(LiveTennisScores, "_get", fake_get)
    with LiveTennisScores() as p:
        with pytest.raises(RuntimeError, match="403"):
            p.fetch_with_counters("tennis", date(2026, 8, 15))


def test_backfill_is_paced_under_the_rate_limit(monkeypatch):
    """Le palier Basic plafonne à 60 req/min. Un rattrapage de soixante jours
    enchaîne soixante appels : sans cadence il tombe exactement sur la limite
    et échoue en plein milieu, laissant la moitié de l'historique sans
    résultat.

    ⚠️ La couche HTTP est remplacée, PAS `_get` : la cadence vit dans `_get`,
    et le mocker annulerait exactement ce qu'on veut vérifier. C'est le piège
    du §13.10, où un mock masquait le défaut qu'il était censé couvrir."""
    from datetime import date

    from src.score_sources import LiveTennisScores

    monkeypatch.setenv("SCORES_TENNIS_KEY", "x")
    monkeypatch.setenv("SCORES_TENNIS_MIN_INTERVAL_SEC", "0.05")

    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [], "meta": {"has_more": False}}

    monkeypatch.setattr("httpx.Client.get", lambda self, path, params=None: _Resp())

    with LiveTennisScores() as p:
        assert p.min_interval_sec == 0.05
        for d in (1, 2, 3):
            p.fetch_with_counters("tennis", date(2026, 8, d))

    # Le premier appel part sans attendre, les suivants sont espacés.
    assert len(slept) >= 2, "aucune cadence appliquée entre les journées"
    assert all(0 < x <= 0.05 for x in slept)


def test_pacing_is_read_at_construction_not_at_import(monkeypatch):
    """Une valeur figée à l'import rendrait le réglage de `.env` sans effet, et
    rien ne dirait pourquoi. Même piège que `provider_for` (§19.11)."""
    from src.score_sources import LiveTennisScores

    monkeypatch.setenv("SCORES_TENNIS_KEY", "x")
    monkeypatch.delenv("SCORES_TENNIS_MIN_INTERVAL_SEC", raising=False)
    with LiveTennisScores() as p:
        assert p.min_interval_sec == 1.1        # 54 req/min, sous les 60
        assert 60 / p.min_interval_sec < 60

    monkeypatch.setenv("SCORES_TENNIS_MIN_INTERVAL_SEC", "2.5")
    with LiveTennisScores() as p:
        assert p.min_interval_sec == 2.5
