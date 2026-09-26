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


# ------------------------------------------------------------- orientation ---

def test_a_swapped_match_has_its_winner_put_back_the_right_way():
    """`match_event` apparie les deux orientations — indispensable au tennis —
    mais ne dit pas laquelle il a retenue. Sans remise d'aplomb, un pari `home`
    est noté sur le joueur que NOUS appelons `away`, et rien ne le signale : le
    score et le vainqueur restent cohérents entre eux, seul leur rattachement à
    nos noms est faux."""
    ours = [_ours("Laura Mair", "Yelyzaveta Kotliar", T)]
    # La source ordonne les joueurs dans l'autre sens, et c'est ELLE qui gagne.
    res = [_res("Yelyzaveta Kotliar", "Laura Mair", T, sport="tennis",
                winner="home", hs=12, aws=4)]

    bindings, counters = bind_results(ours, res, sport="tennis")

    assert counters["orientation_corrigee"] == 1
    _, r = bindings[0]
    # Dans NOTRE ordre, Kotliar est `away` : c'est donc elle la gagnante.
    assert r.winner == "away"
    assert r.home == "Laura Mair" and r.away == "Yelyzaveta Kotliar"
    # Les scores suivent les camps, sinon le total resterait juste mais le
    # rattachement faux.
    assert (r.home_score, r.away_score) == (4, 12)


def test_a_correctly_ordered_match_is_left_alone():
    ours = [_ours("Laura Mair", "Yelyzaveta Kotliar", T)]
    res = [_res("Laura Mair", "Yelyzaveta Kotliar", T, sport="tennis",
                winner="home", hs=12, aws=4)]
    bindings, counters = bind_results(ours, res, sport="tennis")
    assert counters["orientation_corrigee"] == 0
    _, r = bindings[0]
    assert r.winner == "home" and (r.home_score, r.away_score) == (12, 4)


def test_a_flipped_football_draw_stays_a_draw():
    """Un nul n'a pas de côté : le retourner doit le laisser intact."""
    ours = [_ours("Anderlecht", "Club Brugge", T)]
    res = [_res("Club Brugge", "Anderlecht", T, winner="draw", hs=1, aws=1)]
    bindings, counters = bind_results(ours, res, sport="soccer")
    _, r = bindings[0]
    assert r.winner == "draw"


# ------------------------------------------- orientation : noms emboîtés ---
#
# Mesuré le 27/09 : l'ancienne règle (le seul score d'appariement) stockait le
# score À L'ENVERS sur des clubs aux noms emboîtés — « Dundee » vaut 100 contre
# « Dundee United » par fragment. Ces paires sont réelles ; chacune est jouée
# dans l'ordre de la source ET à l'envers.

from src.scores import _is_swapped, _orientation  # noqa: E402

PAIRES_REELLES = [
    (("Dundee Utd", "Dundee"), ("Dundee United", "Dundee")),
    (("Dundee United", "Dundee"), ("Dundee United", "Dundee")),
    (("Inter", "Inter Miami"), ("Inter", "Inter Miami")),
    (("Sporting CP", "Sporting Braga"), ("Sporting CP", "Braga")),
    (("Man Utd", "Man City"), ("Manchester United", "Manchester City")),
    (("Real Madrid", "Atletico Madrid"), ("Real Madrid", "Atlético Madrid")),
    (("Dep. Maipu", "Atl. Tucuman"), ("Deportivo Maipu", "Atletico Tucuman")),
    (("Sinner J", "Alcaraz C"), ("Jannik Sinner", "Carlos Alcaraz")),
    (("Zverev A", "Zverev M"), ("Alexander Zverev", "Mischa Zverev")),
    (("Aberdeen B", "Elgin City"), ("Aberdeen U21", "Elgin City")),
    (("Newcastle Jets", "Newcastle United"), ("Newcastle Jets", "Newcastle")),
    (("Paris FC", "Paris Saint-Germain"), ("Paris FC", "Paris Saint Germain")),
    (("Olimpia", "Olimpija Ljubljana"), ("Olimpia", "Olimpija")),
    (("Inter Toronto", "Vancouver FC"), ("York United", "Vancouver FC")),
]


