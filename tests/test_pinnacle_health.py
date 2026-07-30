"""Une panne de Pinnacle doit se signaler, pas passer inaperçue.

Pinnacle est le point de défaillance unique : sans lui il n'y a plus de ligne
juste, donc plus de value bets — et surtout plus de captures de clôture, qui ne
se rattrapent pas une fois la purge passée. Une panne d'1 h 30 est passée
totalement silencieuse ; ces tests couvrent le garde-fou.
"""
from __future__ import annotations

import pytest

from src import main


@pytest.fixture(autouse=True)
def _reset():
    main._PINNACLE_FAILS.clear()
    main._PINNACLE_ALERTED.clear()
    yield
    main._PINNACLE_FAILS.clear()
    main._PINNACLE_ALERTED.clear()


@pytest.fixture
def sent(monkeypatch) -> list[str]:
    out: list[str] = []
    monkeypatch.setattr(main, "send_system_alert",
                        lambda cfg, text, **kw: out.append(text) or True)
    return out


def test_a_passing_failure_stays_quiet(sent):
    """Les 403 passagers de Pinnacle sont fréquents et sans gravité — alerter
    dessus rendrait l'alarme inutile."""
    for _ in range(main._PINNACLE_ALERT_AFTER - 1):
        main._pinnacle_health("soccer", ok=False, tg_cfg=object())
    assert sent == []


def test_a_lasting_outage_alerts_once(sent):
    for _ in range(main._PINNACLE_ALERT_AFTER + 4):
        main._pinnacle_health("soccer", ok=False, tg_cfg=object())
    assert len(sent) == 1, "une seule alarme par panne, pas une par cycle"
    assert "Pinnacle muet" in sent[0]


def test_recovery_is_announced(sent):
    for _ in range(main._PINNACLE_ALERT_AFTER):
        main._pinnacle_health("soccer", ok=False, tg_cfg=object())
    main._pinnacle_health("soccer", ok=True, tg_cfg=object())

    assert len(sent) == 2
    assert "rétabli" in sent[1]
    # Le compteur repart de zéro : la panne suivante réalerte.
    for _ in range(main._PINNACLE_ALERT_AFTER):
        main._pinnacle_health("soccer", ok=False, tg_cfg=object())
    assert len(sent) == 3


def test_a_recovery_without_an_outage_says_nothing(sent):
    """Un cycle sain après un échec isolé ne doit pas produire de message."""
    main._pinnacle_health("soccer", ok=False, tg_cfg=object())
    main._pinnacle_health("soccer", ok=True, tg_cfg=object())
    assert sent == []


def test_sports_are_tracked_separately(sent):
    """Le daemon boucle par sport ; une panne tennis ne doit pas être masquée
    par un cycle soccer réussi."""
    for _ in range(main._PINNACLE_ALERT_AFTER):
        main._pinnacle_health("tennis", ok=False, tg_cfg=object())
        main._pinnacle_health("soccer", ok=True, tg_cfg=object())
    assert len(sent) == 1
    assert "tennis" in sent[0]


def test_no_telegram_config_is_not_an_error(monkeypatch):
    """Sans config Telegram le daemon doit continuer, pas planter."""
    from src.alerter import send_system_alert
    assert send_system_alert(None, "test") is False


def test_quotes_reused_during_a_backoff_are_never_restored(monkeypatch):
    """Pendant une pause après 403, les dernières cotes servent encore à
    détecter — mais elles sont DÉJÀ en base. Les réinsérer dupliquerait chaque
    issue, et pinnacle_closing_group, qui devigue toutes les issues du dernier
    instantané, travaillerait sur six cotes au lieu de trois : overround
    doublé, ligne de clôture fausse, aucun signe extérieur."""
    from datetime import datetime, timezone
    import httpx
    import src.main as m
    from src.models import Book, MarketType, OddQuote, Outcome

    q = [OddQuote(event_key="202608011200::a__vs__b", book=Book.PINNACLE,
                  market=MarketType.H2H, outcome=Outcome(label="home"),
                  decimal_odd=2.0, fetched_at=datetime.now(timezone.utc),
                  source_event_id="1")]
    state = {"fail": False}

    class _Pin:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport):
            if state["fail"]:
                raise httpx.HTTPStatusError(
                    "403", request=httpx.Request("GET", "https://x"),
                    response=httpx.Response(403))
            return iter(q)

    monkeypatch.setattr(m, "PinnacleScraper", _Pin)
    monkeypatch.setattr(m, "_PINNACLE_GAP", 0.0)   # l'écartement n'est pas l'objet ici
    for d in (m._PINNACLE_CACHE, m._PINNACLE_BLOCKED_UNTIL,
              m._PINNACLE_BACKOFF, m._PINNACLE_SERVED_FROM_CACHE):
        d.clear()

    # Succès : les cotes sont neuves, donc à enregistrer.
    assert m.fetch_pinnacle_quotes("soccer") == q
    assert m.pinnacle_was_cached("soccer") is False

    # 403 : on retombe sur le cache, et il ne faut surtout pas le réenregistrer.
    state["fail"] = True
    assert m.fetch_pinnacle_quotes("soccer") == q
    assert m.pinnacle_was_cached("soccer") is True


