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
from src.matcher import class_marker_from_league
from src.score_sources import parse_apifootball_results


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
        assert class_marker_from_league(nom) == "W", nom


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
    assert res[0].home == "Deportivo Cali W"
    assert res[0].away == "Atletico Nacional W"


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


# ---------------------------------------------------------------------------
# La correction qui compte : NOTRE côté.
#
# La première tentative a posé le marqueur sur les équipes de la SOURCE, qui
# l'avaient déjà — elle n'a rien changé, et seul le compteur `classe_reportee=0`
# l'a révélé. Les noms ci-dessous sont ceux relevés dans le fichier réel du
# 20/08 et dans notre base : ne pas les inventer, c'est tout l'intérêt.
# ---------------------------------------------------------------------------
from datetime import timedelta

from src.scores import MatchResult, OurEvent, bind_results

T = datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc)


def _leur(home, away, quand=T):
    return MatchResult(sport="soccer", home=home, away=away, start_time=quand,
                       winner="home", home_score=2.0, away_score=1.0,
                       source="api-football", source_id="1")


def test_le_feminin_etait_perdu_sans_la_ligue():
    """Sans `league`, nos noms restent « main » et la barrière rejette tout.
    C'est l'état d'avant le correctif, gardé sous test."""
    nous = [OurEvent("k", "Houston Dash", "Chicago Red Stars", T)]
    liens, c = bind_results(nous, [_leur("Houston Dash W", "Chicago Red Stars W")],
                            sport="soccer")
    assert liens == []
    assert c["sans_candidat"] == 1


def test_la_ligue_rend_le_feminin_appariable():
    """Le correctif. Noms réels des deux côtés, journée du 20/08."""
    nous = [OurEvent("k", "Houston Dash", "Chicago Red Stars", T,
                     league="USA - National Womens Soccer League")]
    liens, c = bind_results(nous, [_leur("Houston Dash W", "Chicago Red Stars W")],
                            sport="soccer")
    assert [k for k, _ in liens] == ["k"]
    assert c["classe_posee"] == 1


def test_les_jeunes_aussi():
    """`Nasjonal U19 Champions League` chez eux, équipes nues chez nous."""
    nous = [OurEvent("k", "Rosenborg", "HamKam", T,
                     league="Norway - Nasjonal U19 Champions League")]
    liens, _ = bind_results(nous, [_leur("Rosenborg U19", "HamKam U19")],
                            sport="soccer")
    assert [k for k, _ in liens] == ["k"]


def test_le_masculin_ne_vole_pas_le_resultat_du_feminin():
    """⚠️ LE test de non-régression. Rosenborg masculin et Rosenborg féminin
    jouent le même soir — c'est le cas réel du 20/08 (`Norway - Toppserien
    Women` porte « rosenborg »). Le masculin ne doit JAMAIS prendre le
    résultat du féminin, et c'est ce que la barrière protège."""
    nous = [OurEvent("m", "Rosenborg", "Lyn", T)]          # masculin, sans ligue
    liens, c = bind_results(nous, [_leur("Rosenborg W", "Lyn W")], sport="soccer")
    assert liens == []
    assert c["sans_candidat"] == 1


def test_le_feminin_ne_prend_pas_le_resultat_du_masculin():
    """Et dans l'autre sens."""
    nous = [OurEvent("f", "Rosenborg", "Lyn", T, league="Norway - Toppserien Women")]
    liens, _ = bind_results(nous, [_leur("Rosenborg", "Lyn")], sport="soccer")
    assert liens == []


def test_deux_matchs_le_meme_soir_vont_au_bon_endroit():
    """Le cas complet : masculin et féminin du même club, même soirée, les
    deux résultats disponibles. Chacun doit trouver le sien."""
    nous = [
        OurEvent("masc", "Rosenborg", "Lyn", T),
        OurEvent("fem", "Rosenborg", "Lyn", T + timedelta(minutes=5),
                 league="Norway - Toppserien Women"),
    ]
    leurs = [_leur("Rosenborg", "Lyn"),
             _leur("Rosenborg W", "Lyn W", T + timedelta(minutes=5))]
    liens, _ = bind_results(nous, leurs, sport="soccer")

    par_cle = {k: (r.home, r.away) for k, r in liens}
    assert par_cle["masc"] == ("Rosenborg", "Lyn")
    assert par_cle["fem"] == ("Rosenborg W", "Lyn W")
