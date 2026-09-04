"""Aucun message ne doit pouvoir casser le parseur HTML de Telegram.

CE QUI EST ARRIVÉ LE 04/09
--------------------------
Les cinq formateurs interpolaient des noms venus des flux — équipes, ligues,
libellés d'issue, noms de books — DIRECTEMENT dans du HTML, avec
`parse_mode=HTML`. Un seul `&` nu suffit à faire refuser le message ENTIER par
Telegram : `400 Bad Request: can't parse entities`. Et « Brighton & Hove
Albion », « Bosnia & Herzegovina » sont des noms parfaitement ordinaires.

CE QUI A RENDU LA PANNE INVISIBLE ET PERMANENTE
-----------------------------------------------
`send_value_bet` ne marque un pari comme notifié QUE si l'envoi réussit — une
protection contre la perte d'alerte, qui se retourne ici : un pari dont le nom
casse le HTML est réessayé à chaque cycle, indéfiniment. Les envois valides,
eux, se marquent et sortent de la file. Au bout de quelques cycles la file ne
contient plus QUE des messages impossibles, et chacun paie sa pause de
`min_send_interval_s` avant d'échouer. Mesuré : 34 à 37 pauses par cycle de
soccer — 109 à 119 s — pour zéro alerte reçue.

Le cycle ralentissait, et le vrai symptôme n'était pas la lenteur : c'était le
silence.

Ces tests ne vérifient pas la mise en forme : ils vérifient que la sortie est
du HTML que Telegram accepte, quels que soient les noms qu'on y injecte.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from src.alerter import (_ht, format_clv_alert, format_middle, format_surebet,
                         format_value_bet)
from src.models import Book, MarketType, Outcome, ValueBet

NOW = datetime(2026, 5, 28, tzinfo=timezone.utc)

# Les seules balises que Telegram accepte en parse_mode=HTML.
TAGS_OK = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a",
           "code", "pre", "tg-spoiler", "blockquote", "span"}

# Des noms qui existent vraiment, et deux qui n'existent pas mais qu'un flux
# peut produire. Le `&` est le cas qui a mordu.
NOMS_HOSTILES = [
    "Brighton & Hove Albion",
    "Bosnia & Herzegovina",
    "Crvena <b>Zvezda</b>",
    "Guinée-Bissau U<19",
    "AS Roma & > Lazio",
    "Tottenham &amp; Chelsea",   # déjà échappé à la source : ne doit pas casser non plus
]


def _html_valide(msg: str) -> list[str]:
    """Rend la liste des problèmes. Vide = Telegram accepterait le message."""
    soucis = []
    # Un `&` qui n'ouvre pas une entité connue : « can't parse entities ».
    for m in re.finditer(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", msg):
        soucis.append(f"& nu à l'offset {m.start()}")
    # Un `<` qui n'ouvre pas une balise autorisée : « Unsupported start tag ».
    for m in re.finditer(r"<(/?)([^\s>/]*)", msg):
        nom = m.group(2).lower().split("/")[0]
        if nom not in TAGS_OK:
            soucis.append(f"balise interdite <{m.group(2)}> à l'offset {m.start()}")
    return soucis


def _bet(event_key: str, league: str | None = None, label: str = "home",
         ref: Book | None = None) -> ValueBet:
    return ValueBet(
        event_key=event_key, book=Book.UNIBET_BE, market=MarketType.H2H,
        outcome=Outcome(label=label, line=None), odd_taken=1.86,
        fair_prob=0.565, fair_odd=1.77, ev_pct=5.0, kelly_stake_pct=1.5,
        detected_at=NOW, league=league, reference_book=ref)


# ── Le helper lui-même ───────────────────────────────────────────────

def test_ht_echappe_les_trois_caracteres_qui_cassent():
    assert _ht("Brighton & Hove") == "Brighton &amp; Hove"
    assert _ht("U<19 > U17") == "U&lt;19 &gt; U17"


def test_ht_ne_touche_pas_aux_guillemets():
    """Le texte n'est jamais dans un attribut : `&quot;` s'afficherait tel
    quel dans le message, ce qui serait un bug visible."""
    assert _ht('Dinamo "Kiev"') == 'Dinamo "Kiev"'


def test_ht_accepte_autre_chose_qu_une_chaine():
    """Un score vient du flux et peut arriver en entier."""
    assert _ht(3) == "3"
    assert _ht(None) == "None"


# ── Les formateurs ───────────────────────────────────────────────────

@pytest.mark.parametrize("nom", NOMS_HOSTILES)
def test_value_bet_survit_a_une_ligue_hostile(nom):
    msg = format_value_bet(_bet("209906010000::brighton__vs__leeds", league=nom))
    assert _html_valide(msg) == [], msg


@pytest.mark.parametrize("nom", NOMS_HOSTILES)
def test_value_bet_survit_a_un_libelle_hostile(nom):
    msg = format_value_bet(_bet("209906010000::brighton__vs__leeds", label=nom))
    assert _html_valide(msg) == [], msg


@pytest.mark.parametrize("nom", NOMS_HOSTILES)
def test_value_bet_survit_a_une_cle_d_evenement_illisible(nom):
    """Quand `parse_event_key` échoue, la clé BRUTE part dans le message."""
    msg = format_value_bet(_bet(nom))
    assert _html_valide(msg) == [], msg


def test_value_bet_survit_a_un_nom_d_equipe_hostile(monkeypatch):
    """Le nom vient du registre des équipes, alimenté par les scrapers — donc
    par les books, donc hors de notre contrôle."""
    monkeypatch.setattr("src.alerter._prettify_team_name",
                        lambda n: "Brighton & Hove Albion")
    msg = format_value_bet(_bet("209906010000::a__vs__b"))
    assert _html_valide(msg) == [], msg
    assert "Brighton &amp; Hove Albion" in msg


def test_le_texte_reste_lisible_apres_echappement():
    """Échapper ne doit pas défigurer : l'utilisateur doit lire « & », pas
    « &amp; ». C'est Telegram qui décode — ce test verrouille la forme sur le
    fil, pas à l'écran, et sert de garde contre un double échappement."""
    msg = format_value_bet(_bet("209906010000::a__vs__b",
                                league="Bosnia & Herzegovina"))
    assert "Bosnia &amp; Herzegovina" in msg
    assert "&amp;amp;" not in msg


