from __future__ import annotations
from datetime import datetime, timedelta, timezone
from src.alerter import TelegramConfig

NOW = datetime(2026, 8, 17, 20, 0, tzinfo=timezone.utc)


def test_value_and_surebet_windows_are_independent(monkeypatch):
    """Ouvrir la fenêtre des value bets ne doit PAS ouvrir celle des surebets.

    Mesuré le 17/08 : la tranche 5-15 min est la meilleure du système pour les
    value bets (+10,97 %, 89,9 % de positives sur le premium). Rien n'a été
    mesuré côté surebets, et le risque y est différent — un surebet est une
    incohérence entre deux books, donc une seule cote figée fabrique un
    arbitrage fantôme, alors qu'un value bet est protégé par sa référence
    sharp."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("TELEGRAM_MIN_MINUTES_TO_KICKOFF", "5")
    monkeypatch.delenv("TELEGRAM_SUREBET_MIN_MINUTES_TO_KICKOFF", raising=False)
    cfg = TelegramConfig.from_env()
    assert cfg.min_minutes_to_kickoff == 5
    assert cfg.surebet_min_minutes_to_kickoff == 15


def test_surebet_window_can_be_set_on_its_own(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("TELEGRAM_SUREBET_MIN_MINUTES_TO_KICKOFF", "30")
    cfg = TelegramConfig.from_env()
    assert cfg.surebet_min_minutes_to_kickoff == 30


def test_defaults_are_unchanged_when_nothing_is_set(monkeypatch):
    """Un déploiement qui ne touche à rien garde exactement le comportement
    d'avant — sinon le correctif changerait le système en silence."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    for k in ("TELEGRAM_MIN_MINUTES_TO_KICKOFF",
              "TELEGRAM_SUREBET_MIN_MINUTES_TO_KICKOFF"):
        monkeypatch.delenv(k, raising=False)
    cfg = TelegramConfig.from_env()
    assert cfg.min_minutes_to_kickoff == 15
    assert cfg.surebet_min_minutes_to_kickoff == 15