def test_pinnacle_is_queried_every_cycle_by_default(monkeypatch):
    """Pas d'espacement imposé : seule une limitation avérée met en pause."""
    from datetime import datetime, timezone
    import src.main as m
    from src.models import Book, MarketType, OddQuote, Outcome

    calls = {"n": 0}
    q = [OddQuote(event_key="202608011200::a__vs__b", book=Book.PINNACLE,
                  market=MarketType.H2H, outcome=Outcome(label="home"),
                  decimal_odd=2.0, fetched_at=datetime.now(timezone.utc),
                  source_event_id="1")]

    class _Pin:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport):
            calls["n"] += 1
            return iter(q)

    monkeypatch.setattr(m, "PinnacleScraper", _Pin)
    monkeypatch.setattr(m, "_PINNACLE_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(m, "_PINNACLE_GAP", 0.0)
    for d in (m._PINNACLE_CACHE, m._PINNACLE_BLOCKED_UNTIL,
              m._PINNACLE_BACKOFF, m._PINNACLE_SERVED_FROM_CACHE):
        d.clear()

    for _ in range(3):
        m.fetch_pinnacle_quotes("soccer")
    assert calls["n"] == 3
    assert m.pinnacle_was_cached("soccer") is False


def test_a_403_backs_off_instead_of_retrying_every_cycle():
    """Redemander toutes les 20 s pendant une limitation la prolonge."""
    import time
    import httpx
    import src.main as m

    class _Pin:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport):
            raise httpx.HTTPStatusError(
                "403", request=httpx.Request("GET", "https://x"),
                response=httpx.Response(403))

    m._PINNACLE_CACHE.clear(); m._PINNACLE_BLOCKED_UNTIL.clear()
    m._PINNACLE_BACKOFF.clear(); m._PINNACLE_SERVED_FROM_CACHE.clear()
    old_gap, m._PINNACLE_GAP = m._PINNACLE_GAP, 0.0
    old = m.PinnacleScraper
    m.PinnacleScraper = _Pin
    try:
        assert m.fetch_pinnacle_quotes("soccer") == []
        assert m._PINNACLE_BACKOFF["soccer"] >= 60
        assert m._PINNACLE_BLOCKED_UNTIL["soccer"] > time.monotonic()
    finally:
        m.PinnacleScraper = old
        m._PINNACLE_GAP = old_gap


def test_pinnacle_calls_are_serialised_across_sports(monkeypatch):
    """Les sports sont scannés en parallèle. Sans sérialisation, chaque cycle
    part en rafale de six requêtes simultanées — ce qu'un limiteur de débit
    sanctionne bien plus durement que le même volume étalé."""
    import threading
    from datetime import datetime, timezone
    import src.main as m
    from src.models import Book, MarketType, OddQuote, Outcome

    import time

    overlap = {"max": 0, "cur": 0}
    guard = threading.Lock()

    class _Pin:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport):
            with guard:
                overlap["cur"] += 1
                overlap["max"] = max(overlap["max"], overlap["cur"])
            time.sleep(0.05)
            with guard:
                overlap["cur"] -= 1
            return iter([OddQuote(
                event_key="202608011200::a__vs__b", book=Book.PINNACLE,
                market=MarketType.H2H, outcome=Outcome(label="home"),
                decimal_odd=2.0, fetched_at=datetime.now(timezone.utc),
                source_event_id="1")])

    monkeypatch.setattr(m, "PinnacleScraper", _Pin)
    monkeypatch.setattr(m, "_PINNACLE_GAP", 0.0)
    for d in (m._PINNACLE_CACHE, m._PINNACLE_BLOCKED_UNTIL,
              m._PINNACLE_BACKOFF, m._PINNACLE_SERVED_FROM_CACHE):
        d.clear()

    threads = [threading.Thread(target=m.fetch_pinnacle_quotes, args=(s,))
               for s in ("soccer", "tennis", "hockey")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlap["max"] == 1, "deux appels Pinnacle simultanés"