def test_les_balises_voulues_survivent():
    """L'échappement porte sur les VALEURS, pas sur le gabarit : les <b> du
    message doivent rester."""
    msg = format_value_bet(_bet("209906010000::a__vs__b", league="Serie A"))
    assert "<b>" in msg and "</b>" in msg


@pytest.mark.parametrize("nom", NOMS_HOSTILES)
def test_clv_survit_a_un_libelle_hostile(nom):
    row = {"event_key": "209906010000::brighton__vs__leeds",
           "book": "unibet_be", "outcome_label": nom, "line": None,
           "odd_taken": 1.86, "ev_pct": 5.0}
    msg = format_clv_alert(row, 4.2, 1.77, 30)
    assert _html_valide(msg) == [], msg


@pytest.mark.parametrize("nom", NOMS_HOSTILES)
def test_clv_survit_a_un_book_inconnu(nom):
    """`book` vient de la base ; une valeur qui n'est pas dans l'enum tombe
    dans le repli, et le repli imprimait la chaîne brute."""
    row = {"event_key": "209906010000::a__vs__b", "book": nom,
           "outcome_label": "home", "line": None, "odd_taken": 1.86,
           "ev_pct": 5.0}
    msg = format_clv_alert(row, 4.2, 1.77, 30)
    assert _html_valide(msg) == [], msg


@pytest.mark.parametrize("nom", NOMS_HOSTILES)
def test_surebet_survit_a_un_libelle_hostile(nom):
    from src.surebet import Surebet
    sb = Surebet(event_key="209906010000::brighton__vs__leeds",
                 market=MarketType.H2H, line=None,
                 legs={nom: (2.10, Book.UNIBET_BE), "away": (2.15, Book.BETFIRST)},
                 margin=0.023)
    msg = format_surebet(sb)
    assert _html_valide(msg) == [], msg


@pytest.mark.parametrize("nom", NOMS_HOSTILES)
def test_surebet_survit_a_une_cle_illisible(nom):
    from src.surebet import Surebet
    sb = Surebet(event_key=nom, market=MarketType.H2H, line=None,
                 legs={"home": (2.10, Book.UNIBET_BE),
                       "away": (2.15, Book.BETFIRST)},
                 margin=0.023)
    msg = format_surebet(sb)
    assert _html_valide(msg) == [], msg
