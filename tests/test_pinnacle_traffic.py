"""Régulation du trafic Pinnacle.

Mesuré le 01/08 contre l'API : le football pèse 3,35 Mo compressés de
calendrier et 1,70 Mo de cotes par cycle, le tennis 0,15 Mo pour les deux.
C'est le football, et lui seul, qui se fait refuser — le limiteur compte donc
les octets, pas les requêtes. Ces tests verrouillent les trois décisions qui
en découlent, parce qu'aucune ne se voit dans les logs si elle casse.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest


_MATCHUP = {
    "id": 1,
    "startTime": "2026-06-01T20:00:00Z",
    "participants": [
        {"name": "Team A", "alignment": "home"},
        {"name": "Team B", "alignment": "away"},
    ],
    "league": {"name": "Test League"},
}
_MARKETS = [{
    "matchupId": 1,
    "period": 0,
    "status": "open",
    "type": "moneyline",
    "prices": [
        {"designation": "home", "price": -110},
        {"designation": "away", "price": +110},
    ],
}]


def _routed(calls: list[str]):
    """Stub de _get qui note le chemin appelé et renvoie la charge attendue."""
    def _get(self, path, params=None):
        calls.append(path)
        return [_MATCHUP] if path.endswith("/matchups") else list(_MARKETS)
    return _get


def test_calendar_is_cached_but_prices_are_not():
    """Le calendrier coûte deux fois le prix des cotes et ne porte aucun prix.
    Le recharger à chaque cycle achetait les deux tiers du trafic Pinnacle
    pour une information identique."""
    from src.scrapers.pinnacle import PinnacleScraper, matchups_cache_clear

    matchups_cache_clear()
    calls: list[str] = []
    with patch.object(PinnacleScraper, "_get", _routed(calls)):
        with PinnacleScraper() as pin:
            for _ in range(3):
                list(pin.fetch_market_quotes("soccer"))

    assert sum(1 for c in calls if c.endswith("/matchups")) == 1, \
        "le calendrier doit être rechargé une seule fois sur trois cycles"
    assert sum(1 for c in calls if c.endswith("/markets/straight")) == 3, \
        "les cotes doivent être rechargées à CHAQUE cycle — sinon on price du périmé"
    matchups_cache_clear()


def test_cached_calendar_still_yields_quotes():
    """Le cache retient le calendrier déjà indexé. S'il était mal rangé, les
    cycles suivants ne trouveraient plus aucun matchup et sortiraient zéro
    cote — en silence, sans la moindre erreur."""
    from src.scrapers.pinnacle import PinnacleScraper, matchups_cache_clear

    matchups_cache_clear()
    calls: list[str] = []
    with patch.object(PinnacleScraper, "_get", _routed(calls)):
        with PinnacleScraper() as pin:
            first = list(pin.fetch_market_quotes("soccer"))
            second = list(pin.fetch_market_quotes("soccer"))

    assert len(first) == 2
    assert len(second) == len(first), "un cycle servi par le cache doit sortir autant de cotes"
    matchups_cache_clear()


def test_expired_calendar_is_refetched():
    from src.scrapers import pinnacle as p
    from src.scrapers.pinnacle import PinnacleScraper, matchups_cache_clear

    matchups_cache_clear()
    calls: list[str] = []
    with patch.object(PinnacleScraper, "_get", _routed(calls)):
        with patch.object(p, "_MATCHUPS_TTL", 0.0):
            with PinnacleScraper() as pin:
                list(pin.fetch_market_quotes("soccer"))
                list(pin.fetch_market_quotes("soccer"))

    assert sum(1 for c in calls if c.endswith("/matchups")) == 2
    matchups_cache_clear()


def test_backoff_ceiling_stays_under_cache_reuse():
    """La règle de dimensionnement, et la raison des pannes de 35 min.

    Tant que le plafond de recul reste sous la durée de réutilisation du
    cache, une limitation isolée est absorbée sans aucun trou de détection.
    Dès qu'il la dépasse, le cache meurt pendant le recul et plus rien n'est
    ni détecté ni capturé — c'est ce qui a produit 60 + 120 + 240 + 480 +
    600 + 600 = 2100 s d'aveuglement pour six tentatives."""
    import src.main as m

    assert m._PINNACLE_BACKOFF_MAX < m._PINNACLE_MAX_REUSE, (
        f"plafond de recul {m._PINNACLE_BACKOFF_MAX}s >= réutilisation "
        f"{m._PINNACLE_MAX_REUSE}s : une limitation créera un trou de détection"
    )
    assert m._PINNACLE_BACKOFF_START <= m._PINNACLE_BACKOFF_MAX