@pytest.mark.parametrize("nous, source", PAIRES_REELLES)
def test_l_orientation_n_est_jamais_fausse(nous, source):
    """Dans l'ordre de la source : « direct » ou « on ne sait pas ». À
    l'envers : « inverse » ou « on ne sait pas ». Jamais le contraire."""
    ev = _ours(*nous, T)
    assert _orientation(ev, _res(*source, T)) in ("direct", None)
    assert _orientation(ev, _res(source[1], source[0], T)) in ("inverse", None)


def test_l_ancienne_regle_retournait_ces_matchs():
    """La preuve que la correction corrige quelque chose : sur les mêmes
    paires, l'ancienne règle se trompait au moins une fois par cas cité."""
    faux = []
    for nous, source in PAIRES_REELLES:
        ev = _ours(*nous, T)
        if _is_swapped(ev, _res(*source, T)):
            faux.append((nous, "même ordre"))
        if not _is_swapped(ev, _res(source[1], source[0], T)):
            faux.append((nous, "inversée"))
    assert {n for n, _o in faux} >= {("Dundee Utd", "Dundee"),
                                     ("Dundee United", "Dundee"),
                                     ("Inter", "Inter Miami"),
                                     ("Paris FC", "Paris Saint-Germain"),
                                     ("Zverev A", "Zverev M")}


def test_dundee_utd_n_est_plus_regle_a_l_envers():
    """Même ordre, abréviation « Utd » : l'ancienne règle retournait le score
    (Dundee United vainqueur 2-1 stocké comme Dundee vainqueur). La nouvelle,
    faute de pouvoir trancher, laisse le match sans résultat."""
    ours = [_ours("Dundee Utd", "Dundee", T)]
    res = [_res("Dundee United", "Dundee", T, winner="home", hs=2, aws=1)]
    bindings, counters = bind_results(ours, res, sport="soccer")
    assert bindings == []
    assert counters["orientation_indecidable"] == 1


def test_un_derby_a_l_envers_est_remis_d_aplomb():
    """« Inter v Inter Miami » contre une source qui les inverse : l'ancienne
    règle voyait l'égalité et laissait le score à l'envers."""
    ours = [_ours("Inter", "Inter Miami", T)]
    res = [_res("Inter Miami", "Inter", T, winner="home", hs=3, aws=0)]
    bindings, counters = bind_results(ours, res, sport="soccer")
    _, r = bindings[0]
    assert (r.home, r.away) == ("Inter", "Inter Miami")
    assert (r.home_score, r.away_score, r.winner) == (0, 3, "away")
    assert counters["orientation_corrigee"] == 1


def test_un_nul_symetrique_se_regle_meme_sans_orientation():
    """1-1 reste 1-1 dans les deux sens : l'incertitude d'orientation ne doit
    pas coûter ce résultat."""
    ours = [_ours("Dundee Utd", "Dundee", T)]
    res = [_res("Dundee United", "Dundee", T, winner="draw", hs=1, aws=1)]
    bindings, counters = bind_results(ours, res, sport="soccer")
    assert len(bindings) == 1 and counters["orientation_indecidable"] == 0


def test_les_freres_zverev_ne_sont_jamais_regles_a_l_envers():
    ours = [_ours("Zverev A", "Zverev M", T)]
    for src in (("Alexander Zverev", "Mischa Zverev"),
                ("Mischa Zverev", "Alexander Zverev")):
        res = [_res(*src, T, sport="tennis", winner="home", hs=12, aws=8)]
        bindings, _c = bind_results(ours, res, sport="tennis")
        for _k, r in bindings:
            gagnant = r.home if r.winner == "home" else r.away
            assert gagnant == src[0], (src, r)


def test_un_tennis_a_jeux_egaux_n_est_pas_sans_cote():
    """7-6 4-6 7-6 : 18 jeux partout, et pourtant un vainqueur. Seul un NUL
    au score symétrique se passe d'orientation ; ce match-là, faute de savoir
    dans quel sens le lire, doit rester sans résultat."""
    ours = [_ours("Zverev A", "Zverev M", T)]
    res = [_res("Mischa Zverev", "Alexander Zverev", T, sport="tennis",
                winner="home", hs=18, aws=18)]
    bindings, counters = bind_results(ours, res, sport="tennis")
    assert bindings == [] and counters["orientation_indecidable"] == 1


