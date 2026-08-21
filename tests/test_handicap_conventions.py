"""Conventions de ligne des handicaps — §21.13.

Le verdict de cette sonde décide si un book entre ou reste dehors. Se tromper
dans le sens permissif fabrique des paris de valeur inventés, sans qu'aucune
erreur soit levée (§21.3) — donc la classification doit être stricte, et
« deux conventions mêlées » doit valoir refus.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from unittest.mock import patch

from src.models import Book, MarketType, OddQuote, Outcome


def _q(book, label, line, ek="2030-01-01T20:00|a|b"):
    return OddQuote(event_key=ek, book=book, market=MarketType.HANDICAP,
                    outcome=Outcome(label=label, line=line), decimal_odd=1.9,
                    fetched_at=datetime.now(timezone.utc), source_event_id="1")


def _lancer(quotes, capsys):
    from scripts.handicap_conventions import main
    with patch("src.main._fetch_all_parallel", return_value=quotes), \
         patch.object(sys, "argv", ["handicap_conventions"]):
        main()
    return capsys.readouterr().out


def test_pinnacle_signe_chaque_cote(capsys):
    """Mesuré le 20/08 : 8 876 spreads sur 8 876 en lignes opposées."""
    out = _lancer([_q(Book.PINNACLE, "home", -1.0), _q(Book.PINNACLE, "away", 1.0)], capsys)
    assert "signée, alignable directement" in out
    assert "Directement alignables : pinnacle" in out


def test_un_book_miroir_est_signale_mais_pas_rejete(capsys):
    """Même |ligne| des deux côtés : récupérable, mais le signe devient une
    hypothèse sur la sémantique du book, pas une lecture."""
    out = _lancer([_q(Book.UNIBET_BE, "home", 1.0), _q(Book.UNIBET_BE, "away", 1.0)], capsys)
    assert "miroir" in out
    assert "À déduire du côté      : unibet_be" in out


def test_deux_conventions_melees_valent_REFUS(capsys):
    """Le cas dangereux : un book qui signe parfois et pas toujours.

    Lire son signe serait juste une fois sur deux, et l'erreur ne lèverait
    rien. Le refus est la seule réponse sûre.
    """
    quotes = [
        _q(Book.CIRCUS_BE, "home", -1.0, "e1"), _q(Book.CIRCUS_BE, "away", 1.0, "e1"),
        _q(Book.CIRCUS_BE, "home", 2.0, "e2"), _q(Book.CIRCUS_BE, "away", 2.0, "e2"),
    ]
    out = _lancer(quotes, capsys)
    assert "deux conventions mêlées" in out
    assert "À laisser dehors       : circus_be" in out


def test_deux_lignes_sans_rapport_sont_deux_marches_pas_une_incoherence(capsys):
    """−1,0 et +2,5 sur le même match, ce sont DEUX handicaps différents.

    C'est normal et ça ne dit rien d'une convention. Chacun n'ayant qu'un
    côté, aucun n'est classé — et le book ressort inutilisable, ce qui est le
    verdict juste : sans les deux côtés, pas de devig.
    """
    quotes = [_q(Book.LADBROKES_BE, "home", -1.0), _q(Book.LADBROKES_BE, "away", 2.5)]
    out = _lancer(quotes, capsys)
    assert "aucun couple complet" in out
    assert "Directement alignables : aucun" in out


def test_un_seul_cote_n_est_pas_classe(capsys):
    """Un côté seul ne dit rien de la convention : il ne doit pas compter
    comme une preuve."""
    out = _lancer([_q(Book.GOLDEN_PALACE, "home", -1.0)], capsys)
    assert "aucun couple complet" in out
    assert "Directement alignables : aucun" in out


def test_la_ligne_zero_est_ECARTEE_et_non_classee(capsys):
    """Un handicap 0 n'a pas de signe : c'est le même marché des deux côtés.

    Le compter comme « miroir » faisait déclarer Pinnacle « deux conventions
    mêlées » sur 941 groupes — un faux verdict de refus sur la référence même
    du projet, relevé le 21/08.
    """
    quotes = [_q(Book.BETANO_BE, "home", 0.0), _q(Book.BETANO_BE, "away", 0.0)]
    out = _lancer(quotes, capsys)
    assert "ligne 0 écartés" in out
    assert "betano_be 2" in out
    assert "conventions mêlées" not in out


def test_la_ligne_zero_ne_fait_pas_rejeter_un_book_par_ailleurs_signe(capsys):
    """Le cas exact de Pinnacle : 6 590 groupes signés et 941 de ligne 0."""
    quotes = [
        _q(Book.PINNACLE, "home", -1.0, "e1"), _q(Book.PINNACLE, "away", 1.0, "e1"),
        _q(Book.PINNACLE, "home", 0.0, "e2"), _q(Book.PINNACLE, "away", 0.0, "e2"),
    ]
    out = _lancer(quotes, capsys)
    assert "signée, alignable directement" in out
    assert "Directement alignables : pinnacle" in out


def test_un_book_sans_handicap_est_distingue_d_un_book_absent(capsys):
    """« Aucun handicap » et « pas de convention lisible » sont deux
    diagnostics opposés. Relevé le 21/08 : aucun soft n'apparaissait au
    tableau, sans qu'on sache si c'était faute de cotes ou faute de lignes."""
    quotes = [_q(Book.PINNACLE, "home", -1.0), _q(Book.PINNACLE, "away", 1.0),
              OddQuote(event_key="2030-01-01T20:00|a|b", book=Book.UNIBET_BE,
                       market=MarketType.H2H, outcome=Outcome(label="home"),
                       decimal_odd=2.0, fetched_at=datetime.now(timezone.utc),
                       source_event_id="1")]
    out = _lancer(quotes, capsys)
    assert "unibet_be" in out and "son scraper n'en mappe pas" in out


def test_aucun_handicap_le_dit_sans_conclure(capsys):
    out = _lancer([], capsys)
    assert "Aucun handicap collecté" in out
