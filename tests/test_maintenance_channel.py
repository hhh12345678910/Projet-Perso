"""Les pannes ont leur canal ; les value bets exceptionnels gardent le leur.

Avant ce commit, `send_system_alert` livrait sur le canal CRITIQUE, celui des
value bets exceptionnels. Les deux flux sont urgents mais ne demandent pas la
même chose : une grosse cote se joue dans la minute, un book muet se répare.
Mêlés, ils obligent à trier à l'oeil au pire moment — et une panne noyée dans
le flux des grosses cotes est une panne qu'on rate (incident Pinnacle de
1 h 30, §12, vu par personne).

Deux garde-fous portent le vrai risque de ce changement :
  * `test_sans_variable_le_comportement_est_inchange` — un réglage absent ne
    doit RIEN changer en production, sinon la mise à jour déplace des alertes
    chez un utilisateur qui n'a rien demandé ;
  * `test_une_panne_ne_disparait_jamais` — le nouveau canal ne doit pas
    créer un trou : sans aucun canal configuré, l'alerte part quand même.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import src.alerter as al
from src import main
from src.alerter import TelegramConfig, send_system_alert
from src.models import Book, MarketType, Outcome, ValueBet

MAINTENANT = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def _cfg(**kw) -> TelegramConfig:
    base = dict(bot_token="jeton", chat_id="PRINCIPAL", min_minutes_to_kickoff=0)
    base.update(kw)
    return TelegramConfig(**base)


@pytest.fixture
def envois(monkeypatch) -> list[tuple[str, str]]:
    """Espionne `_send` au niveau de la CLASSE : `send_system_alert` construit
    son propre alerter, un faux posé sur une instance ne le verrait pas."""
    out: list[tuple[str, str]] = []

    def _faux(self, text, chat_id, reply_markup=None):
        out.append((chat_id, text))
        return True

    monkeypatch.setattr(al.TelegramAlerter, "_send", _faux)
    return out


# ══ le comportement demandé ════════════════════════════════════════════
def test_les_pannes_partent_sur_le_canal_maintenance(envois):
    cfg = _cfg(maintenance_chat_id="MAINTENANCE", critical_chat_id="CRITIQUE")
    assert send_system_alert(cfg, "🚨 Pinnacle muet") is True
    assert [c for c, _ in envois] == ["MAINTENANCE"]


def test_le_canal_critique_n_est_plus_touche_par_les_pannes(envois):
    """C'est tout l'objet du commit : « Value Bet Exceptionnel » doit rester
    un canal de paris, pas d'exploitation."""
    cfg = _cfg(maintenance_chat_id="MAINTENANCE", critical_chat_id="CRITIQUE")
    send_system_alert(cfg, "🚨 unibet_be muet")
    assert "CRITIQUE" not in {c for c, _ in envois}


# ══ ce qui ne doit pas bouger ══════════════════════════════════════════
def test_sans_variable_le_comportement_est_inchange(envois):
    """Le garde-fou qui compte : une installation qui n'ajoute pas la variable
    doit voir exactement ce qu'elle voyait avant."""
    cfg = _cfg(critical_chat_id="CRITIQUE")
    send_system_alert(cfg, "🚨 Pinnacle muet")
    assert [c for c, _ in envois] == ["CRITIQUE"]


def test_une_panne_ne_disparait_jamais(envois):
    """Ni maintenance ni critique : l'alerte doit quand même sortir. Un canal
    d'exploitation vide ne doit pas transformer une panne en silence."""
    cfg = _cfg()
    assert send_system_alert(cfg, "🚨 Pinnacle muet") is True
    assert [c for c, _ in envois] == ["PRINCIPAL"]


def test_sans_config_telegram_ce_n_est_toujours_pas_une_erreur(envois):
    assert send_system_alert(None, "🚨 Pinnacle muet") is False
    assert envois == []


