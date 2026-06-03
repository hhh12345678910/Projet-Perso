from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from src.alerter import (
    TelegramAlerter,
    TelegramConfig,
    format_value_bet,
    send_alerts,
)
from src.models import Book, MarketType, Outcome, ValueBet


NOW = datetime(2026, 5, 28, tzinfo=timezone.utc)


def _bet(ev_pct: float = 5.0, label: str = "home", line: float | None = None) -> ValueBet:
    return ValueBet(
        event_key="202606010000::boise__vs__sarasota",
        book=Book.UNIBET_BE,
        market=MarketType.H2H,
        outcome=Outcome(label=label, line=line),
        odd_taken=1.86,
        fair_prob=0.5650,
        fair_odd=1.77,
        ev_pct=ev_pct,
        kelly_stake_pct=1.50,
        detected_at=NOW,
    )


def test_config_from_env_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert TelegramConfig.from_env() is None


def test_config_from_env_reads_credentials(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("TELEGRAM_MIN_EV", "4.5")
    c = TelegramConfig.from_env()
    assert c is not None
    assert c.bot_token == "tok123"
    assert c.chat_id == "42"
    assert c.min_ev_pct == 4.5


def test_format_includes_ev_book_event_and_odd():
    msg = format_value_bet(_bet(ev_pct=5.17))
    assert "+5.17% EV" in msg
    assert "unibet_be" in msg
    assert "boise__vs__sarasota" in msg
    assert "@ 1.86" in msg
    assert "fair 1.77" in msg


def test_format_includes_line_when_present():
    bet = _bet(label="over", line=2.5)
    bet.market = MarketType.TOTALS
    msg = format_value_bet(bet)
    assert "over 2.5" in msg


def test_alerter_sends_message_above_threshold():
    client = MagicMock(spec=httpx.Client)
    client.post.return_value.status_code = 200
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0)
    with TelegramAlerter(cfg, client=client) as a:
        assert a.send_value_bet(_bet(ev_pct=5.0)) is True
    client.post.assert_called_once()
    args, kwargs = client.post.call_args
    assert "/bott/sendMessage" in args[0]
    assert kwargs["json"]["chat_id"] == "c"
    assert kwargs["json"]["parse_mode"] == "HTML"


def test_alerter_skips_low_ev_silently():
    client = MagicMock(spec=httpx.Client)
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0)
    with TelegramAlerter(cfg, client=client) as a:
        assert a.send_value_bet(_bet(ev_pct=2.0)) is False
    client.post.assert_not_called()


def test_alerter_reports_non_200_through_print_fn():
    client = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.status_code = 403
    resp.text = "forbidden"
    client.post.return_value = resp
    printed = []
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0)
    with TelegramAlerter(cfg, client=client, print_fn=printed.append) as a:
        assert a.send_value_bet(_bet(ev_pct=5.0)) is False
    assert printed and "403" in printed[0]


def test_alerter_swallows_network_failure():
    client = MagicMock(spec=httpx.Client)
    client.post.side_effect = httpx.ConnectError("boom")
    printed = []
    cfg = TelegramConfig(bot_token="t", chat_id="c")
    with TelegramAlerter(cfg, client=client, print_fn=printed.append) as a:
        assert a.send_value_bet(_bet(ev_pct=5.0)) is False
    assert printed and "boom" in printed[0]


def test_send_alerts_returns_zero_when_no_config():
    assert send_alerts([_bet()], config=None) == 0


def test_send_alerts_counts_sent_only_above_threshold(monkeypatch):
    sent_count = {"n": 0}

    class FakeClient:
        def post(self, url, json):
            sent_count["n"] += 1
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    monkeypatch.setattr("src.alerter.httpx.Client", lambda **_: FakeClient())
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0)
    bets = [_bet(ev_pct=2.0), _bet(ev_pct=5.0), _bet(ev_pct=4.1)]
    assert send_alerts(bets, cfg) == 2
    assert sent_count["n"] == 2
