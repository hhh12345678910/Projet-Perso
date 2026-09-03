"""La collecte Unibet en parallèle doit rendre EXACTEMENT ce que la série rendait.

`fetch_all_events` interroge un endpoint par compétition (termKey). La boucle
était en file : mesuré le 03/09, Unibet tenait le chemin critique du cycle
52 % du temps, médiane 12,1 s mais p90 à 23,4 s — le coût vaut N × latence.

Le paralléliser est sans danger à UNE condition, et c'est elle qu'on teste ici :
**la déduplication garde le PREMIER exemplaire d'un event_id**. Fusionner dans
l'ordre d'arrivée des threads changerait donc quel exemplaire gagne, selon
quelle requête a répondu en premier ce jour-là. Ce serait un changement de
données non déterministe, déguisé en optimisation, et invisible : les deux
versions renvoient le même NOMBRE d'événements.

Les faux `fetch_listview` de ces tests dorment des durées choisies pour que
l'ordre d'ARRIVÉE soit l'INVERSE de l'ordre des termKeys. Sans le rangement par
index, ces tests échouent.
"""
from __future__ import annotations

import time

import httpx
import pytest

from src.scrapers.unibet import UnibetScraper


def _ev(eid: int, nom: str) -> dict:
    """Un événement Kambi réduit à ce que la fusion regarde."""
    return {"event": {"id": eid, "name": nom}}


class _Faux(UnibetScraper):
    """Scraper dont seules les deux requêtes réseau sont remplacées."""

    def __init__(self, payloads: dict, retards: dict | None = None,
                 explose: set | None = None):
        self._payloads = payloads
        self._retards = retards or {}
        self._explose = explose or set()
        self.appels: list = []

    def fetch_sport_term_keys(self, sport: str = "soccer") -> list[str]:
        return [k for k in self._payloads if k != ""]

    def fetch_listview(self, sport: str = "soccer", path_suffix: str = "") -> dict:
        self.appels.append(path_suffix)
        time.sleep(self._retards.get(path_suffix, 0.0))
        if path_suffix in self._explose:
            raise httpx.HTTPError("boom")
        return {"events": self._payloads[path_suffix]}


# Le même id 99 apparaît dans trois termKeys sous trois NOMS différents : c'est
# le nom du gagnant qui révèle lequel a été gardé.
PAYLOADS = {
    "":        [_ev(1, "sentinelle"), _ev(99, "PREMIER")],
    "france":  [_ev(2, "b"), _ev(99, "deuxième")],
    "italie":  [_ev(3, "c"), _ev(99, "troisième")],
    "espagne": [_ev(4, "d")],
}
# Les retards inversent l'ordre d'arrivée : "espagne" répond en premier, "" en
# dernier. Sans rangement par index, "PREMIER" perdrait.
RETARDS = {"": 0.20, "france": 0.14, "italie": 0.08, "espagne": 0.01}


def test_le_parallele_rend_exactement_ce_que_la_serie_rendait(monkeypatch):
    monkeypatch.setenv("UNIBET_PARALLEL_TERMS", "1")
    serie = _Faux(PAYLOADS, RETARDS).fetch_all_events("soccer")

    monkeypatch.setenv("UNIBET_PARALLEL_TERMS", "6")
    para = _Faux(PAYLOADS, RETARDS).fetch_all_events("soccer")

    assert para == serie, "le parallèle ne rend pas la même chose que la série"


def test_le_premier_exemplaire_d_un_id_gagne_malgre_l_ordre_d_arrivee(monkeypatch):
    """Le cœur du risque : « espagne » répond dix fois plus vite que « » ."""
    monkeypatch.setenv("UNIBET_PARALLEL_TERMS", "6")
    res = _Faux(PAYLOADS, RETARDS).fetch_all_events("soccer")
    gagnant = [e for e in res["events"] if e["event"]["id"] == 99]
    assert len(gagnant) == 1, "l'id 99 doit apparaître une seule fois"
    assert gagnant[0]["event"]["name"] == "PREMIER", (
        "c'est l'exemplaire du PREMIER termKey qui doit gagner, pas celui qui "
        "a répondu en premier")


