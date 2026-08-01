"""Classification des noms de ligue Pinnacle.

Le nom brut reste stocké tel quel : cette catégorie n'est qu'une vue dérivée,
révisable. Ces tests fixent les cas qui se trompent facilement — précédence
entre catégories, et surtout les noms de compétition que plusieurs pays
partagent.
"""
from __future__ import annotations

import pytest

from src.leagues import (
    CUP, D2, D3, EAST, FRIENDLY, OTHER, SCANDINAVIA, SOUTH_AMERICA, TOP5,
    WOMEN, YOUTH, categorize,
)


@pytest.mark.parametrize("name,expected", [
    ("England - Premier League", TOP5),
    ("Spain - La Liga", TOP5),
    ("Italy - Serie A", TOP5),
    ("Germany - Bundesliga", TOP5),
    ("France - Ligue 1", TOP5),
])
def test_top5(name, expected):
    assert categorize(name) == expected


@pytest.mark.parametrize("name", [
    "Brazil - Serie A",          # même nom, autre pays
    "Russia - Premier League",
    "Austria - Bundesliga",
    "Portugal - Primera Division",
])
def test_same_competition_name_in_another_country_is_not_top5(name):
    """Le piège principal : « Serie A » existe au Brésil, « Bundesliga » en
    Autriche, « Premier League » en Russie. Les compter comme top 5 mélangerait
    des marchés qui n'ont rien à voir en liquidité."""
    assert categorize(name) != TOP5


@pytest.mark.parametrize("name,expected", [
    ("England - Championship", D2),
    ("Germany - 2. Bundesliga", D2),
    ("France - Ligue 2", D2),
    ("Sweden - Superettan", D2),
    ("England - League One", D3),
    ("Germany - 3. Liga", D3),
    ("Italy - Serie C", D3),
])
def test_tiers(name, expected):
    assert categorize(name) == expected


@pytest.mark.parametrize("name,expected", [
    ("Sweden - Allsvenskan", SCANDINAVIA),
    ("Norway - Eliteserien", SCANDINAVIA),
    ("Finland - Veikkausliiga", SCANDINAVIA),
    ("Poland - Ekstraklasa", EAST),
    ("Serbia - Superliga Srbije", EAST),
    ("Brazil - Brasileirao", SOUTH_AMERICA),
    ("Argentina - Liga Profesional", SOUTH_AMERICA),
])
def test_regions(name, expected):
    assert categorize(name) == expected


@pytest.mark.parametrize("name,expected", [
    ("UEFA Champions League", CUP),
    ("England - FA Cup", CUP),
    ("Copa Libertadores", CUP),
    ("Germany - DFB Pokal", CUP),
])
def test_cups(name, expected):
    assert categorize(name) == expected


def test_precedence_friendly_beats_everything():
    """Un amical entre deux clubs de top 5 reste un amical : effectifs
    remaniés, motivation absente, liquidité faible. Le classer en top 5
    polluerait la catégorie la plus fiable avec la moins fiable."""
    assert categorize("Club Friendlies") == FRIENDLY
    assert categorize("England - Premier League Friendly") == FRIENDLY


def test_precedence_women_beats_tier_and_region():
    assert categorize("Sweden - Damallsvenskan Women") == WOMEN
    assert categorize("England - Women's Championship") == WOMEN


def test_precedence_youth_beats_cup():
    assert categorize("UEFA Youth League") == YOUTH
    assert categorize("Italy - Primavera Cup") == YOUTH


def test_precedence_cup_beats_tier():
    """Une coupe mélange les divisions : la classer par le niveau d'une des
    deux équipes n'aurait pas de sens."""
    assert categorize("England - Championship Cup") == CUP


def test_unknown_and_empty():
    assert categorize("") == OTHER
    assert categorize(None) == OTHER
    assert categorize("Ligue Inconnue Du Bout Du Monde") == OTHER


def test_accents_and_separators():
    """Pinnacle mélange « - », « – » et les accents selon les compétitions."""
    assert categorize("Suède – Allsvenskan") == SCANDINAVIA
    assert categorize("FRANCE  --  LIGUE 1") == TOP5
