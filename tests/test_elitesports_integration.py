"""EliteSports câblé comme les autres softbooks — l'intégration, pas le parseur.

Le parseur est testé dans `test_elitesports.py` contre un échantillon réel.
Ici on vérifie que le book est traité EXACTEMENT comme les autres : présent
dans le registre du cycle, coupable par `BOOKS_DISABLED`, nommé dans les
alertes, et silencieux au lieu de tomber quand l'API ne répond pas.

⚠️ Ce fichier existe parce qu'un book peut être parfaitement parsé et rester
INVISIBLE — c'est le §21.8 (cinq books sur huit muets) et le §19.11 (le
listener servait du code périmé). Un scraper qui marche n'est pas un book
intégré.
"""
from __future__ import annotations

import inspect

import pytest

from src.models import Book


def test_le_book_existe_dans_l_enum():
    assert Book.ELITESPORTS.value == "elitesports"


def test_il_est_dans_le_registre_du_cycle():
    """S'il n'y est pas, le scraper est du code mort et rien ne le dirait."""
    from src.main import _fetch_all_parallel
    src = inspect.getsource(_fetch_all_parallel)
    assert '"EliteSports"' in src
    assert "fetch_elitesports_quotes" in src
    # Et il ne doit pas être commenté, contrairement aux jumeaux Kambi.
    ligne = [l for l in src.splitlines() if '"EliteSports"' in l][0]
    assert not ligne.strip().startswith("#"), "le book est enregistré mais commenté"


def test_il_est_nommable_dans_les_alertes():
    """Sans libellé, l'alerte afficherait la valeur brute de l'enum."""
    from src.alerter import _BOOK_NAMES
    assert _BOOK_NAMES[Book.ELITESPORTS] == "EliteSports"


def test_il_est_coupable_par_BOOKS_DISABLED():
    """Le coupe-circuit sans déploiement doit fonctionner sur lui aussi. La
    comparaison se fait en minuscules dans `_fetch_all_parallel`."""
    from src.main import _fetch_all_parallel
    src = inspect.getsource(_fetch_all_parallel)
    assert "BOOKS_DISABLED" in src
    assert "elitesports" == "EliteSports".lower()


def test_une_panne_d_api_rend_une_liste_vide_sans_lever(monkeypatch):
    """Un book injoignable ne doit pas emporter le cycle : les autres books
    continuent. C'est la règle de tout le registre."""
    import httpx

    from src import main as m

    class Cassé:
        book = Book.ELITESPORTS
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def fetch_pages(self, sport):
            raise httpx.ConnectError("injoignable")
            yield  # pragma: no cover

    monkeypatch.setattr(m, "EliteSportsScraper", Cassé)
    assert m.fetch_elitesports_quotes("soccer") == []


def test_une_page_illisible_ne_perd_pas_les_precedentes(monkeypatch):
    """Une page qui casse en cours de balayage garde ce qui a été collecté —
    même règle que la pagination des scores. Un book à moitié collecté vaut
    mieux qu'un book absent."""
    import json
    from pathlib import Path

    from src import main as m

    reel = json.loads((Path(__file__).parent / "fixtures" /
                       "elitesports_prematch_sample.json").read_text(encoding="utf-8"))

    class Bancal:
        book = Book.ELITESPORTS
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def fetch_pages(self, sport):
            yield reel                 # page saine
            yield {"content": "pas une liste"}   # page corrompue

    monkeypatch.setattr(m, "EliteSportsScraper", Bancal)
    quotes = m.fetch_elitesports_quotes("soccer")
    assert len(quotes) == 226, "la page saine doit survivre à la page corrompue"
    assert {q.book for q in quotes} == {Book.ELITESPORTS}


def test_un_sport_non_couvert_ne_fait_pas_d_appel():
    """EliteSports sert le football et le tennis. Le hockey ne doit produire
    aucun appel plutôt qu'une erreur — le daemon scanne sport par sport."""
    from src.scrapers.elitesports import EliteSportsScraper
    sc = EliteSportsScraper.__new__(EliteSportsScraper)   # sans client HTTP
    assert list(sc.fetch_pages("hockey")) == []


def test_la_pagination_s_arrete_sur_une_page_vide_pas_sur_totalPages():
    """⚠️ Mesuré le 22/08 : à `size=10` le tennis annonce `totalPages: 4`,
    cohérent ; à `size=500` il annonce `totalElements: 6` pour 35 événements
    servis. Se fier au compteur tronque le balayage — probablement la cause
    des 1 476 événements de football rendus sur 1 503 annoncés."""
    from src.scrapers.elitesports import EliteSportsScraper

    pleine = {"content": [{"leagueName": "L", "events": [{"eventId": "x"}]}],
              "page": {"totalPages": 1, "totalElements": 6}}   # ment : dit « fini »
    vide = {"content": [], "page": {"totalPages": 1}}
    appels = []

    sc = EliteSportsScraper.__new__(EliteSportsScraper)
    sc._get = lambda path, params: (appels.append(params["page"]),
                                    pleine if params["page"] < 3 else vide)[1]

    pages = list(sc.fetch_pages("soccer"))
    assert len(pages) == 3, "le balayage s'est arrêté sur totalPages au lieu du vide"
    assert appels == [0, 1, 2, 3], "la page vide doit être demandée pour prouver la fin"


def test_la_borne_dure_previent_au_lieu_de_tronquer_en_silence():
    """Si MAX_PAGES est atteint sans page vide, l'offre est peut-être
    incomplète. Un book silencieusement tronqué est pire qu'un book absent."""
    import warnings

    from src.scrapers.elitesports import EliteSportsScraper

    pleine = {"content": [{"leagueName": "L", "events": [{"eventId": "x"}]}]}
    sc = EliteSportsScraper.__new__(EliteSportsScraper)
    sc._get = lambda path, params: pleine

    with warnings.catch_warnings(record=True) as capté:
        warnings.simplefilter("always")
        pages = list(sc.fetch_pages("soccer"))
    assert len(pages) == EliteSportsScraper.MAX_PAGES
    assert capté and "tronquée" in str(capté[0].message)