# Les derbies de famille et les cas que la revue adverse du 27/09 a construits
# pour faire tomber la première version (sans marge) : dans les deux ordres,
# jamais faux — au pire, non réglé.
DERBIES = [
    (("Man Utd", "Man City"), ("Manchester United", "Manchester City"), True),
    (("Sheffield Utd", "Sheffield Wed"), ("Sheffield United", "Sheffield Wednesday"), False),
    (("Paris FC", "Paris Saint-Germain"), ("Paris FC", "Paris Saint Germain"), True),
    (("Dundee United", "Dundee"), ("Dundee United", "Dundee"), True),
    (("Inter", "Inter Miami"), ("Inter", "Inter Miami"), True),
    (("Real Madrid", "Atletico Madrid"), ("Real Madrid", "Atlético Madrid"), True),
    (("Bristol City", "Bristol Rovers"), ("Bristol City", "Bristol Rovers"), True),
    (("Club Brugge", "Cercle Brugge"), ("Club Brugge KV", "Cercle Brugge"), True),
    (("Liege", "Standard Liege"), ("RFC Liege", "Standard Liege"), True),
    (("Gimnasia LP", "Gimnasia Mendoza"), ("Gimnasia L.P.", "Gimnasia Mendoza"), True),
    (("River", "River Plate"), ("River Plate", "River Plate Montevideo"), False),
    (("Gimnasia y Esgrima", "Gimnasia Mendoza"), ("Gimnasia La Plata", "Gimnasia y Esgrima de Mendoza"), False),
    (("S. Tsitsipas", "Tsitsipas P"), ("Stefanos Tsitsipas", "Petros Tsitsipas"), False),
    (("Wang X", "Y. Wang"), ("Xiyu Wang", "Yafan Wang"), False),
]


@pytest.mark.parametrize("nous, source, doit_regler", DERBIES)
def test_les_derbies_ne_sont_jamais_regles_a_l_envers(nous, source, doit_regler):
    ev = _ours(*nous, T)
    direct = _orientation(ev, _res(*source, T))
    inverse = _orientation(ev, _res(source[1], source[0], T))
    assert direct in ("direct", None) and inverse in ("inverse", None)
    # Les grands derbies aux noms nets doivent rester RÉGLÉS : la prudence
    # ne doit pas coûter un Manchester ou un Paris.
    if doit_regler:
        assert (direct, inverse) == ("direct", "inverse")


def test_un_ecart_minuscule_ne_tranche_plus():
    """« River v River Plate » contre « River Plate v River Plate Montevideo » :
    la première version tranchait sur 8 points de `fuzz.ratio`, qui ne
    mesuraient que des longueurs de chaîne — et se trompait."""
    ours = [_ours("River", "River Plate", T)]
    res = [_res("River Plate", "River Plate Montevideo", T, winner="home", hs=2, aws=1)]
    bindings, counters = bind_results(ours, res, sport="soccer")
    assert bindings == [] and counters["orientation_indecidable"] == 1


@pytest.mark.parametrize("nous, source", [
    # Les fragments communs (« y Esgrima ») tirent vers le mauvais club : les
    # deux avis penchent à tort, l'un faiblement. Tirés du corpus de
    # calibration (appariement 23,6 / noms 28,8 ; 13,0 / 41,4).
    (("Gimnasia La Plata", "Gimnasia y Esgrima Mendoza"), ("Gimnasia y Esgrima LP", "Gimnasia M.")),
    (("Gimnasia L.P.", "Gimnasia y Esgrima Mendoza"), ("Gimnasia y Esgrima", "Gimnasia M.")),
    (("Gimnasia LP", "Gimnasia y Esgrima Mendoza"), ("Gimnasia y Esgrima", "Gimnasia M.")),
])
def test_une_famille_exige_deux_avis_nets(nous, source):
    """Entre clubs de la même famille, un avis faible ne suffit pas, même
    confirmé par l'autre : ces cas-là restent sans résultat."""
    ev = _ours(*nous, T)
    assert _orientation(ev, _res(*source, T)) in ("direct", None)
    assert _orientation(ev, _res(source[1], source[0], T)) in ("inverse", None)
