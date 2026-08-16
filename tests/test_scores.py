from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.matcher import event_key
from src.scores import (
    MatchResult,
    OurEvent,
    bind_results,
    tolerance_for_scores,
    winner_from_scores,
)

T = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)


def _res(home: str, away: str, ts: datetime, *, sport: str = "soccer",
         winner: str | None = None, hs: float | None = None,
         aws: float | None = None) -> MatchResult:
    return MatchResult(
        sport=sport, home=home, away=away, start_time=ts, winner=winner,
        home_score=hs, away_score=aws, source="test",
    )


def _ours(home: str, away: str, ts: datetime) -> OurEvent:
    return OurEvent(
        event_key=event_key(home, away, ts), home=home, away=away, start_time=ts,
    )


# --------------------------------------------------------------- vainqueur ---

def test_winner_derived_from_goals_in_football():
    assert winner_from_scores("soccer", 2, 1) == "home"
    assert winner_from_scores("soccer", 0, 3) == "away"
    assert winner_from_scores("soccer", 1, 1) == "draw"


def test_winner_is_never_derived_in_tennis():
    """Le piège n°2 du module : au tennis on peut gagner plus de JEUX et perdre.

    Cas concret — 6-0, 6-7, 6-7 : le joueur à domicile prend 18 jeux contre 14,
    et perd le match deux sets à un. Déduire le vainqueur du total de jeux
    noterait donc ce pari à l'envers, sans lever la moindre exception."""
    assert winner_from_scores("tennis", 18, 14) is None
    assert winner_from_scores("tennis", 12, 20) is None


def test_winner_needs_both_scores():
    assert winner_from_scores("soccer", 2, None) is None
    assert winner_from_scores("soccer", None, None) is None


# ------------------------------------------------------------ utilisabilité ---

def test_result_with_only_a_winner_can_still_grade_h2h():
    assert _res("A", "B", T, winner="home").gradable is True


def test_result_with_only_scores_is_gradable():
    assert _res("A", "B", T, hs=2, aws=1).gradable is True


def test_result_without_winner_nor_scores_is_refused():
    """Il occuperait la clé de `results` et empêcherait un meilleur relevé de
    la prendre — pire que pas de ligne du tout."""
    assert _res("A", "B", T).gradable is False


# ------------------------------------------------------------ rapprochement ---

def test_binds_a_result_to_our_event():
    ours = [_ours("Standard Liège", "Anderlecht", T)]
    res = [_res("Standard de Liege", "RSC Anderlecht", T, winner="home", hs=2, aws=1)]
    bindings, counters = bind_results(ours, res, sport="soccer")
    assert [k for k, _ in bindings] == [ours[0].event_key]
    assert counters["lies"] == 1


def test_one_result_binds_to_every_revised_key_of_the_same_match():
    """LA propriété qui fait exister ce module.

    Pinnacle révise l'horaire d'un match de tennis par pas de 15 minutes et
    chaque révision crée une `event_key` sans effacer l'ancienne — jusqu'à onze
    pour un seul match (§17.8). `results` étant clée sur `event_key`, un
    résultat écrit sous une seule d'entre elles laisserait sans P&L tous les
    paris pris sous les autres, et ils disparaîtraient de la mesure en silence.
    """
    keys = [_ours("Tallon Griekspoor", "Alex Michelsen", T + timedelta(minutes=15 * i))
            for i in range(4)]
    res = [_res("Tallon Griekspoor", "Alex Michelsen", T, sport="tennis",
                winner="home", hs=24, aws=18)]

    bindings, counters = bind_results(keys, res, sport="tennis")

    assert counters["lies"] == 4
    assert {k for k, _ in bindings} == {e.event_key for e in keys}
    # …et c'est bien le MÊME résultat qui est lié partout.
    assert len({id(r) for _, r in bindings}) == 1


def test_unmatched_event_is_counted_not_silently_dropped():
    """« La source ne couvre pas ce match » et « le rapprochement échoue »
    donnent tous deux zéro lien. Sans compteur ils sont indiscernables, et
    c'est le mode de défaillance dominant du projet (§13.12)."""
    ours = [_ours("Deportivo Cuenca", "Mushuc Runa", T)]
    bindings, counters = bind_results(ours, [], sport="soccer")
    assert bindings == []
    assert counters["sans_candidat"] == 1