def test_spacing_never_returns_a_silent_empty():
    """Espacement réglé au-delà de la durée de réutilisation : le cache est
    déjà mort quand l'espacement court encore. Renvoyer [] serait lu plus haut
    comme « hors-saison » — aucune alerte, aucune détection, rien dans les
    logs. Il faut retourner chercher."""
    import src.main as m

    m._PINNACLE_CACHE.clear(); m._PINNACLE_BLOCKED_UNTIL.clear()
    m._PINNACLE_BACKOFF.clear(); m._PINNACLE_SERVED_FROM_CACHE.clear()
    m._PINNACLE_FAILED.clear(); m._PINNACLE_EMPTY_STREAK.clear()

    sentinel = ["une-cote"]

    class _Pin:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport): return sentinel

    old_gap, m._PINNACLE_GAP = m._PINNACLE_GAP, 0.0
    old_min, m._PINNACLE_MIN_INTERVAL = m._PINNACLE_MIN_INTERVAL, 600.0
    old_reuse, m._PINNACLE_MAX_REUSE = m._PINNACLE_MAX_REUSE, 0.0
    old_cls, m.PinnacleScraper = m.PinnacleScraper, _Pin
    try:
        assert m.fetch_pinnacle_quotes("soccer") == sentinel
        # Cache périmé d'office : le second appel doit refaire une requête
        # plutôt que renvoyer un vide muet.
        assert m.fetch_pinnacle_quotes("soccer") == sentinel
    finally:
        m.PinnacleScraper = old_cls
        m._PINNACLE_GAP = old_gap
        m._PINNACLE_MIN_INTERVAL = old_min
        m._PINNACLE_MAX_REUSE = old_reuse
        m._PINNACLE_CACHE.clear()


def test_backoff_resets_when_the_call_succeeds_even_if_empty():
    """Hors-saison, Pinnacle répond correctement avec zéro événement. Si le
    recul n'était remis à zéro que sur une réponse non vide, un tel sport
    gardait le recul hérité de son dernier 403 et repartait du plafond au
    refus suivant."""
    import src.main as m

    m._PINNACLE_CACHE.clear(); m._PINNACLE_BLOCKED_UNTIL.clear()
    m._PINNACLE_BACKOFF.clear(); m._PINNACLE_SERVED_FROM_CACHE.clear()
    m._PINNACLE_FAILED.clear(); m._PINNACLE_EMPTY_STREAK.clear()
    m._PINNACLE_BACKOFF["hockey"] = 120.0

    class _Pin:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport): return []

    old_gap, m._PINNACLE_GAP = m._PINNACLE_GAP, 0.0
    old_cls, m.PinnacleScraper = m.PinnacleScraper, _Pin
    try:
        assert m.fetch_pinnacle_quotes("hockey") == []
        assert "hockey" not in m._PINNACLE_BACKOFF
        assert not m.pinnacle_fetch_failed("hockey"), \
            "une réponse vide n'est pas une panne — voir le hockey d'août"
    finally:
        m.PinnacleScraper = old_cls
        m._PINNACLE_GAP = old_gap
        m._PINNACLE_BACKOFF.clear()
        m._PINNACLE_EMPTY_STREAK.clear()
