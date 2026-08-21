"""Le marqueur de classe vit dans la LIGUE chez API-Football, dans l'ÉQUIPE chez nous.

Le matcher refuse — à raison — d'apparier une section féminine avec la section
masculine du même club : `team_similarity` renvoie 0.0 dès que la classe
diffère. Mais il lit la classe dans le NOM D'ÉQUIPE, et API-Football met les
femmes dans le nom de COMPÉTITION en laissant les équipes en nom de club nu.

Conséquence, mesurée le 21/08 sur la journée du 20/08 : AFC Champions League
Women 0/2, Colombia Liga Women 0/3, NWSL 0/1 — alors que la source servait bien
12, 4 et 1 matchs de ces compétitions. Le football féminin était intégralement
inappariable, sans qu'aucune erreur ne soit levée. C'est 6,1 % du flux.

⚠️ Le test qui compte est `test_le_masculin_ne_matche_toujours_pas_le_feminin` :
la correction ne doit surtout PAS rouvrir la porte que la barrière ferme.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.matcher import team_similarity
from src.score_sources import class_marker_from_league, parse_apifootball_results


def _payload(league, home, away):
    return {"response": [{
        "fixture": {"id": 1, "date": "2026-08-20T18:00:00+00:00",
                    "status": {"short": "FT"}},
        "league": {"name": league},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "score": {"fulltime": {"home": 2, "away": 1}},
    }]}


def test_les_libelles_feminins_reels_sont_reconnus():
    """Les trois compétitions qui ont réellement échoué le 20/08, plus les
    formes que la même API emploie ailleurs."""
    for nom in ("Colombia - Liga Femenina", "USA - NWSL Women",
                "AFC Women's Champions League", "Iceland - Urvalsdeild Women",
                "Frauen-Bundesliga", "Division 1 Féminine"):
        assert class_marker_from_league(nom) == "Women", nom


def test_une_ligue_masculine_ne_declenche_rien():
    """Aucun faux positif : « Women » ne doit pas se lire dans un nom qui ne
    le porte pas, sinon on casserait l'appariement masculin, 94 % du flux."""
    for nom in ("Spain - La Liga", "Poland - 3rd Liga", "Romania - Cup",
                "Club Friendlies", "Brazil - Serie C"):
        assert class_marker_from_league(nom) == "", nom


def test_le_marqueur_est_reporte_sur_les_equipes():
    res, counters = parse_apifootball_results(
        _payload("Colombia - Liga Femenina", "Deportivo Cali", "Atletico Nacional"))

    assert counters["classe_reportee"] == 1
    assert res[0].home == "Deportivo Cali Women"
    assert res[0].away == "Atletico Nacional Women"


def test_le_report_rend_le_match_appariable():
    """LE point : avant, la similarité valait 0 et le match était perdu."""
    res, _ = parse_apifootball_results(
        _payload("USA - NWSL Women", "Portland Thorns", "Angel City"))

    # Notre convention à nous, celle de Pinnacle.
    assert team_similarity("Portland Thorns (W)", res[0].home) >= 85
    # Et sans le report, c'était rigoureusement zéro.
    assert team_similarity("Portland Thorns (W)", "Portland Thorns") == 0.0


def test_le_masculin_ne_matche_toujours_pas_le_feminin():
    """La barrière existe pour empêcher qu'un « Portland Thorns » masculin
    note les paris pris sur le féminin. La correction ne doit pas l'ouvrir."""
    res, _ = parse_apifootball_results(
        _payload("USA - NWSL Women", "Portland Thorns", "Angel City"))

    assert team_similarity("Portland Thorns", res[0].home) == 0.0


def test_un_marqueur_deja_present_nest_pas_double():
    res, counters = parse_apifootball_results(
        _payload("Iceland - Urvalsdeild Women", "Valur W", "Breidablik Women"))

    assert res[0].home == "Valur W"
    assert res[0].away == "Breidablik Women"
    assert counters["classe_reportee"] == 0


def test_les_jeunes_suivent_la_meme_regle():
    """`Ukraine - U19 League` et `Norway - Nasjonal U19` étaient dans le
    fichier réel du 20/08."""
    res, _ = parse_apifootball_results(
        _payload("Ukraine - U19 League", "Dynamo Kyiv", "Shakhtar"))

    assert res[0].home == "Dynamo Kyiv U19"
    assert team_similarity("Dynamo Kyiv U19", res[0].home) >= 85
    assert team_similarity("Dynamo Kyiv", res[0].home) == 0.0


def test_le_masculin_reste_intact():
    """94 % du flux ne doit rien voir changer."""
    res, counters = parse_apifootball_results(
        _payload("Spain - La Liga", "Real Madrid", "Barcelona"))

    assert (res[0].home, res[0].away) == ("Real Madrid", "Barcelona")
    assert counters["classe_reportee"] == 0
