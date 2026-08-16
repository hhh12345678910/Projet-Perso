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


def test_a_stale_dump_is_refused(tmp_path):
    """Un onglet fermé laisse le fichier intact : sans borne d'âge, ses cotes
    mortes passeraient pour fraîches, en silence (§5)."""
    import json, os, time
    from src.scrapers.magicbetting import load_pushed_quotes

    f = tmp_path / "soccer.json"
    f.write_text(json.dumps([_event()]), encoding="utf-8")
    assert load_pushed_quotes(str(f), print_fn=lambda *_: None)

    old = time.time() - 3600
    os.utime(f, (old, old))
    said = []
    assert load_pushed_quotes(str(f), print_fn=said.append) == []
    assert any("vieux de" in s for s in said), "le rejet doit être signalé"


def test_a_missing_or_broken_dump_never_raises(tmp_path):
    from src.scrapers.magicbetting import load_pushed_quotes
    assert load_pushed_quotes(str(tmp_path / "absent.json")) == []
    bad = tmp_path / "bad.json"; bad.write_text("{pas du json", encoding="utf-8")
    assert load_pushed_quotes(str(bad), print_fn=lambda *_: None) == []


def test_a_two_way_market_never_invents_a_draw():
    """Le tennis n'a pas de nul. Un nom de joueur mal orthographié doit être
    ÉCARTÉ, jamais étiqueté « draw » : cela fabriquerait une troisième issue,
    et la ligne juste serait dévigée sur une somme fausse — sans qu'aucune
    erreur n'apparaisse nulle part."""
    ev = _event(SId=1, HT="Sinner J.", AT="Alcaraz C.", StakeTypes=[
        {"Id": 1, "N": "Winner", "Stakes": [
            {"N": "Sinner J.", "F": 1.80, "A": None},
            {"N": "Alcaraz  C.", "F": 2.05, "A": None},   # espace en trop
        ]}])
    labels = [q.outcome.label for q in parse_events([ev])]
    assert labels == ["home"], f"attendu ['home'], obtenu {labels}"


def test_a_three_way_market_still_finds_the_draw():
    labels = sorted(q.outcome.label for q in parse_events([_event()])
                    if q.market is MarketType.H2H)
    assert labels == ["away", "draw", "home"]


# --- Tennis, raccordé le 16/08 sur capture réelle (SId 3, ATP Cincinnati) ----


def _tennis(**over):
    """Structure relevée sur la sonde : vainqueur sous 702, totaux sous 3.

    Les libellés sont en français ici parce que la capture a été faite avec le
    `langId` du site ; le pont de production demande du néerlandais. Le
    parseur doit lire les deux, sans jamais dépendre de la langue pour le
    vainqueur — celui-ci se reconnaît en comparant aux noms des joueurs."""
    ev = {
        "Id": 39959605, "SId": 3, "HT": "Brandon Nakashima",
        "AT": "Aleksandar Kovacevic", "D": "2026-08-17T17:00:00Z",
        "CN": "ATP Cincinnati", "HS": None, "AS": None,
        "StakeTypes": [
            {"Id": 702, "N": "Vainqueur.", "Stakes": [
                {"N": "Brandon Nakashima", "F": 1.44, "A": None},
                {"N": "Aleksandar Kovacevic", "F": 2.75, "A": None}]},
            {"Id": 3, "N": "Total des matchs", "Stakes": [
                {"N": "Au-dessus", "F": 1.90, "A": 22.5},
                {"N": "Moins", "F": 1.90, "A": 22.5}]},
        ],
    }
    ev.update(over)
    return ev


def test_tennis_winner_and_game_totals():
    qs = list(parse_events([_tennis()]))
    got = {(q.market.value, q.outcome.label, q.outcome.line): q.decimal_odd for q in qs}
    assert got == {
        ("h2h", "home", None): 1.44,
        ("h2h", "away", None): 2.75,
        ("totals", "over", 22.5): 1.90,
        ("totals", "under", 22.5): 1.90,
    }


def test_football_id_1_is_mute_in_tennis():
    """Le mapping est PAR SPORT, et c'est tout l'enjeu.

    Le football écrit son 1X2 sous l'Id 1. Si ce code restait valide en
    tennis, une réponse tennis contenant un Id 1 (« Total pair/impair », entre
    autres) serait lue comme un vainqueur de match — trois issues fabriquées
    sur un sport qui n'en a que deux, et un devig faux sans la moindre erreur."""
    ev = _tennis(StakeTypes=[
        {"Id": 1, "N": "quelque chose d'autre", "Stakes": [
            {"N": "Brandon Nakashima", "F": 1.44, "A": None},
            {"N": "Aleksandar Kovacevic", "F": 2.75, "A": None}]},
    ])
    assert list(parse_events([ev])) == []


def test_tennis_id_702_is_mute_in_football():
    """Et réciproquement : l'Id 702 ne veut rien dire en football."""
    ev = _event(StakeTypes=[
        {"Id": 702, "N": "?", "Stakes": [
            {"N": "Orlando City SC", "F": 2.12, "A": None},
            {"N": "FC Cincinnati", "F": 2.58, "A": None}]},
    ])
    assert list(parse_events([ev])) == []


def test_tournament_winner_is_not_taken():
    """Les « vainqueur » à 38 issues (401499) sont des vainqueurs de TOURNOI.

    Rien chez Pinnacle en face, et les prendre pour un vainqueur de match
    fabriquerait une ligne à 38 termes dont le devig n'aurait aucun sens."""
    ev = _tennis(StakeTypes=[
        {"Id": 401499, "N": "Vainqueur", "Stakes": [
            {"N": "Alexander Zverev", "F": 4.5, "A": None},
            {"N": "Ben Shelton", "F": 7.0, "A": None}]},
    ])
    assert list(parse_events([ev])) == []


def test_foreign_sport_is_set_aside_and_reported():
    """Le tennis poussé dans soccer.json : déjà arrivé sur Circus."""
    foreign: list = []
    qs = list(parse_events([_tennis(), _event()], expect_sport="soccer",
                           foreign=foreign))
    assert foreign == [3]
    assert qs and all(q.outcome.line in (None, 2.5) for q in qs)


def test_no_filter_keeps_both_sports():
    qs = list(parse_events([_tennis(), _event()]))
    assert len(qs) == 9        # 4 tennis + 5 football
