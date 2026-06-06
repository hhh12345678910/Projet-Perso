from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from src.alerter import (
    TelegramAlerter,
    TelegramConfig,
    format_surebet,
    format_value_bet,
    send_alerts,
    send_surebet_alerts,
)
from src.models import Book, MarketType, Outcome, ValueBet
from src.surebet import Surebet


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


def test_format_includes_ev_friendly_book_name_and_odd():
    msg = format_value_bet(_bet(ev_pct=5.17))
    assert "+5.17% EV" in msg
    # Book name is the human-friendly label, not the enum value.
    assert "Unibet" in msg
    assert "unibet_be" not in msg
    # Teams come out title-cased from the normalized event-key fragments.
    assert "Boise vs Sarasota" in msg
    assert "@ 1.86" in msg
    assert "fair 1.77" in msg
    # No more raw event_key dump in the body.
    assert "boise__vs__sarasota" not in msg


def test_format_includes_kickoff_date():
    msg = format_value_bet(_bet(ev_pct=5.17))
    # The event_key encodes 2026-06-01 00:00 UTC -> 02:00 Brussels time in summer
    # so the local kickoff shows the converted hour, with the date prefix.
    assert "📅" in msg
    # Should mention a time HH:MM
    import re
    assert re.search(r"\d{2}:\d{2}", msg)


def test_format_includes_line_when_present():
    bet = _bet(label="over", line=2.5)
    bet.market = MarketType.TOTALS
    msg = format_value_bet(bet)
    assert "over 2.5" in msg


def test_format_falls_back_when_event_key_is_unparseable():
    bet = _bet()
    bet.event_key = "garbage"
    msg = format_value_bet(bet)
    # No crash + the raw key is still surfaced so we can debug.
    assert "garbage" in msg
    assert "+5.00% EV" in msg


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


def _surebet(margin: float = 0.025, suspicious: bool = False,
             event_key: str = "202606010000::boise__vs__sarasota",
             line: float | None = None) -> Surebet:
    return Surebet(
        event_key=event_key,
        market=MarketType.H2H,
        line=line,
        legs={
            "home": (1.95, Book.UNIBET_BE),
            "draw": (3.85, Book.BETFIRST),
            "away": (4.20, Book.LADBROKES_BE),
        },
        margin=margin,
        suspicious=suspicious,
    )


def test_format_surebet_lists_every_leg_with_book_name():
    msg = format_surebet(_surebet(margin=0.0234))
    assert "SUREBET" in msg and "+2.34%" in msg
    assert "💰" in msg            # normal surebet emoji
    assert "⚠️" not in msg       # non-suspicious, no warning
    assert "Boise vs Sarasota" in msg
    # Each leg appears with its book name (friendly form, not the enum value).
    assert "Unibet" in msg
    assert "BetFirst" in msg
    assert "Ladbrokes" in msg
    assert "1.95" in msg and "3.85" in msg and "4.20" in msg
    assert "📅" in msg


def test_format_surebet_marks_suspicious_in_header_and_footer():
    sb = _surebet(margin=0.61, suspicious=True)
    msg = format_surebet(sb)
    # Different header emoji + "SUSPECT" word so it stands out in chat preview.
    assert "⚠️" in msg
    assert "SUSPECT" in msg
    # Footer reminder to verify before acting on a suspect signal.
    assert "Marge inhabituelle" in msg


def test_format_surebet_shows_line_for_totals():
    sb = _surebet(line=2.5)
    sb.market = MarketType.TOTALS
    msg = format_surebet(sb)
    assert "totals 2.5" in msg


def test_alerter_send_surebet_above_threshold():
    client = MagicMock(spec=httpx.Client)
    client.post.return_value.status_code = 200
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_surebet_margin_pct=1.0)
    with TelegramAlerter(cfg, client=client) as a:
        assert a.send_surebet(_surebet(margin=0.025)) is True
    client.post.assert_called_once()


def test_alerter_skips_low_margin_surebet():
    client = MagicMock(spec=httpx.Client)
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_surebet_margin_pct=1.0)
    with TelegramAlerter(cfg, client=client) as a:
        assert a.send_surebet(_surebet(margin=0.005)) is False
    client.post.assert_not_called()


def test_alerter_skips_suspicious_surebet():
    client = MagicMock(spec=httpx.Client)
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_surebet_margin_pct=1.0)
    with TelegramAlerter(cfg, client=client) as a:
        assert a.send_surebet(_surebet(margin=0.20, suspicious=True)) is False
    client.post.assert_not_called()


def test_send_surebet_alerts_counts_above_threshold(monkeypatch):
    sent_count = {"n": 0}

    class FakeClient:
        def post(self, url, json):
            sent_count["n"] += 1
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    monkeypatch.setattr("src.alerter.httpx.Client", lambda **_: FakeClient())
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_surebet_margin_pct=1.0)
    surebets = [
        _surebet(margin=0.005),                 # below threshold
        _surebet(margin=0.025),                 # above threshold
        _surebet(margin=0.20, suspicious=True), # suspicious -> skipped
        _surebet(margin=0.015),                 # above threshold
    ]
    assert send_surebet_alerts(surebets, cfg) == 2
    assert sent_count["n"] == 2


def test_send_surebet_alerts_returns_zero_when_no_config():
    assert send_surebet_alerts([_surebet()], config=None) == 0


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