def test_le_routage_des_value_bets_n_est_pas_touche(monkeypatch, envois):
    """Un value bet à EV extrême doit continuer d'aller au canal critique, et
    JAMAIS au canal maintenance : le nouveau canal n'attrape que les pannes."""
    monkeypatch.setattr(al, "_load_played_keys", lambda: (set(), set()))
    monkeypatch.setattr(al, "_load_books_alert_off", lambda: set())
    cfg = _cfg(maintenance_chat_id="MAINTENANCE", critical_chat_id="CRITIQUE",
               min_critical_ev_pct=35.0)
    bet = ValueBet(
        event_key="209912011200::a__vs__b", book=Book.UNIBET_BE,
        market=MarketType.H2H, outcome=Outcome("home"), odd_taken=2.10,
        fair_prob=0.55, fair_odd=1.82, ev_pct=50.0, kelly_stake_pct=1.0,
        detected_at=MAINTENANT)
    with al.TelegramAlerter(cfg) as alerter:
        alerter.send_value_bet(bet, sport="soccer")
    canaux = {c for c, _ in envois}
    assert "CRITIQUE" in canaux
    assert "MAINTENANCE" not in canaux


# ══ la lecture de l'environnement ══════════════════════════════════════
def test_la_variable_d_environnement_est_lue(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "jeton")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "PRINCIPAL")
    monkeypatch.setenv("TELEGRAM_MAINTENANCE_CHAT_ID", "-1002222222222")
    cfg = TelegramConfig.from_env()
    assert cfg is not None and cfg.maintenance_chat_id == "-1002222222222"


def test_une_variable_vide_vaut_absente(monkeypatch):
    """`TELEGRAM_MAINTENANCE_CHAT_ID=` dans un .env ne doit pas produire un
    chat_id vide qui avalerait silencieusement les alertes."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "jeton")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "PRINCIPAL")
    monkeypatch.setenv("TELEGRAM_MAINTENANCE_CHAT_ID", "")
    monkeypatch.setenv("TELEGRAM_CRITICAL_CHAT_ID", "CRITIQUE")
    cfg = TelegramConfig.from_env()
    assert cfg is not None and cfg.maintenance_chat_id is None
    assert cfg.effective_maintenance_chat_id == "CRITIQUE"


# ══ de bout en bout : les quatre alertes que l'utilisateur a nommées ════
@pytest.fixture(autouse=True)
def _reset_sante():
    for d in (main._PINNACLE_FAILS, main._PINNACLE_ALERTED,
              main._PINNACLE_DOWN_SINCE):
        d.clear()
    yield
    for d in (main._PINNACLE_FAILS, main._PINNACLE_ALERTED,
              main._PINNACLE_DOWN_SINCE):
        d.clear()


@pytest.fixture
def horloge(monkeypatch):
    etat = {"t": 1000.0}
    monkeypatch.setattr(main.time, "monotonic", lambda: etat["t"])
    return etat


def test_pinnacle_muet_puis_retabli_partent_en_maintenance(envois, horloge):
    """Le chemin réel, `main` compris : c'est lui qui doit livrer sur le bon
    canal, pas seulement la fonction d'envoi prise isolément."""
    cfg = _cfg(maintenance_chat_id="MAINTENANCE", critical_chat_id="CRITIQUE")
    main._pinnacle_health("soccer", ok=False, tg_cfg=cfg)   # début de panne
    horloge["t"] += (main._PINNACLE_ALERT_AFTER_MIN + 1) * 60
    main._pinnacle_health("soccer", ok=False, tg_cfg=cfg)   # la panne dure
    main._pinnacle_health("soccer", ok=True, tg_cfg=cfg)    # rétablissement
    assert [c for c, _ in envois] == ["MAINTENANCE", "MAINTENANCE"]
    assert "Pinnacle muet" in envois[0][1]
    assert "Pinnacle rétabli" in envois[1][1]