def test_l_ordre_des_evenements_est_celui_des_termkeys(monkeypatch):
    monkeypatch.setenv("UNIBET_PARALLEL_TERMS", "6")
    res = _Faux(PAYLOADS, RETARDS).fetch_all_events("soccer")
    assert [e["event"]["id"] for e in res["events"]] == [1, 99, 2, 3, 4]


def test_un_termkey_en_erreur_ne_fait_pas_tomber_les_autres(monkeypatch):
    monkeypatch.setenv("UNIBET_PARALLEL_TERMS", "6")
    res = _Faux(PAYLOADS, RETARDS, explose={"italie"}).fetch_all_events("soccer")
    ids = [e["event"]["id"] for e in res["events"]]
    assert ids == [1, 99, 2, 4], "seul « italie » doit manquer"


def test_une_exception_non_http_ne_fait_pas_tomber_la_collecte(monkeypatch):
    """`fetch_listview` peut lever autre chose qu'une HTTPError — JSON illisible,
    tenacity à bout. En série ça emportait toute la collecte Unibet."""
    class _Pire(_Faux):
        def fetch_listview(self, sport="soccer", path_suffix=""):
            if path_suffix == "france":
                raise ValueError("JSON illisible")
            return super().fetch_listview(sport, path_suffix)

    monkeypatch.setenv("UNIBET_PARALLEL_TERMS", "6")
    res = _Pire(PAYLOADS, RETARDS).fetch_all_events("soccer")
    assert [e["event"]["id"] for e in res["events"]] == [1, 99, 3, 4]


def test_le_parallele_est_bien_plus_rapide_que_la_serie(monkeypatch):
    """Sans cette mesure, la parallélisation pourrait être inerte et tous les
    autres tests passeraient quand même."""
    retards = {k: 0.15 for k in PAYLOADS}
    monkeypatch.setenv("UNIBET_PARALLEL_TERMS", "1")
    t0 = time.monotonic(); _Faux(PAYLOADS, retards).fetch_all_events("soccer")
    serie = time.monotonic() - t0

    monkeypatch.setenv("UNIBET_PARALLEL_TERMS", "6")
    t0 = time.monotonic(); _Faux(PAYLOADS, retards).fetch_all_events("soccer")
    para = time.monotonic() - t0

    assert para < serie / 2, f"série {serie:.2f}s, parallèle {para:.2f}s"


def test_toutes_les_competitions_sont_bien_interrogees(monkeypatch):
    """Le gain ne doit pas venir d'un termKey oublié en route."""
    monkeypatch.setenv("UNIBET_PARALLEL_TERMS", "6")
    f = _Faux(PAYLOADS, RETARDS)
    f.fetch_all_events("soccer")
    assert sorted(f.appels) == sorted(PAYLOADS), "un termKey n'a pas été demandé"


@pytest.mark.parametrize("valeur", ["0", "-3", "abracadabra", "", "6.5"])
def test_un_reglage_absurde_ne_casse_pas_la_collecte(valeur, monkeypatch):
    """Un `.env` mal rempli ne doit JAMAIS faire taire un book du premium.

    La première version de ce test acceptait la `ValueError` — elle figeait
    donc le défaut au lieu de le signaler. Unibet est l'un des deux books du
    canal premium : une faute de frappe qui l'éteint serait journalisée en
    « Unibet skipped » et personne ne ferait le lien."""
    monkeypatch.setenv("UNIBET_PARALLEL_TERMS", valeur)
    res = _Faux(PAYLOADS, RETARDS).fetch_all_events("soccer")
    assert [e["event"]["id"] for e in res["events"]] == [1, 99, 2, 3, 4], (
        f"UNIBET_PARALLEL_TERMS={valeur!r} a changé ou cassé la collecte")
