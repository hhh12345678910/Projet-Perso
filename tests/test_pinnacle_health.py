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