def test_matched_but_unusable_result_has_its_own_counter():
    ours = [_ours("Standard Liège", "Anderlecht", T)]
    res = [_res("Standard de Liege", "RSC Anderlecht", T)]   # ni vainqueur ni score
    bindings, counters = bind_results(ours, res, sport="soccer")
    assert bindings == []
    assert counters["resultat_inutilisable"] == 1
    assert counters["sans_candidat"] == 0


def test_other_sports_results_are_ignored():
    ours = [_ours("Standard Liège", "Anderlecht", T)]
    res = [_res("Standard de Liege", "RSC Anderlecht", T, sport="tennis", winner="home")]
    bindings, counters = bind_results(ours, res, sport="soccer")
    assert bindings == []
    assert counters["sans_candidat"] == 1


def test_ambiguous_candidates_are_refused():
    """Deux candidats presque aussi bons : on refuse de deviner. Un résultat
    attribué au mauvais match noterait un pari sur le score d'un autre."""
    ours = [_ours("Manchester United", "Liverpool", T)]
    res = [
        _res("Manchester United", "Liverpool", T, winner="home", hs=2, aws=1),
        _res("Manchester United", "Liverpool", T + timedelta(minutes=2),
             winner="away", hs=0, aws=3),
    ]
    bindings, counters = bind_results(ours, res, sport="soccer")
    assert bindings == []
    assert counters["sans_candidat"] == 1


def test_home_away_swap_is_matched():
    """Les sources n'ont pas toutes la même idée du domicile, et au tennis la
    notion n'existe pas. `match_event` teste déjà les deux sens."""
    ours = [_ours("Anderlecht", "Club Brugge", T)]
    res = [_res("Club Brugge KV", "RSC Anderlecht", T, winner="home", hs=1, aws=0)]
    bindings, counters = bind_results(ours, res, sport="soccer")
    assert counters["lies"] == 1


# ---------------------------------------------------------------- fenêtres ---

def test_tennis_gets_the_wide_window_and_football_the_tight_one():
    """Reprend les tolérances de `matcher` au lieu d'en inventer : une sonde
    qui mesure d'autres réglages que la production ment (§17.7)."""
    assert tolerance_for_scores("tennis") == 12 * 60
    assert tolerance_for_scores("soccer") == 10


def test_football_result_hours_away_is_not_bound():
    ours = [_ours("Standard Liège", "Anderlecht", T)]
    res = [_res("Standard Liège", "Anderlecht", T + timedelta(hours=5),
                winner="home", hs=2, aws=1)]
    bindings, counters = bind_results(ours, res, sport="soccer")
    assert bindings == []
    assert counters["sans_candidat"] == 1


def test_tennis_result_hours_away_is_still_bound():
    """L'heure d'un match de tennis n'est qu'une estimation : le précédent
    libère le court quand il veut. Le nom reste le juge."""
    ours = [_ours("Tallon Griekspoor", "Alex Michelsen", T)]
    res = [_res("Tallon Griekspoor", "Alex Michelsen", T + timedelta(hours=5),
                sport="tennis", winner="home", hs=24, aws=18)]
    bindings, counters = bind_results(ours, res, sport="tennis")
    assert counters["lies"] == 1


# ------------------------------------------------------- notation de bout en bout ---

@pytest.mark.parametrize(
    "sport, line, hs, aws, label, expected",
    [
        # Football : buts. 2+1 = 3 > 2,5.
        ("soccer", 2.5, 2, 1, "over", "won"),
        ("soccer", 2.5, 1, 0, "over", "lost"),
        ("soccer", 3.0, 2, 1, "over", "void"),      # push, pas une perte
        # Tennis : JEUX. 24+18 = 42 — une ligne de SETS (2,5) rendrait « over »
        # gagnant à tous les coups, ce que ce test verrouille en montrant que la
        # ligne vit bien dans l'échelle des jeux.
        ("tennis", 22.5, 13, 11, "over", "won"),
        ("tennis", 22.5, 12, 10, "over", "lost"),
    ],
)
def test_scores_feed_settle_in_the_unit_of_the_totals_market(
    sport, line, hs, aws, label, expected
):
    """Le contrat entre ce module et `clv.settle()` : `home_score` porte l'unité
    que compte le marché « totals » du sport — buts au football, JEUX au tennis.

    Mettre des sets au tennis (2-0, 2-1) ferait comparer un total de jeux à un
    total de sets, donc « under » gagnant partout, sans lever d'erreur. C'est la
    confusion jeux/sets du §19.2."""
    from src.clv import settle

    r = _res("A", "B", T, sport=sport, hs=hs, aws=aws)
    assert settle("totals", label, line, r.winner, r.home_score, r.away_score) == expected
