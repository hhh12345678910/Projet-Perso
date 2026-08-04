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


@pytest.fixture
def clock(monkeypatch):
    """Horloge pilotée : le seuil est une durée, pas un nombre de cycles."""
    state = {"t": 1000.0}
    monkeypatch.setattr(main.time, "monotonic", lambda: state["t"])
    main._PINNACLE_DOWN_SINCE.clear()
    return state


def _down(minutes: float, clock, sport="soccer", step=0.5):
    """Simule `minutes` de panne, un cycle toutes les `step` minutes."""
    elapsed = 0.0
    while elapsed <= minutes:
        main._pinnacle_health(sport, ok=False, tg_cfg=object())
        clock["t"] += step * 60
        elapsed += step


def test_a_passing_failure_stays_quiet(sent, clock):
    """Les coupures courtes sont désormais attendues : le recul après 403 les
    absorbe seul. Alerter dessus remplissait le canal critique toute la nuit,
    ce qui apprend à l'ignorer et lui retire toute valeur."""
    _down(main._PINNACLE_ALERT_AFTER_MIN - 2, clock)
    assert sent == []


def test_a_lasting_outage_alerts_once(sent, clock):
    _down(main._PINNACLE_ALERT_AFTER_MIN + 10, clock)
    assert len(sent) == 1, "une seule alarme par panne, pas une par cycle"
    assert "Pinnacle muet" in sent[0]


def test_recovery_carries_the_real_duration(sent, clock):
    _down(main._PINNACLE_ALERT_AFTER_MIN + 5, clock)
    main._pinnacle_health("soccer", ok=True, tg_cfg=object())

    assert len(sent) == 2
    assert "rétabli" in sent[1]
    # La durée permet de juger si des clôtures ont été perdues.
    assert "min" in sent[1] or " h " in sent[1]

    # L'état repart de zéro : la panne suivante réalerte.
    _down(main._PINNACLE_ALERT_AFTER_MIN + 1, clock)
    assert len(sent) == 3


def test_a_recovery_without_an_outage_says_nothing(sent, clock):
    """Un cycle sain après un échec isolé ne doit pas produire de message."""
    main._pinnacle_health("soccer", ok=False, tg_cfg=object())
    main._pinnacle_health("soccer", ok=True, tg_cfg=object())
    assert sent == []


def test_sports_are_tracked_separately(sent, clock):
    """Le daemon boucle par sport ; une panne tennis ne doit pas être masquée
    par un cycle soccer réussi."""
    elapsed = 0.0
    while elapsed <= main._PINNACLE_ALERT_AFTER_MIN + 1:
        main._pinnacle_health("tennis", ok=False, tg_cfg=object())
        main._pinnacle_health("soccer", ok=True, tg_cfg=object())
        clock["t"] += 30
        elapsed += 0.5
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
        assert m._PINNACLE_BACKOFF["soccer"] >= m._PINNACLE_BACKOFF_START
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


def test_an_empty_response_is_not_an_outage(monkeypatch):
    """Hors-saison, Pinnacle répond correctement avec zéro événement : en août
    il n'y a pas un seul match de hockey. Confondre ça avec une panne faisait
    alerter en permanence sur les sports sans calendrier — l'essentiel du bruit
    relevé une nuit entière sur le canal critique."""
    import src.main as m

    class _Empty:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport):
            return iter([])

    monkeypatch.setattr(m, "PinnacleScraper", _Empty)
    monkeypatch.setattr(m, "_PINNACLE_GAP", 0.0)
    for d in (m._PINNACLE_CACHE, m._PINNACLE_BLOCKED_UNTIL, m._PINNACLE_BACKOFF,
              m._PINNACLE_SERVED_FROM_CACHE, m._PINNACLE_FAILED):
        d.clear()

    assert m.fetch_pinnacle_quotes("hockey") == []
    assert m.pinnacle_fetch_failed("hockey") is False


def test_a_403_is_an_outage(monkeypatch):
    import httpx
    import src.main as m

    class _Blocked:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport):
            raise httpx.HTTPStatusError(
                "403", request=httpx.Request("GET", "https://x"),
                response=httpx.Response(403))

    monkeypatch.setattr(m, "PinnacleScraper", _Blocked)
    monkeypatch.setattr(m, "_PINNACLE_GAP", 0.0)
    for d in (m._PINNACLE_CACHE, m._PINNACLE_BLOCKED_UNTIL, m._PINNACLE_BACKOFF,
              m._PINNACLE_SERVED_FROM_CACHE, m._PINNACLE_FAILED):
        d.clear()

    assert m.fetch_pinnacle_quotes("soccer") == []
    assert m.pinnacle_fetch_failed("soccer") is True


def _raising(exc):
    class _Boom:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport):
            raise exc
    return _Boom


def _clean(m):
    for d in (m._PINNACLE_CACHE, m._PINNACLE_BLOCKED_UNTIL, m._PINNACLE_BACKOFF,
              m._PINNACLE_SERVED_FROM_CACHE, m._PINNACLE_FAILED,
              m._PINNACLE_EMPTY_STREAK, m._PINNACLE_LAST_PROBE):
        d.clear()


