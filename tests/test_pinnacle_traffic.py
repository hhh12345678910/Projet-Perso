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

# Tout ce fichier porte sur la COLLECTE : espacement des appels, recul,
# réutilisation du cache. Ces noms vivent dans `src.orchestration`.


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
    import src.orchestration as orch

    assert orch._PINNACLE_BACKOFF_MAX < orch._PINNACLE_MAX_REUSE, (
        f"plafond de recul {orch._PINNACLE_BACKOFF_MAX}s >= réutilisation "
        f"{orch._PINNACLE_MAX_REUSE}s : une limitation créera un trou de détection"
    )
    assert orch._PINNACLE_BACKOFF_START <= orch._PINNACLE_BACKOFF_MAX


def test_spacing_never_returns_a_silent_empty():
    """Espacement réglé au-delà de la durée de réutilisation : le cache est
    déjà mort quand l'espacement court encore. Renvoyer [] serait lu plus haut
    comme « hors-saison » — aucune alerte, aucune détection, rien dans les
    logs. Il faut retourner chercher."""
    import src.orchestration as orch

    orch._PINNACLE_CACHE.clear(); orch._PINNACLE_BLOCKED_UNTIL.clear()
    orch._PINNACLE_BACKOFF.clear(); orch._PINNACLE_SERVED_FROM_CACHE.clear()
    orch._PINNACLE_FAILED.clear(); orch._PINNACLE_EMPTY_STREAK.clear()

    sentinel = ["une-cote"]

    class _Pin:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport): return sentinel

    old_gap, orch._PINNACLE_GAP = orch._PINNACLE_GAP, 0.0
    old_min, orch._PINNACLE_MIN_INTERVAL = orch._PINNACLE_MIN_INTERVAL, 600.0
    old_reuse, orch._PINNACLE_MAX_REUSE = orch._PINNACLE_MAX_REUSE, 0.0
    old_cls, orch.PinnacleScraper = orch.PinnacleScraper, _Pin
    try:
        assert orch.fetch_pinnacle_quotes("soccer") == sentinel
        # Cache périmé d'office : le second appel doit refaire une requête
        # plutôt que renvoyer un vide muet.
        assert orch.fetch_pinnacle_quotes("soccer") == sentinel
    finally:
        orch.PinnacleScraper = old_cls
        orch._PINNACLE_GAP = old_gap
        orch._PINNACLE_MIN_INTERVAL = old_min
        orch._PINNACLE_MAX_REUSE = old_reuse
        orch._PINNACLE_CACHE.clear()


def test_backoff_resets_when_the_call_succeeds_even_if_empty():
    """Hors-saison, Pinnacle répond correctement avec zéro événement. Si le
    recul n'était remis à zéro que sur une réponse non vide, un tel sport
    gardait le recul hérité de son dernier 403 et repartait du plafond au
    refus suivant."""
    import src.orchestration as orch

    orch._PINNACLE_CACHE.clear(); orch._PINNACLE_BLOCKED_UNTIL.clear()
    orch._PINNACLE_BACKOFF.clear(); orch._PINNACLE_SERVED_FROM_CACHE.clear()
    orch._PINNACLE_FAILED.clear(); orch._PINNACLE_EMPTY_STREAK.clear()
    orch._PINNACLE_BACKOFF["hockey"] = 120.0

    class _Pin:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport): return []

    old_gap, orch._PINNACLE_GAP = orch._PINNACLE_GAP, 0.0
    old_cls, orch.PinnacleScraper = orch.PinnacleScraper, _Pin
    try:
        assert orch.fetch_pinnacle_quotes("hockey") == []
        assert "hockey" not in orch._PINNACLE_BACKOFF
        assert not orch.pinnacle_fetch_failed("hockey"), \
            "une réponse vide n'est pas une panne — voir le hockey d'août"
    finally:
        orch.PinnacleScraper = old_cls
        orch._PINNACLE_GAP = old_gap
        orch._PINNACLE_BACKOFF.clear()
        orch._PINNACLE_EMPTY_STREAK.clear()


