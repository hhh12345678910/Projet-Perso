"""Quand tenacity abandonne, c'est l'erreur D'ORIGINE qui doit remonter.

LE DÉFAUT, ET POURQUOI IL EST INVISIBLE
---------------------------------------
`@retry(...)` sans `reraise=True` ne relance pas l'échec final : tenacity
l'emballe dans une `tenacity.RetryError`, qui n'hérite PAS de
`httpx.HTTPError`. Or tous les filets du projet sont écrits pour attraper des
erreurs HTTP — à commencer par celui de `fetch_all_meetings`, qui promet dans
son propre docstring « Best-effort: HTTP errors on a single meeting are
skipped ».

Cette promesse était fausse dès que les trois tentatives s'épuisaient. Une
compétition sur quatre-vingts qui renvoie 429 trois fois de suite faisait
tomber les quatre-vingts, en emportant celles déjà collectées — et le journal
n'écrivait qu'un « Ladbrokes 48.0s skipped: RetryError[...] », sans jamais
nommer la requête fautive.

⚠️ CE N'EST PAS UNE HYPOTHÈSE. Le projet l'a déjà vécu sur Pinnacle : un 503
de maintenance traversait `fetch_pinnacle_quotes` sans poser le drapeau
d'échec, et le cycle concluait « Pinnacle sans événement (hors-saison ?) » —
sur du football, un 4 août. Voir `orchestration._unwrap_retry`, écrit pour
réparer ça au point d'appel. `reraise=True` le règle à la source, et les deux
se complètent sans se gêner : une exception qui n'est pas une `RetryError`
traverse `_unwrap_retry` inchangée.
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
import tenacity

from src.scrapers.ladbrokes import LadbrokesScraper

RACINE = Path(__file__).resolve().parents[1]
SCRAPERS = sorted((RACINE / "src" / "scrapers").glob("*.py"))


# ── Le garde-fou structurel ──────────────────────────────────────────

def _blocs_retry(source: str) -> list[str]:
    """Le contenu de chaque `@retry(...)`, quelle que soit sa mise en page.

    ⚠️ UNE EXPRESSION RÉGULIÈRE ANCRÉE SUR « \\n    ) » NE SUFFIT PAS, et
    c'est une leçon payée : quatre des quatorze scrapers écrivaient leur
    décorateur sur UNE SEULE ligne. Le motif ne les voyait pas, donc le test
    les déclarait conformes alors qu'ils portaient exactement le défaut.
    Un garde-fou aveugle là où le défaut se trouve ne garde rien.

    On compte donc les parenthèses, ce qui marche pour les deux formes."""
    blocs, i = [], 0
    while (i := source.find("@retry(", i)) != -1:
        debut = i + len("@retry(")
        profondeur, j = 1, debut
        while j < len(source) and profondeur:
            profondeur += {"(": 1, ")": -1}.get(source[j], 0)
            j += 1
        blocs.append(source[debut:j - 1])
        i = j
    return blocs


def test_le_garde_fou_voit_les_DEUX_mises_en_page():
    """Le garde-fou ci-dessous ne vaut que s'il voit la forme compacte. Sans
    ce test, sa cécité serait elle-même silencieuse."""
    compact = "    @retry(stop=stop_after_attempt(3), wait=wait_exponential(max=8))\n"
    etale = "    @retry(\n        stop=stop_after_attempt(3),\n        reraise=True,\n    )\n"
    assert len(_blocs_retry(compact)) == 1
    assert "reraise" not in _blocs_retry(compact)[0]
    assert len(_blocs_retry(etale)) == 1
    assert "reraise=True" in _blocs_retry(etale)[0]



@pytest.mark.parametrize("fichier", [f for f in SCRAPERS
                                     if "@retry" in f.read_text(encoding="utf-8")],
                         ids=lambda f: f.stem)
def test_chaque_retry_porte_reraise(fichier):
    """⚠️ CE TEST EXISTE POUR QUE LE DÉFAUT NE REVIENNE PAS. Un scraper ajouté
    demain en recopiant le décorateur d'un voisin hériterait du trou sans que
    rien ne le signale : le book marcherait parfaitement jusqu'au jour où il
    se fait limiter, et perdrait alors tout son lot d'un coup."""
    source = fichier.read_text(encoding="utf-8")
    blocs = _blocs_retry(source)
    assert blocs, f"{fichier.name} : aucun @retry trouvé alors qu'il en contient"
    for bloc in blocs:
        assert "reraise=True" in bloc, (
            f"{fichier.name} : un @retry sans reraise=True — l'échec final "
            f"remontera en RetryError et traversera les except httpx.HTTPError\n"
            f"  {bloc[:120]}")


# ── Le comportement, prouvé sur le scraper où ça mordait ─────────────

class _Client:
    """Un client HTTP qui renvoie 429 pour certains chemins et 200 sinon."""

    def __init__(self, en_panne: tuple[str, ...], charges: dict):
        self.en_panne, self.charges, self.appels = en_panne, charges, []

    def get(self, url, params=None):
        self.appels.append(url)
        requete = httpx.Request("GET", url)
        if any(p in url for p in self.en_panne):
            return httpx.Response(429, request=requete)
        for cle, charge in self.charges.items():
            if cle in url:
                return httpx.Response(200, json=charge, request=requete)
        return httpx.Response(200, json={}, request=requete)

    def close(self):
        pass