def test_a_maintenance_503_is_an_outage_not_an_empty_calendar(monkeypatch):
    """Panne vécue le 04/08 : Pinnacle a répondu 503 « MAINTENANCE » pendant
    des heures, sur football ET tennis. tenacity ré-emballe l'échec final dans
    RetryError, qui n'est pas une HTTPStatusError — le tri par code HTTP ne la
    voyait donc jamais, le drapeau d'échec restait faux, et le cycle concluait
    « Pinnacle sans événement (hors-saison ?) » sur du football un 4 août.

    Conséquence : aucune alerte « Pinnacle muet », et la panne totale du
    système s'est découverte à l'absence de notifications de value."""
    import httpx
    from tenacity import RetryError
    import src.main as m

    inner = httpx.HTTPStatusError(
        "503", request=httpx.Request("GET", "https://x"),
        response=httpx.Response(503, text='{"title": "MAINTENANCE"}'))

    class _Attempt:
        def exception(self): return inner

    monkeypatch.setattr(m, "PinnacleScraper", _raising(RetryError(_Attempt())))
    monkeypatch.setattr(m, "_PINNACLE_GAP", 0.0)
    _clean(m)

    assert m.fetch_pinnacle_quotes("soccer") == []
    assert m.pinnacle_fetch_failed("soccer") is True, \
        "un 503 est une panne, pas un calendrier vide"
    # Et il doit reculer : marteler une API en maintenance ne sert à rien.
    assert m._PINNACLE_BACKOFF.get("soccer", 0) > 0


def test_a_retry_wrapped_403_still_backs_off(monkeypatch):
    """Le déballage doit servir tous les codes, pas seulement le 503."""
    import httpx
    from tenacity import RetryError
    import src.main as m

    inner = httpx.HTTPStatusError(
        "403", request=httpx.Request("GET", "https://x"),
        response=httpx.Response(403))

    class _Attempt:
        def exception(self): return inner

    monkeypatch.setattr(m, "PinnacleScraper", _raising(RetryError(_Attempt())))
    monkeypatch.setattr(m, "_PINNACLE_GAP", 0.0)
    _clean(m)

    assert m.fetch_pinnacle_quotes("soccer") == []
    assert m.pinnacle_fetch_failed("soccer") is True
    assert m._PINNACLE_BACKOFF.get("soccer", 0) > 0


def test_a_network_failure_is_reported_as_an_outage(monkeypatch):
    """Une RetryError qui n'enveloppe aucun code HTTP (timeout, DNS) doit
    quand même poser le drapeau : elle remonte, mais pas en silence."""
    import httpx
    from tenacity import RetryError
    import src.main as m

    class _Attempt:
        def exception(self): return httpx.ConnectError("dns")

    monkeypatch.setattr(m, "PinnacleScraper", _raising(RetryError(_Attempt())))
    monkeypatch.setattr(m, "_PINNACLE_GAP", 0.0)
    _clean(m)

    with pytest.raises(Exception):
        m.fetch_pinnacle_quotes("soccer")
    assert m.pinnacle_fetch_failed("soccer") is True


def test_an_out_of_season_sport_stops_burning_the_quota(monkeypatch):
    """Quatre sports scannés font huit requêtes Pinnacle par cycle, dont
    quatre pour des calendriers vides en août. C'est ce quota qui manque au
    football, bloqué 20 à 70 cycles par heure."""
    import src.main as m

    calls = {"n": 0}

    class _Empty:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport):
            calls["n"] += 1
            return iter([])

    monkeypatch.setattr(m, "PinnacleScraper", _Empty)
    monkeypatch.setattr(m, "_PINNACLE_GAP", 0.0)
    monkeypatch.setattr(m, "_PINNACLE_IDLE_AFTER", 3)
    for d in (m._PINNACLE_CACHE, m._PINNACLE_BLOCKED_UNTIL, m._PINNACLE_BACKOFF,
              m._PINNACLE_SERVED_FROM_CACHE, m._PINNACLE_FAILED,
              m._PINNACLE_EMPTY_STREAK, m._PINNACLE_LAST_PROBE):
        d.clear()

    for _ in range(12):
        assert m.fetch_pinnacle_quotes("hockey") == []
    # Trois tentatives, puis le sport passe en sondage espacé.
    assert calls["n"] == 3
    # Et surtout : jamais compté comme une panne.
    assert m.pinnacle_fetch_failed("hockey") is False


def test_the_sport_comes_back_on_its_own_when_the_season_starts(monkeypatch):
    """Sans ça il faudrait penser à rééditer SPORT_LIST en octobre."""
    from datetime import datetime, timezone
    import src.main as m
    from src.models import Book, MarketType, OddQuote, Outcome

    state = {"events": False}
    q = [OddQuote(event_key="202610011200::a__vs__b", book=Book.PINNACLE,
                  market=MarketType.H2H, outcome=Outcome(label="home"),
                  decimal_odd=2.0, fetched_at=datetime.now(timezone.utc),
                  source_event_id="1")]

    class _Seasonal:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch_market_quotes(self, sport):
            return iter(q if state["events"] else [])

    monkeypatch.setattr(m, "PinnacleScraper", _Seasonal)
    monkeypatch.setattr(m, "_PINNACLE_GAP", 0.0)
    monkeypatch.setattr(m, "_PINNACLE_IDLE_AFTER", 2)
    monkeypatch.setattr(m, "_PINNACLE_IDLE_INTERVAL", 0.0)   # sondage dû
    for d in (m._PINNACLE_CACHE, m._PINNACLE_BLOCKED_UNTIL, m._PINNACLE_BACKOFF,
              m._PINNACLE_SERVED_FROM_CACHE, m._PINNACLE_FAILED,
              m._PINNACLE_EMPTY_STREAK, m._PINNACLE_LAST_PROBE):
        d.clear()

    for _ in range(5):
        m.fetch_pinnacle_quotes("hockey")
    state["events"] = True
    assert m.fetch_pinnacle_quotes("hockey") == q
    assert m._PINNACLE_EMPTY_STREAK["hockey"] == 0
