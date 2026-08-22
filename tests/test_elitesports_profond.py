"""EliteSports — le balayage PROFOND, en cache de fond.

La route globale pose une fenêtre de ~2 jours (1 480 événements du 22 au
24/08, mesuré) ; la route par ligue ne la pose pas — la Coupe d'Allemagne y va
jusqu'au 2 septembre. Balayer les 302 ligues rend **255 événements
exploitables de plus**, dont 166 au-delà de 48 h, la tranche que le §9 mesure
à +6,38 % de CLV avec 3,0 σ.

⚠️ Mais ce balayage coûte **55 secondes**. Le faire DANS le cycle silencierait
le sport entier — c'est exactement ce qui avait fait retirer Smarkets en
juillet (§5). D'où le cache de fond, motif de BetFirst : le cycle lit ce qui
est prêt et repart aussitôt.

Ce fichier garde les trois propriétés qui rendent ce motif sûr :
le cycle n'attend jamais, un cache trop vieux rend RIEN plutôt que des cotes
mortes, et la route globale l'emporte sur le cache là où les deux se
recouvrent.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

# Le balayage profond vit dans la couche de COLLECTE, pas dans la CLI. Viser
# `src.main` remplacerait des noms que ce code ne consulte plus (§21.23).
from src import orchestration as orch
from src.models import Book, MarketType, OddQuote, Outcome


@pytest.fixture(autouse=True)
def _cache_vierge():
    orch._ELITE_DEEP_CACHE.clear()
    orch._ELITE_DEEP_REFRESHING.clear()
    yield
    orch._ELITE_DEEP_CACHE.clear()
    orch._ELITE_DEEP_REFRESHING.clear()


def _q(cle="k", label="home", odd=2.0, line=None):
    return OddQuote(event_key=cle, book=Book.ELITESPORTS, market=MarketType.H2H,
                    outcome=Outcome(label=label, line=line), decimal_odd=odd,
                    fetched_at=datetime.now(timezone.utc), source_event_id="x")


def test_le_cycle_n_attend_jamais(monkeypatch):
    """LA propriété qui compte. Un cache vide rend une liste vide TOUT DE
    SUITE et lance le rafraîchissement en fond — il ne bloque pas 55 s."""
    lances = []
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: lances.append(k)})())
    t0 = time.perf_counter()
    assert orch._elitesports_deep_quotes("soccer") == []
    assert time.perf_counter() - t0 < 0.5
    assert lances, "le rafraîchissement de fond n'a pas été lancé"


def test_un_cache_trop_vieux_rend_RIEN_pas_des_cotes_mortes(monkeypatch):
    """La leçon de la garde de fraîcheur du §5 : un flux périmé traité comme
    frais fabrique des value bets contre des prix qui n'existent plus."""
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: None})())
    vieux = time.monotonic() - (orch._ELITE_DEEP_MAX_AGE + 60)
    orch._ELITE_DEEP_CACHE["soccer"] = (vieux, [_q()])
    assert orch._elitesports_deep_quotes("soccer") == []


def test_un_cache_frais_est_servi(monkeypatch):
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: None})())
    orch._ELITE_DEEP_CACHE["soccer"] = (time.monotonic(), [_q()])
    assert len(orch._elitesports_deep_quotes("soccer")) == 1


def test_un_seul_rafraichissement_a_la_fois(monkeypatch):
    """Deux threads sur le même sport doubleraient 302 appels pour écrire le
    même cache — le piège du §7."""
    lances = []
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: lances.append(1)})())
    orch._elitesports_deep_quotes("soccer")
    orch._elitesports_deep_quotes("soccer")
    assert len(lances) == 1


def test_la_route_globale_l_emporte_sur_le_cache(monkeypatch):
    """Sur un marché présent des deux côtés, le prix de MAINTENANT vaut mieux
    que celui d'il y a un quart d'heure."""
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: None})())
    orch._ELITE_DEEP_CACHE["soccer"] = (time.monotonic(),
                                     [_q("commun", odd=9.99), _q("lointain", odd=3.0)])

    class Global:
        book = Book.ELITESPORTS
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def fetch_pages(self, sport):
            yield {"_": 1}
    monkeypatch.setattr(orch, "EliteSportsScraper", Global)
    monkeypatch.setattr(orch, "elitesports_parse_prematch",
                        lambda payload, book: [_q("commun", odd=2.0)])

    quotes = orch.fetch_elitesports_quotes("soccer")
    cotes = {q.event_key: q.decimal_odd for q in quotes}
    assert cotes["commun"] == 2.0, "le cache a écrasé la cote fraîche"
    assert cotes["lointain"] == 3.0, "l'apport lointain du cache est perdu"


def test_le_coupe_circuit_desactive_tout(monkeypatch):
    """À 0, le book retombe sur sa route globale, comme avant le 22/08."""
    monkeypatch.setattr(orch, "_ELITE_DEEP_ENABLED", False)
    orch._ELITE_DEEP_CACHE["soccer"] = (time.monotonic(), [_q()])
    assert orch._elitesports_deep_quotes("soccer") == []


def test_le_rafraichissement_ne_leve_jamais(monkeypatch):
    """Thread détaché : une exception y serait perdue ET emporterait le
    drapeau, laissant le cache figé pour toujours sans que rien ne le dise."""
    class Cassé:
        def __enter__(self): raise RuntimeError("API morte")
        def __exit__(self, *a): return None
    monkeypatch.setattr(orch, "EliteSportsScraper", Cassé)
    orch._ELITE_DEEP_REFRESHING.add("soccer")
    orch._elitesports_deep_refresh("soccer")          # ne doit pas lever
    assert "soccer" not in orch._ELITE_DEEP_REFRESHING, "le drapeau reste bloqué"