@pytest.fixture
def sans_attente(monkeypatch):
    """Les trois tentatives, sans les 3 s de recul exponentiel."""
    monkeypatch.setattr(LadbrokesScraper._get.retry, "wait",
                        tenacity.wait_fixed(0))


def test_un_429_epuise_remonte_en_HTTPStatusError_pas_en_RetryError(sans_attente):
    """Le cœur du correctif : le TYPE de ce qui remonte."""
    lb = LadbrokesScraper()
    lb._client = _Client(en_panne=("prematch-menu",), charges={})
    with pytest.raises(httpx.HTTPStatusError) as e:
        lb.fetch_prematch_menu()
    assert e.value.response.status_code == 429
    assert not isinstance(e.value, tenacity.RetryError)


def _menu_a_deux_competitions() -> dict:
    return {"result": {"sportList": [{
        "description": "FOOTBALL", "aliasUrl": "football",
        "itemList": [
            {"itemType": "meeting", "aliasUrl": "serie-a", "eventsNr": 10},
            {"itemType": "meeting", "aliasUrl": "serie-d", "eventsNr": 8},
        ]}]}}


def _competition(nom: str) -> dict:
    return {"result": {"dataGroupList": [
        {"itemList": [{"eventId": nom, "nom": nom}]}]}}


def test_une_competition_en_panne_NE_FAIT_PLUS_TOMBER_LES_AUTRES(sans_attente):
    """⚠️ LE TEST QUI COMPTE. « serie-d » renvoie 429 à chaque tentative ;
    « serie-a » répond normalement. Avant le correctif, la `RetryError` de
    « serie-d » traversait le `except httpx.HTTPError` de la boucle et
    emportait « serie-a » avec elle. Le filet doit maintenant tenir."""
    lb = LadbrokesScraper()
    lb._client = _Client(
        en_panne=("serie-d",),
        charges={"prematch-menu": _menu_a_deux_competitions(),
                 "serie-a": _competition("A")})
    data = lb.fetch_all_meetings("soccer")
    evenements = data["result"]["events"]
    assert [e["nom"] for e in evenements] == ["A"], (
        "la compétition saine doit survivre à la panne de sa voisine")


def test_le_menu_en_panne_reste_une_perte_TOTALE_et_visible(sans_attente):
    """L'autre moitié du contrat. Le menu est hors de la boucle : son échec
    DOIT tout arrêter, et il doit le faire avec une erreur que le garde-fou
    de `fetch_ladbrokes_quotes` reconnaît — sans quoi le book ressortirait
    en `except Exception` générique, sans message utile."""
    lb = LadbrokesScraper()
    lb._client = _Client(en_panne=("prematch-menu",), charges={})
    with pytest.raises(httpx.HTTPError):
        lb.fetch_all_meetings("soccer")


def test_une_erreur_NON_retryable_ne_perd_pas_son_type(sans_attente):
    """Un 404 n'est pas dans le prédicat de retry : il doit remonter du
    premier coup, tel quel, et n'être tenté qu'UNE fois."""
    lb = LadbrokesScraper()
    client = _Client(en_panne=(), charges={})
    client.get = lambda url, params=None: httpx.Response(
        404, request=httpx.Request("GET", url)) or None
    lb._client = client
    with pytest.raises(httpx.HTTPStatusError) as e:
        lb.fetch_prematch_menu()
    assert e.value.response.status_code == 404


# ── La non-régression du chemin Pinnacle ─────────────────────────────

def test_unwrap_retry_laisse_passer_une_exception_NUE(sans_attente):
    """⚠️ `reraise=True` rend `_unwrap_retry` inutile en production, mais ne
    doit pas le casser : c'est lui qui trie les 403/429/5xx de Pinnacle pour
    décider du recul. Une `HTTPStatusError` nue doit le traverser INCHANGÉE,
    sinon le recul ne se déclenche plus."""
    from src.orchestration import _unwrap_retry
    brute = httpx.HTTPStatusError(
        "429", request=httpx.Request("GET", "https://x/"),
        response=httpx.Response(429, request=httpx.Request("GET", "https://x/")))
    assert _unwrap_retry(brute) is brute


def test_unwrap_retry_sait_toujours_deballer_une_RetryError():
    """Il reste le filet de sécurité pour tout `@retry` qui n'aurait pas —
    ou plus — son `reraise`."""
    from src.orchestration import _unwrap_retry

    @tenacity.retry(stop=tenacity.stop_after_attempt(2),
                    wait=tenacity.wait_fixed(0))
    def echoue():
        raise httpx.HTTPStatusError(
            "503", request=httpx.Request("GET", "https://x/"),
            response=httpx.Response(503, request=httpx.Request("GET", "https://x/")))

    with pytest.raises(tenacity.RetryError) as e:
        echoue()
    interne = _unwrap_retry(e.value)
    assert isinstance(interne, httpx.HTTPStatusError)
    assert interne.response.status_code == 503