def test_idle_probing_does_not_report_a_phantom_outage():
    """Un sport en sondage espacé ne demande rien : ce n'est ni un succès ni un
    échec. Laisser traîner le drapeau d'échec de sa dernière sonde faisait
    compter les minutes suivantes comme autant de cycles en panne, sans
    qu'aucune requête ne parte — « coupure de 33 min (52 cycles) » sur un sport
    qui n'avait aucun match à clôturer.

    Le test fixe LAST_PROBE explicitement : s'appuyer sur la valeur absolue de
    time.monotonic() rendait le résultat dépendant de l'ancienneté de la
    machine."""
    import time as _t
    import httpx
    import src.orchestration as orch

    for d in (orch._PINNACLE_CACHE, orch._PINNACLE_BLOCKED_UNTIL, orch._PINNACLE_BACKOFF,
              orch._PINNACLE_SERVED_FROM_CACHE, orch._PINNACLE_FAILED,
              orch._PINNACLE_EMPTY_STREAK, orch._PINNACLE_LAST_PROBE):
        d.clear()

    class _Pin:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport):
            raise httpx.HTTPStatusError(
                "403", request=httpx.Request("GET", "https://x"),
                response=httpx.Response(403))

    old_gap, orch._PINNACLE_GAP = orch._PINNACLE_GAP, 0.0
    old_cls, orch.PinnacleScraper = orch.PinnacleScraper, _Pin
    try:
        # Dix réponses vides : le tennis est en sondage espacé, et sa sonde
        # est due maintenant.
        orch._PINNACLE_EMPTY_STREAK["tennis"] = orch._PINNACLE_IDLE_AFTER
        orch._PINNACLE_LAST_PROBE["tennis"] = _t.monotonic() - orch._PINNACLE_IDLE_INTERVAL - 1

        assert orch.fetch_pinnacle_quotes("tennis") == []
        assert orch.pinnacle_fetch_failed("tennis") is True, "la sonde a bien échoué"

        # Les cycles suivants tombent entre deux sondages : aucune requête
        # n'est émise, donc aucune panne ne peut être constatée. C'est vrai dès
        # le cycle suivant — avant le correctif, la garde de recul s'exécutait
        # d'abord et remettait le drapeau à vrai.
        for _ in range(5):
            assert orch.fetch_pinnacle_quotes("tennis") == []
            assert not orch.pinnacle_fetch_failed("tennis"), (
                "un cycle qui ne demande rien ne doit pas compter comme une panne"
            )
    finally:
        orch.PinnacleScraper = old_cls
        orch._PINNACLE_GAP = old_gap
        for d in (orch._PINNACLE_FAILED, orch._PINNACLE_EMPTY_STREAK,
                  orch._PINNACLE_LAST_PROBE, orch._PINNACLE_BLOCKED_UNTIL,
                  orch._PINNACLE_BACKOFF):
            d.clear()


def test_a_real_outage_on_a_sport_with_fixtures_still_alerts():
    """Le garde-fou du test précédent ne doit pas masquer une vraie panne : un
    sport qui a des matchs n'entre jamais en sondage espacé, puisqu'il faut dix
    réponses vides ET réussies pour y arriver."""
    import httpx
    import src.orchestration as orch

    for d in (orch._PINNACLE_CACHE, orch._PINNACLE_BLOCKED_UNTIL, orch._PINNACLE_BACKOFF,
              orch._PINNACLE_SERVED_FROM_CACHE, orch._PINNACLE_FAILED,
              orch._PINNACLE_EMPTY_STREAK, orch._PINNACLE_LAST_PROBE):
        d.clear()

    class _Pin:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport):
            raise httpx.HTTPStatusError(
                "403", request=httpx.Request("GET", "https://x"),
                response=httpx.Response(403))

    old_gap, orch._PINNACLE_GAP = orch._PINNACLE_GAP, 0.0
    old_cls, orch.PinnacleScraper = orch.PinnacleScraper, _Pin
    try:
        assert orch.fetch_pinnacle_quotes("soccer") == []
        assert orch.pinnacle_fetch_failed("soccer") is True
    finally:
        orch.PinnacleScraper = old_cls
        orch._PINNACLE_GAP = old_gap
        for d in (orch._PINNACLE_FAILED, orch._PINNACLE_BLOCKED_UNTIL, orch._PINNACLE_BACKOFF):
            d.clear()
